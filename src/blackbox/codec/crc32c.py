"""CRC32C (Castagnoli) and CRC-32/ISO-HDLC, pure Python.

Two algorithms, one 4-byte on-disk field: `meta.flags` bit 2 selects IEEE (CRC-32 via zlib, C speed)
instead of CRC32C. Layout is identical, so the choice is recorded, not guessed (docs/format.md §2).

Measured on the Stage 1 probe host: see bench/results/ for the per-algorithm throughput used in the
Stage 2A report. CRC32C here is table-driven (slicing-by-4); it is the slow path.
"""
from __future__ import annotations

import zlib

from blackbox.constants import CHECKSUM_CRC32C, CHECKSUM_IEEE

_POLY = 0x82F63B78  # reflected Castagnoli


def _make_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ (_POLY if crc & 1 else 0)
        table.append(crc)
    return table


_T0 = _make_table()
# slicing-by-4 tables: _T[k][i] = CRC of (i followed by k zero bytes)
_T1 = [(_T0[i] >> 8) ^ _T0[_T0[i] & 0xFF] for i in range(256)]
_T2 = [(_T1[i] >> 8) ^ _T0[_T1[i] & 0xFF] for i in range(256)]
_T3 = [(_T2[i] >> 8) ^ _T0[_T2[i] & 0xFF] for i in range(256)]


def crc32c(data: bytes, crc: int = 0) -> int:
    """CRC32C of `data`, optionally continuing from `crc` (finalised form)."""
    c = crc ^ 0xFFFFFFFF
    n = len(data)
    i = 0
    t0, t1, t2, t3 = _T0, _T1, _T2, _T3
    while n - i >= 4:
        c ^= data[i] | (data[i + 1] << 8) | (data[i + 2] << 16) | (data[i + 3] << 24)
        c = (t3[c & 0xFF] ^ t2[(c >> 8) & 0xFF] ^ t1[(c >> 16) & 0xFF] ^ t0[(c >> 24) & 0xFF])
        i += 4
    while i < n:
        c = (c >> 8) ^ t0[(c ^ data[i]) & 0xFF]
        i += 1
    return c ^ 0xFFFFFFFF


def crc_ieee(data: bytes, crc: int = 0) -> int:
    """CRC-32/ISO-HDLC via zlib (C implementation). Same 4-byte width as CRC32C."""
    return zlib.crc32(data, crc) & 0xFFFFFFFF


def checksum(data: bytes, algo: int = CHECKSUM_CRC32C, crc: int = 0) -> int:
    if algo == CHECKSUM_IEEE:
        return crc_ieee(data, crc)
    if algo == CHECKSUM_CRC32C:
        return crc32c(data, crc)
    raise ValueError(f"unknown checksum algorithm {algo!r}")
