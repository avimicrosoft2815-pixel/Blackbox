"""The per-machine redaction key (DV-013, §12.2 stage 7, D8).

`hash` mode's whole claim is that two appearances of the same secret can be correlated without the secret being
recoverable. An unkeyed `sha256` breaks the second half of that for short secrets: a 12-character API key has few
enough candidates that a digest can be brute-forced, and a `[REDACTED:openai:a1b2c3d4]` that can be turned back into
the key is not redaction. So the digest is an **HMAC-SHA256 keyed by a 32-byte per-machine key**:

* the key lives in `~/.blackbox/keys/redaction.key`, mode `0600`, created on first use with `O_EXCL` so two processes
  starting at once cannot each write one, and never rewritten once it exists;
* `BLACKBOX_KEY_PATH` overrides the location, for a container with a read-only home or a test;
* the key never leaves the machine, is never written into a tape or a capsule, and is not derivable from one. What a
  capsule records is the **key id** — the first 8 hex characters of `sha256(key)` — so a reader can tell that two
  capsules were pseudonymised with the same key without being able to use the name to recover anything;
* if the key cannot be read or created (a read-only home, a sandbox that forbids the path), the redactor says so: it
  falls back to an unkeyed digest **and records `"keyed": false`** in the summary, rather than pretending. A capsule
  that claims a keyed pseudonym it did not use is worse than one that was never keyed at all.

This is deliberately not a KMS integration. D8 left the choice open between none / per-machine key / user KMS and the
architecture picked the per-machine key; the key is one file with one owner, and a team that wants rotation deletes it
and accepts that new pseudonyms stop matching old ones — which is what rotation means.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time

from blackbox.platform import current as _plat_current

_plat = _plat_current()

KEY_BYTES = 32
KEY_DIR = os.path.join(".blackbox", "keys")
KEY_FILE = "redaction.key"
ENV_KEY_PATH = "BLACKBOX_KEY_PATH"


class KeyUnavailable(RuntimeError):
    """The key could not be read or created; the caller decides whether to fall back, and records that it did."""


def default_key_path() -> str:
    """`~/.blackbox/keys/redaction.key`, or `BLACKBOX_KEY_PATH` when it is set."""
    override = os.environ.get(ENV_KEY_PATH)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), KEY_DIR, KEY_FILE)


def key_id(key: bytes) -> str:
    """A name for a key that cannot be used as the key: `sha256(key)[:8]`."""
    return hashlib.sha256(key).hexdigest()[:8]


def load_or_create_key(path: str | None = None, *, timeout_s: float = 2.0) -> bytes:
    """Read the machine key, creating it on first use. Raises `KeyUnavailable` when it cannot be had.

    `O_EXCL` is the creation rule: if two processes start at the same instant, one creates the file and the other's
    `open` fails with `EEXIST`, at which point it reads what the winner wrote. The subtlety that cost a CI run is that
    "what the winner wrote" can briefly be *nothing* — the winner has created the file but not yet written the key —
    so a short read is retried for a moment rather than reported as a corrupt key. A file that is still short after
    `timeout_s` is reported as broken, with the size, because at that point it is not a race.
    """
    target = path or default_key_path()
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with open(target, "rb") as handle:
                key = handle.read()
        except FileNotFoundError:
            key = b""
        except OSError as exc:                                  # a read-only home, a denied directory
            raise KeyUnavailable(f"cannot read key {target!r}: {exc}") from exc
        if len(key) >= KEY_BYTES:
            return key[:KEY_BYTES]

        if key:
            # The file exists but is not a key yet: another process is mid-write, or it is broken.
            if time.monotonic() >= deadline:
                raise KeyUnavailable(f"key file {target!r} holds {len(key)} bytes, not {KEY_BYTES}; "
                                     "delete it to have a new one created")
            time.sleep(0.01)
            continue

        directory = os.path.dirname(target)
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
                if os.name == "posix":
                    os.chmod(directory, 0o700)
            descriptor = _plat.open_fd(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            time.sleep(0.01)                                    # another process won; read its key next time round
            if time.monotonic() >= deadline:
                raise KeyUnavailable(f"key {target!r} exists but never became readable") from None
            continue
        except OSError as exc:
            raise KeyUnavailable(f"cannot create key {target!r}: {exc}") from exc
        fresh = secrets.token_bytes(KEY_BYTES)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(fresh)
                handle.flush()
                _plat.fdatasync(handle.fileno())
        except OSError as exc:
            raise KeyUnavailable(f"cannot write key {target!r}: {exc}") from exc
        return fresh
