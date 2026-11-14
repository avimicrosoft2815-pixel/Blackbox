"""Two-slot commit head (P1-07, docs/format.md §3).

Slots alternate and each carries a monotonically increasing `gen` plus its own CRC, so a torn write can
never destroy the newest valid record: readers take the valid slot with the greatest `gen`.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, replace

from blackbox import constants as C
from blackbox.codec.crc32c import crc32c
from blackbox.errors import bbx


@dataclass(frozen=True)
class HeadSlot:
    gen: int = 0
    head_seq: int = 0
    head_off: int = C.SEGMENT_HEADER_BYTES
    tail_seq: int = 0
    sealed_seq: int = 0
    active_segment: int = 1
    flags: int = 0
    dropped_events: int = 0

    def to_bytes(self) -> bytes:
        buf = bytearray(C.HEAD_SLOT_BYTES)
        buf[0:8] = C.MAGIC_HEAD
        struct.pack_into("<QQQQQQ", buf, C.HEAD_GEN_OFF, self.gen, self.head_seq, self.head_off,
                         self.tail_seq, self.sealed_seq, self.active_segment)
        struct.pack_into("<II", buf, C.HEAD_FLAGS_OFF, self.flags, self.dropped_events)
        struct.pack_into("<I", buf, C.HEAD_CRC_OFF, crc32c(bytes(buf[:C.HEAD_CRC_OFF])))
        return bytes(buf)

    @classmethod
    def from_bytes(cls, buf: bytes) -> "HeadSlot | None":
        if len(buf) != C.HEAD_SLOT_BYTES or buf[0:8] != C.MAGIC_HEAD:
            return None
        stored = struct.unpack_from("<I", buf, C.HEAD_CRC_OFF)[0]
        if crc32c(bytes(buf[:C.HEAD_CRC_OFF])) != stored:
            return None
        gen, head_seq, head_off, tail_seq, sealed_seq, active = struct.unpack_from("<QQQQQQ", buf, C.HEAD_GEN_OFF)
        flags, dropped = struct.unpack_from("<II", buf, C.HEAD_FLAGS_OFF)
        return cls(gen=gen, head_seq=head_seq, head_off=head_off, tail_seq=tail_seq,
                   sealed_seq=sealed_seq, active_segment=active, flags=flags, dropped_events=dropped)

    def with_(self, **kw) -> "HeadSlot":
        return replace(self, **kw)


def empty_head_file() -> bytes:
    return b"\x00" * C.HEAD_BYTES


def read_slots(path: str | os.PathLike) -> tuple[HeadSlot | None, HeadSlot | None]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(C.HEAD_BYTES)
    except FileNotFoundError:
        return None, None
    if len(data) < C.HEAD_SLOT_STRIDE + C.HEAD_SLOT_BYTES:
        return None, None
    s0 = HeadSlot.from_bytes(data[0:C.HEAD_SLOT_BYTES])
    s1 = HeadSlot.from_bytes(data[C.HEAD_SLOT_STRIDE:C.HEAD_SLOT_STRIDE + C.HEAD_SLOT_BYTES])
    return s0, s1


def read_best(path: str | os.PathLike) -> HeadSlot | None:
    s0, s1 = read_slots(path)
    if s0 is None and s1 is None:
        return None
    if s0 is None:
        return s1
    if s1 is None:
        return s0
    return s0 if s0.gen >= s1.gen else s1


def write_slot(buf: bytearray, index: int, slot: HeadSlot) -> None:
    """Lay one slot into `buf` in place (no I/O; `publish` writes it out)."""
    data = slot.to_bytes()
    base = index * C.HEAD_SLOT_STRIDE
    buf[base:base + C.HEAD_SLOT_BYTES] = data


def next_slot_index(slot: HeadSlot | None) -> int:
    """Alternate slots. Writing the non-current slot means a torn write cannot clobber the last commit."""
    if slot is None:
        return 0
    return slot.gen % 2          # alternate: gen1 -> slot1, gen2 -> slot0, ...


def publish(path: str | os.PathLike, slot: HeadSlot, *, mode: str = "process", fd=None, die_point=None) -> HeadSlot:
    """Publish `slot` (with gen+1) into the non-current slot and flush it."""
    current = read_best(path)
    index = next_slot_index(current)
    gen = (current.gen if current else 0) + 1
    new_slot = slot.with_(gen=gen)
    base = index * C.HEAD_SLOT_STRIDE
    with open(path, "r+b") as fh:
        buf = bytearray(fh.read(C.HEAD_BYTES))
        if len(buf) < C.HEAD_BYTES:
            buf.extend(b"\x00" * (C.HEAD_BYTES - len(buf)))
        write_slot(buf, index, new_slot)
        # two halves: a crash between them leaves a slot whose CRC does not match, so readers fall back
        # to the other slot (the previous commit). This is the S4 fault we must survive.
        fh.seek(base)
        fh.write(bytes(buf[base:base + 32]))
        fh.flush()
        if die_point is not None:
            die_point("S4")
        fh.seek(base + 32)
        fh.write(bytes(buf[base + 32:base + C.HEAD_SLOT_BYTES]))
        fh.flush()
        if mode in ("kernel", "paranoid"):
            if hasattr(os, "fdatasync"):
                os.fdatasync(fh.fileno())
            else:  # Windows
                os.fsync(fh.fileno())
    return new_slot
