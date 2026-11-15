"""Two-slot commit head (P1-07, docs/format.md §3).

Slots alternate and each carries a monotonically increasing `gen` plus its own CRC, so a torn write can
never destroy the newest valid record: readers take the valid slot with the greatest `gen`.
"""
from __future__ import annotations

import contextlib
import os
import struct
from dataclasses import dataclass, replace
from struct import pack_into
from typing import Any

from blackbox import constants as C
from blackbox.codec.crc32 import crc32
from blackbox.platform import current as _current_platform


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

    def to_bytes(self) -> bytes:
        buf = bytearray(C.HEAD_SLOT_BYTES)
        buf[0:8] = C.MAGIC_HEAD
        struct.pack_into("<QQQQQQ", buf, C.HEAD_GEN_OFF, self.gen, self.head_seq, self.head_off,
                         self.tail_seq, self.sealed_seq, self.active_segment)
        struct.pack_into("<II", buf, C.HEAD_FLAGS_OFF, self.flags, self.dropped_events)
        struct.pack_into("<I", buf, C.HEAD_CRC_OFF, crc32(bytes(buf[:C.HEAD_CRC_OFF])))
        return bytes(buf)

    @classmethod
    def from_bytes(cls, buf: bytes) -> HeadSlot | None:
        if len(buf) != C.HEAD_SLOT_BYTES or buf[0:8] != C.MAGIC_HEAD:
            return None
        stored = struct.unpack_from("<I", buf, C.HEAD_CRC_OFF)[0]
        if crc32(bytes(buf[:C.HEAD_CRC_OFF])) != stored:
            return None
        gen, head_seq, head_off, tail_seq, sealed_seq, active = struct.unpack_from("<QQQQQQ", buf, C.HEAD_GEN_OFF)
        flags, dropped = struct.unpack_from("<II", buf, C.HEAD_FLAGS_OFF)
        return cls(gen=gen, head_seq=head_seq, head_off=head_off, tail_seq=tail_seq,
                   sealed_seq=sealed_seq, active_segment=active, flags=flags, dropped_events=dropped)

    def with_(self, **kw: Any) -> HeadSlot:
        """Copy-with-overrides. Kept for readers; the writer's hot path mutates instead."""
        return replace(self, **kw)

    def set(self, **kw: Any) -> HeadSlot:
        for key, value in kw.items():
            setattr(self, key, value)
        return self


def empty_head_file() -> bytes:
    return b"\x00" * C.HEAD_BYTES


def read_slots(path: str | os.PathLike[str]) -> tuple[HeadSlot | None, HeadSlot | None]:
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


def read_best_in(tape_dir: str) -> HeadSlot | None:
    """Read the head of a tape directory (convenience for callers that only have the directory)."""
    return read_best(os.path.join(tape_dir, "head"))


def read_best(path: str | os.PathLike[str]) -> HeadSlot | None:
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


def slot_index_for(gen: int) -> int:
    """Slot for a given generation. Deriving it from `gen` alone keeps the alternation stateless."""
    return gen % 2


def next_slot_index(slot: HeadSlot | None) -> int:
    """Alternate slots. Writing the non-current slot means a torn write cannot clobber the last commit."""
    if slot is None:
        return 0
    return slot_index_for(slot.gen + 1)


def publish(path: str | os.PathLike[str], slot: HeadSlot, *, mode: str = "process", fd: int | None = None,
            die_point: Any = None) -> HeadSlot:
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
            _current_platform().fdatasync(fh.fileno())
    return new_slot


class HeadFile:
    """The `head` file, read and written with `pread`/`pwrite` — **no mapping anywhere** (ADR-005, C21).

    Publishing a batch is one `pwrite` of the 64 B slot at the alternate slot offset: one syscall, into the page
    cache, which is what makes the commit visible to every other process (F1) and what `kernel`/`paranoid`
    upgrade with `fdatasync(head_fd)` on the caller's schedule, never per event.

    A mapping was removed here for the same reason it was removed from the segment: a sparse file mapped on a
    full filesystem faults on first store, and the fault kills the user's process. A 64 B `pwrite` cannot be
    torn by a process kill (the kernel copies it atomically for this purpose), so the two-slot protocol now
    exists for the case that *can* tear it: a machine dying mid-write (F2/F3), where the reader's
    older-slot-CRC-wins rule is the recovery. `half_hook` keeps that case testable by forcing the body and the
    CRC into two separate writes.
    """

    __slots__ = ("_dirty", "_plat", "_slotbuf", "_slots", "fd", "half_hook", "path")

    def __init__(self, path: str, *, create: bool = True) -> None:
        self.path = path
        existed = os.path.exists(path)
        if not existed:
            # Create the file atomically: a kill between O_CREAT and the ftruncate used to leave an empty
            # `head` that no reader could parse while segments already existed (kill matrix seed 120, tmpfs).
            # tmp + ftruncate + fsync + replace + fsync(dir) means the name appears only when it is complete.
            tmp = path + ".tmp"
            fd = _current_platform().open_fd(tmp, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.ftruncate(fd, C.HEAD_BYTES)
                _current_platform().fdatasync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            with contextlib.suppress(OSError):
                _current_platform().fsync_dir(os.path.dirname(path) or ".")
            existed = True
        self._plat = _current_platform()
        self.fd = self._plat.open_fd(path, os.O_RDWR)
        try:
            if not existed or os.fstat(self.fd).st_size < C.HEAD_BYTES:
                os.ftruncate(self.fd, C.HEAD_BYTES)
                if create:
                    self._plat.fdatasync(self.fd)
        except Exception:
            os.close(self.fd)
            raise
        self._dirty = False
        self._plat = _current_platform()
        self._slotbuf = bytearray(C.HEAD_SLOT_BYTES)
        self.half_hook = None      # fault injection: forces the body and the CRC into two pwrites (S4)
        self._slots: tuple[HeadSlot | None, HeadSlot | None] = (None, None)
        self.reload()

    # ── reading ─────────────────────────────────────────────────────────────
    def reload(self) -> tuple[HeadSlot | None, HeadSlot | None]:
        s0 = HeadSlot.from_bytes(self._plat.pread(self.fd, C.HEAD_SLOT_BYTES, 0))
        base = C.HEAD_SLOT_STRIDE
        s1 = HeadSlot.from_bytes(self._plat.pread(self.fd, C.HEAD_SLOT_BYTES, base))
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
    def publish(self, slot: HeadSlot, *, split: bool = False) -> HeadSlot:
        """`gen += 1` then **one** `pwrite` of the whole 64 B slot at the alternate offset.

        The slot is built in a buffer and written once, so a process kill leaves either the old slot or the new
        one, never a half. When `half_hook` is armed (S4) the body and the CRC go out as two writes, which is
        how a machine-level tear is reproduced: the body then does not match its CRC and `best()` keeps the
        older slot.
        """
        slot.gen += 1
        base = (slot.gen % 2) * C.HEAD_SLOT_STRIDE
        buf = self._slotbuf
        buf[0:8] = C.MAGIC_HEAD
        pack_into("<QQQQQQ", buf, C.HEAD_GEN_OFF, slot.gen, slot.head_seq, slot.head_off,
                  slot.tail_seq, slot.sealed_seq, slot.active_segment)
        pack_into("<II", buf, C.HEAD_FLAGS_OFF, slot.flags, slot.dropped_events)
        pack_into("<I", buf, C.HEAD_CRC_OFF, crc32(buf[0:C.HEAD_CRC_OFF]))
        if not split:
            self._plat.pwrite(self.fd, buf, base)                 # the whole slot: one syscall, never torn
        else:
            self._plat.pwrite(self.fd, buf[0:C.HEAD_CRC_OFF], base)   # body first ...
            if self.half_hook is not None:                            # ... crash here leaves a bad CRC
                self.half_hook()
            self._plat.pwrite(self.fd, buf[C.HEAD_CRC_OFF:C.HEAD_SLOT_BYTES], base + C.HEAD_CRC_OFF)
        self._dirty = True
        return slot

    def sync(self) -> None:
        if not self._dirty:
            return
        self._plat.fdatasync(self.fd)
        self._dirty = False

    def close(self) -> None:
        with contextlib.suppress(OSError):
            if self._dirty:
                self.sync()
        os.close(self.fd)


# The name every caller used while this was a mapping; kept so the rename does not churn imports (C21).
HeadMap = HeadFile
