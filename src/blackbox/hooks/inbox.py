"""The hook inbox (P3B-07): one atomic file per event, folded into the tape later.

A hook is a **synchronous checkpoint in somebody else's program**. Claude Code runs `blackbox ingest` before it uses a
tool; the agent waits. That fixes the design more than any document does:

* **The fast path writes one file and exits.** No tape, no lock, no mmap, no scan: `tmp + fdatasync + os.replace` into
  `inbox/<session>/<ts_ns>-<nonce>.json`, and the process is gone in tens of milliseconds. Anything that needed the
  tape's writer here would make the user's prompt wait for a `fsync` on a tape that another process currently owns.
* **Concurrency is the normal case, not the edge case.** A parallel tool batch fires several hooks at once, from several
  processes; `os.replace` is atomic within a directory on every filesystem this project supports, so the folder sees
  whole files or none. `tests/test_hook_inbox.py` runs 50 concurrent ingests through real subprocesses and demands 50
  files and zero corruption.
* **Folding is at-least-once, and says so.** Each file's frames are appended **and published** before the file is
  unlinked, so a kill in between re-folds it on the next pass rather than losing it. Duplicate frames are therefore
  possible in that window — which is why every payload carries `ingest_id` (the file's own name): a reader can drop a
  repeat instead of counting it twice, and the alternative (unlink-then-append) would silently *lose* an event, which is
  the failure this project exists to prevent.
* **A corrupt entry is quarantined, not skipped.** `inbox/.bad/` plus a count, `BBX-E152`. A hook that wrote a half
  file is a bug somewhere, and a folder that dropped it quietly would be the second bug.

The mapping itself lives in `hooks/events.py` and runs at fold time (D-039): `ingest` imports only the standard
library, which is what keeps its cold start in the budget a human is waiting on.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any

# The sync and the descriptor go through the platform dispatch like every other file this project writes (C29 needs
# `O_BINARY` on Windows; C32 keeps `fsync` out of call sites). The import is cheap and the cold-start measurement in
# `tests/test_hook_inbox.py` includes it, because a hook budget that only holds without the platform layer is not a
# budget — the number in `docs/STATUS.md` is the measured one.
from blackbox.platform import current as _plat_current

_plat = _plat_current()

ENVELOPE_VERSION = 1
BAD_DIR = ".bad"
STATE_NAME = "state.json"
_SUFFIX = ".json"


class FoldResult:
    """What one fold did, in numbers a caller can print and a test can assert.

    A plain class rather than a dataclass on purpose: `dataclasses` costs ~10 ms to import, and this module is on the
    import path of the command a coding agent's hook runs (`blackbox ingest`). The saving is the difference between
    fitting the 40 ms budget and not, which is why it is spent here and not somewhere it would only be tidier.
    """

    __slots__ = ("files", "frames", "quarantined", "sessions")

    def __init__(self) -> None:
        self.files = 0
        self.frames = 0
        self.quarantined: list[str] = []
        self.sessions: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.quarantined

    def to_dict(self) -> dict[str, Any]:
        return {"files": self.files, "frames": self.frames,
                "quarantined": list(self.quarantined), "sessions": list(self.sessions)}


def default_dir(explicit: str | None = None) -> str:
    """Where the inbox lives when nobody said: `--inbox`, then `$BLACKBOX_INBOX`, then next to the tape, then home.

    A hook runs in whatever environment the coding agent was started in, which under `blackbox run` means
    `BLACKBOX_DATA_DIR` is set and the events land beside the tape they will be folded into. Started by hand, there is
    no run and `~/.blackbox/inbox` is the only answer that does not depend on the tool's working directory.
    """
    if explicit:
        return explicit
    env = os.environ.get("BLACKBOX_INBOX")
    if env:
        return env
    data_dir = os.environ.get("BLACKBOX_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "inbox")
    return os.path.join(os.path.expanduser("~"), ".blackbox", "inbox")


# ── the fast path ────────────────────────────────────────────────────────────────
def session_dir(inbox_dir: str, session_id: str) -> str:
    """`inbox/<session>/`. The id is sanitised because it arrives from another program's JSON."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (session_id or "unknown"))[:128]
    return os.path.join(inbox_dir, safe or "unknown")


def write_event(inbox_dir: str, *, tool: str, event: str, session_id: str,
                body: Any, now_ns: int | None = None, nonce: str | None = None) -> str:
    """Write one event atomically and return its path. This is the whole of `blackbox ingest`."""
    directory = session_dir(inbox_dir, session_id)
    os.makedirs(directory, exist_ok=True)
    stamp = int(now_ns if now_ns is not None else time.time_ns())
    nonce = nonce or os.urandom(4).hex()
    name = f"{stamp:019d}-{nonce}{_SUFFIX}"
    path = os.path.join(directory, name)
    envelope = {"version": ENVELOPE_VERSION, "tool": tool, "event": event, "session_id": session_id,
                "at_ns": stamp, "ingest_id": name[:-len(_SUFFIX)], "body": body}
    blob = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    # No directory fsync is attempted: Windows cannot, and on the platforms that can the window it would close (a power
    # cut in the same millisecond as the rename) is smaller than the cost of the scan that would be needed to notice it.
    # The rename is still the durability claim — the bytes are on disk before the file becomes visible.
    tmp = os.path.join(directory, f".{name}.tmp")
    fd = _plat.open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
            fh.flush()
            _plat.fdatasync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


# ── reading it back ──────────────────────────────────────────────────────────────
def pending(inbox_dir: str) -> list[str]:
    """Every un-folded event file, oldest first — the name's timestamp prefix *is* the order."""
    out: list[str] = []
    try:
        sessions = sorted(os.listdir(inbox_dir))
    except FileNotFoundError:
        return out
    for session in sessions:
        directory = os.path.join(inbox_dir, session)
        if session == BAD_DIR or not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.endswith(_SUFFIX) and not name.startswith(".") and name != STATE_NAME:
                out.append(os.path.join(directory, name))
    return out


def read_envelope(path: str) -> dict[str, Any]:
    """Parse one envelope. Raises `ValueError` for anything the folder must quarantine."""
    with open(path, "rb") as fh:
        try:
            body = json.loads(fh.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"not JSON: {exc}") from None
    if not isinstance(body, dict):
        raise ValueError("envelope is not an object")
    for key in ("version", "tool", "event", "session_id", "at_ns"):
        if key not in body:
            raise ValueError(f"envelope is missing {key}")
    if body["version"] > ENVELOPE_VERSION:
        raise ValueError(f"envelope version {body['version']} is newer than {ENVELOPE_VERSION}")
    if not isinstance(body.get("body"), (dict, type(None))):
        raise ValueError("envelope body is not an object")
    return body


def quarantine(inbox_dir: str, path: str) -> str:
    """Move one unreadable entry into `inbox/.bad/`, keeping its name. Returns the new path."""
    target_dir = os.path.join(inbox_dir, BAD_DIR)
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(path))
    if os.path.exists(target):
        target = f"{target}.{os.urandom(3).hex()}"
    os.replace(path, target)
    return target


# ── the fold ─────────────────────────────────────────────────────────────────────
def _state_path(inbox_dir: str, session: str) -> str:
    return os.path.join(inbox_dir, session, STATE_NAME)


def _read_state(inbox_dir: str, session: str) -> dict[str, Any]:
    try:
        with open(_state_path(inbox_dir, session), "rb") as fh:
            body = json.loads(fh.read().decode("utf-8"))
        if isinstance(body, dict):
            return {"next_slot": int(body.get("next_slot", 1)), "open": dict(body.get("open") or {})}
    except (OSError, ValueError, TypeError):
        pass
    return {"next_slot": 1, "open": {}}


def _write_state(inbox_dir: str, session: str, state: dict[str, Any]) -> None:
    path = _state_path(inbox_dir, session)
    blob = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        _plat.fdatasync(fh.fileno())
    os.replace(tmp, path)


def fold(inbox_dir: str, writer: Any, *, limit: int | None = None) -> FoldResult:
    """Map every pending event into `writer`, publish, then unlink. One tape per session, one pass per call.

    `writer` is a `TapeWriter` (or anything with `append`/`publish`). The state file per session is what makes a
    `PostToolUse` able to close the `NODE_OPEN` its `PreToolUse` opened **in an earlier process**: the slots are on
    disk, next to the entries, not in a folder that dies with the command that ran it.
    """
    from blackbox.codec import cbor
    from blackbox.hooks import events as events_mod

    result = FoldResult()
    for path in pending(inbox_dir)[:limit]:
        session = os.path.basename(os.path.dirname(path))
        try:
            envelope = read_envelope(path)
        except (OSError, ValueError):
            result.quarantined.append(os.path.basename(path))
            with contextlib.suppress(OSError):
                quarantine(inbox_dir, path)
            continue
        state = _read_state(inbox_dir, session)
        written = 0
        for record in events_mod.map_event(envelope["tool"], envelope["event"], envelope.get("body") or {}):
            payload = dict(record.payload)
            payload.setdefault("ingest_id", envelope.get("ingest_id", ""))
            payload.setdefault("tool", envelope["tool"])
            slot = 0
            if record.opens is not None:
                slot = int(state["next_slot"])
                state["next_slot"] = min(slot + 1, 0xFFFF)
                state["open"][record.opens] = slot
            elif record.closes is not None:
                slot = int(state["open"].pop(record.closes, 0) or 0)
                if not slot:
                    # No open node by that name: say so rather than closing something nobody saw open.
                    payload = {"name": record.closes, "kind": "tool_event",
                               "raw_name": envelope["event"], "why": "close without an open node",
                               "tool": envelope["tool"], "ingest_id": payload["ingest_id"]}
                    record = events_mod.Record(0, payload)
                    slot = 0
                else:
                    payload["node_slot"] = slot
            kind = record.kind or 0
            if not kind:
                from blackbox import constants as C
                kind = C.KIND_MARK
            writer.append(kind, cbor.encode(payload), node_slot=slot, priority=1)
            written += 1
        writer.publish()
        _write_state(inbox_dir, session, state)
        with contextlib.suppress(OSError):
            os.unlink(path)
        result.files += 1
        result.frames += written
        if session not in result.sessions:
            result.sessions.append(session)
    return result
