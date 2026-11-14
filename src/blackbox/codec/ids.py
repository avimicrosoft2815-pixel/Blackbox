"""Identifiers, fingerprints and keyed hashes (P1-04).

`run_id` and `capsule_id` are time-prefixed so lexicographic order is chronological; `node_id` is derived
from content so two runs of the same code produce comparable ids (plan §20.1).
"""
from __future__ import annotations

import hashlib
import os
import time

from blackbox.codec import cbor

_B32 = "0123456789abcdefghjkmnpqrstvwxyz"   # Crockford-ish, no i/l/o/u
HASH_BYTES = 32
UUSIZE = 16


def _b32(data: bytes) -> str:
    out, bits, val = [], 0, 0
    for byte in data:
        val = (val << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_B32[(val >> bits) & 31])
    if bits:
        out.append(_B32[(val << (5 - bits)) & 31])
    return "".join(out)


def new_writer_uuid() -> bytes:
    return os.urandom(UUSIZE)


def writer_uuid_hex(uuid_bytes: bytes) -> str:
    return uuid_bytes.hex()


def _time_prefixed(prefix: str) -> str:
    now_ms = time.time_ns() // 1_000_000
    head = now_ms.to_bytes(6, "big")
    return prefix + _b32(head + os.urandom(10))


def new_run_id() -> str:
    """Sortable run id: 'r_' + 26 base32 chars."""
    return _time_prefixed("r_")


def new_capsule_id(run_id: str, seal_no: int) -> str:
    return f"{run_id}.s{seal_no}"


def blake2b(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=HASH_BYTES).digest()


def fingerprint(obj) -> bytes:
    """Stable 32-byte fingerprint of a CBOR-encodable object (key order normalised by the encoder)."""
    return blake2b(cbor.encode(obj))


def node_id(run_id: str, path: str, kind: str, ordinal: int = 0) -> str:
    """Content-derived node id: same (run, path, kind, ordinal) ⇒ same id (plan §20.1)."""
    return "n_" + _b32(blake2b(f"{run_id}|{path}|{kind}|{ordinal}".encode()))[:20]


def boot_id_hash() -> bytes:
    """First 8 bytes of BLAKE2b-256 over the kernel boot id; zeros when unavailable (Windows)."""
    for path in ("/proc/sys/kernel/random/boot_id",):
        try:
            with open(path, "rb") as fh:
                return blake2b(fh.read().strip())[:8]
        except OSError:
            continue
    return b"\x00" * 8


def keyed_hash(key: bytes, data: bytes) -> bytes:
    """HMAC-BLAKE2b-256; used so hashes cannot be rainbow-tabled off-machine (plan §12.2)."""
    return hashlib.blake2b(data, digest_size=HASH_BYTES, key=key).digest()


def host_fingerprint() -> bytes:
    """Stable-ish host identity without touching the network (no gethostname: it can hit NSS/DNS)."""
    import platform as _platform

    try:
        host = os.uname().nodename                      # POSIX, local kernel call
    except AttributeError:                              # Windows
        host = os.environ.get("COMPUTERNAME", "unknown")
    material = f"{host}|{_platform.system()}|{_platform.machine()}".encode()
    return blake2b(material)[:16] + b"\x00" * 16
