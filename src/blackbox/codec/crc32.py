"""CRC-32/ISO-HDLC — the only checksum in format v2 (C4).

Polynomial `0x04C11DB7` (reflected `0xEDB88320`), init `0xFFFFFFFF`, xorout `0xFFFFFFFF`, implemented by
`zlib.crc32` (C speed). Check value: `crc32(b"123456789") == 0xCBF43926`.

Why not CRC32C: the pure-Python Castagnoli implementation this build started with measured 110.5 µs per KiB
(≈ 9 MB/s) against 0.45 µs per KiB for `zlib.crc32` — the checksum alone would have exceeded the §15.1 event
budget. A CRC here detects torn or stale frames; tamper evidence is the BLAKE2b chain, not the CRC. Carrying a
second algorithm would also force every reader (including the Go one) to implement it for nothing.
See `docs/adr/ADR-004-crc32-not-crc32c.md` and `docs/DEVIATIONS.md` DV-005.
"""
from __future__ import annotations

import zlib

CHECK_VALUE = 0xCBF43926


def crc32(data, crc: int = 0) -> int:
    """CRC-32/ISO-HDLC of `data`, continuing from `crc`. Accepts bytes-like objects, including memoryviews."""
    return zlib.crc32(data, crc) & 0xFFFFFFFF


def checksum_parts(parts, crc: int = 0) -> int:
    """Fold several buffers without joining them (a frame's CRC covers header + payload + trailer)."""
    for part in parts:
        crc = crc32(part, crc)
    return crc
