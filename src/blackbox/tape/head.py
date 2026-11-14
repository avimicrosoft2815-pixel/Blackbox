"""Two-slot commit head (P1-07, docs/format.md §3).

Slots alternate and each carries a monotonically increasing `gen` plus its own CRC, so a torn write can
never destroy the newest valid record: readers take the valid slot with the greatest `gen`.
"""
from __future__ import annotations

import mmap
import os
import struct
from struct import pack_into
from dataclasses import dataclass, replace

from blackbox import constants as C
from blackbox.codec.crc32c import checksum
from blackbox.errors import bbx


@dataclass
class HeadSlot:
    gen: int = 0
    head_seq: int = 0
    head_off: int = C.SEGMENT_HEADER_BYTES
    tail_seq: int = 0
    sealed_seq: int = 0
    active_segment: int = 1
    flags: int = 0
    dropped_events: int = 0

    def to_bytes(self, algo: int = 0) -> bytes:
        buf = bytearray(C.HEAD_SLOT_BYTES)
        buf[0:8] = C.MAGIC_HEAD
        struct.pack_into("<QQQQQQ", buf, C.HEAD_GEN_OFF, self.gen, self.head_seq, self.head_off,
                         self.tail_seq, self.sealed_seq, self.active_segment)
        struct.pack_into("<II", buf, C.HEAD_FLAGS_OFF, self.flags, self.dropped_events)
        struct.pack_into("<I", buf, C.HEAD_CRC_OFF, checksum(bytes(buf[:C.HEAD_CRC_OFF]), algo))
        return bytes(buf)

    @classmethod
    def from_bytes(cls, buf: bytes, algo: int = 0) -> "HeadSlot | None":
        if len(buf) != C.HEAD_SLOT_BYTES or buf[0:8] != C.MAGIC_HEAD:
            return None
        stored = struct.unpack_from("<I", buf, C.HEAD_CRC_OFF)[0]
        if checksum(bytes(buf[:C.HEAD_CRC_OFF]), algo) != stored:
            return None
        gen, head_seq, head_off, tail_seq, sealed_seq, active = struct.unpack_from("<QQQQQQ", buf, C.HEAD_GEN_OFF)
        flags, dropped = struct.unpack_from("<II", buf, C.HEAD_FLAGS_OFF)
        return cls(gen=gen, head_seq=head_seq, head_off=head_off, tail_seq=tail_seq,
                   sealed_seq=sealed_seq, active_segment=active, flags=flags, dropped_events=dropped)

    def with_(self, **kw) -> "HeadSlot":
        """Copy-with-overrides. Kept for readers; the writer's hot path mutates instead."""
        return replace(self, **kw)

    def set(self, **kw) -> "HeadSlot":
        for key, value in kw.items():
            setattr(self, key, value)
        return self


def empty_head_file() -> bytes:
    return b"\x00" * C.HEAD_BYTES


def read_slots(path: str | os.PathLike, algo: int = 0) -> tuple[HeadSlot | None, HeadSlot | None]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(C.HEAD_BYTES)
    except FileNotFoundError:
        return None, None
    if len(data) < C.HEAD_SLOT_STRIDE + C.HEAD_SLOT_BYTES:
        return None, None
    s0 = HeadSlot.from_bytes(data[0:C.HEAD_SLOT_BYTES], algo)
    s1 = HeadSlot.from_bytes(data[C.HEAD_SLOT_STRIDE:C.HEAD_SLOT_STRIDE + C.HEAD_SLOT_BYTES], algo)
    return s0, s1


def read_best_in(tape_dir: str, algo: int | None = None) -> HeadSlot | None:
    """Read the head of a tape whose checksum profile is recorded in its `meta` (the normal case)."""
    if algo is None:
        from blackbox import constants as _C
        from blackbox.tape import meta as meta_mod

        try:
            with open(os.path.join(tape_dir, "meta"), "rb") as fh:
                algo = meta_mod.decode(fh.read(_C.META_BYTES))["checksum_algo"]
        except (OSError, Exception):
            algo = 0
    return read_best(os.path.join(tape_dir, "head"), algo)


def read_best(path: str | os.PathLike, algo: int = 0) -> HeadSlot | None:
    s0, s1 = read_slots(path, algo)
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


def slot_index_for(gen: int) -> int:
    """Slot for a given generation. Deriving it from `gen` alone keeps the alternation stateless."""
    return gen % 2


def next_slot_index(slot: HeadSlot | None) -> int:
    """Alternate slots. Writing the non-current slot means a torn write cannot clobber the last commit."""
    if slot is None:
        return 0
    return slot_index_for(slot.gen + 1)


def publish(path: str | os.PathLike, slot: HeadSlot, *, mode: str = "process", fd=None,
            die_point=None, algo: int = 0) -> HeadSlot:
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


class HeadMap:
    """The `head` file mapped once; publishing a batch is two stores into the mapping (§8.2 step 5-7).

    In `process` mode there is no syscall on the publish path: the stores land in the page cache of a
    MAP_SHARED mapping, which is what makes the commit visible to every other process (F1). `kernel` and
    `paranoid` add `flush` + `fdatasync(head_fd)` on the caller's schedule, never per event.
    """

    __slots__ = ("path", "mm", "fd", "_dirty", "_slots", "half_hook", "algo", "_slotbuf")

    def __init__(self, path: str, *, create: bool = True, algo: int = 0) -> None:
        self.algo = algo
        self.path = path
        existed = os.path.exists(path)
        self.fd = os.open(path, os.O_RDWR | (0 if existed else os.O_CREAT), 0o600)
        try:
            if not existed or os.fstat(self.fd).st_size < C.HEAD_BYTES:
                os.ftruncate(self.fd, C.HEAD_BYTES)
                if create:
                    os.fsync(self.fd)
            self.mm = mmap.mmap(self.fd, C.HEAD_BYTES)
        except Exception:
            os.close(self.fd)
            raise
        self._dirty = False
        self._slotbuf = bytearray(C.HEAD_SLOT_BYTES)
        self.half_hook = None      # fault injection: runs between the body store and the CRC store
        self._slots: tuple[HeadSlot | None, HeadSlot | None] = (None, None)
        self.reload()

    # ── reading ─────────────────────────────────────────────────────────────
    def reload(self) -> tuple[HeadSlot | None, HeadSlot | None]:
        s0 = HeadSlot.from_bytes(bytes(self.mm[0:C.HEAD_SLOT_BYTES]), self.algo)
        base = C.HEAD_SLOT_STRIDE
        s1 = HeadSlot.from_bytes(bytes(self.mm[base:base + C.HEAD_SLOT_BYTES]), self.algo)
        self._slots = (s0, s1)
        return self._slots

    @property
    def slots(self) -> tuple[HeadSlot | None, HeadSlot | None]:
        return self.reload()

    def best(self) -> HeadSlot | None:
        """Read back from the mapping. The writer keeps its own cursor; this is for readers and tests."""
        s0, s1 = self.reload()
        if s0 is None and s1 is None:
            return None
        if s0 is None:
            return s1
        if s1 is None:
            return s0
        return s0 if s0.gen >= s1.gen else s1

    # ── writing ─────────────────────────────────────────────────────────────
    def publish(self, slot: HeadSlot) -> HeadSlot:
        """`gen += 1` then two stores: the 60-byte body, then the CRC.

        The CRC is written last on purpose: a crash between the two stores leaves a body that does not match
        its CRC, so `best()` keeps the older slot. `process` mode does not flush: the stores are already in the
        page cache of a MAP_SHARED mapping, which is what other processes read (F1).
        """
        slot.gen += 1
        base = (slot.gen % 2) * C.HEAD_SLOT_STRIDE
        buf = self._slotbuf
        buf[0:8] = C.MAGIC_HEAD
        pack_into("<QQQQQQ", buf, C.HEAD_GEN_OFF, slot.gen, slot.head_seq, slot.head_off,
                  slot.tail_seq, slot.sealed_seq, slot.active_segment)
        pack_into("<II", buf, C.HEAD_FLAGS_OFF, slot.flags, slot.dropped_events)
        mm = self.mm
        mm[base:base + C.HEAD_CRC_OFF] = buf[0:C.HEAD_CRC_OFF]
        if self.half_hook is not None:
            self.half_hook()
        pack_into("<I", mm, base + C.HEAD_CRC_OFF, checksum(buf[0:C.HEAD_CRC_OFF], self.algo))
        self._dirty = True
        return slot

    def sync(self) -> None:
        if not self._dirty:
            return
        self.mm.flush()
        if hasattr(os, "fdatasync"):
            os.fdatasync(self.fd)
        else:                                   # pragma: no cover - Windows
            os.fsync(self.fd)
        self._dirty = False

    def close(self) -> None:
        try:
            self.mm.flush()
        except (ValueError, BufferError):
            pass
        try:
            self.mm.close()
        finally:
            os.close(self.fd)
