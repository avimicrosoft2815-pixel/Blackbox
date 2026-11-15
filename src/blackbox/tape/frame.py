"""Frame encode/decode (P1-05, docs/format.md §4).

Write order inside a frame is payload → trailer → header, so a crash mid-frame leaves no parseable
header and the backward scanner can never treat a partial frame as committed data.
"""
from __future__ import annotations

import struct
import time
from typing import Any

from blackbox import constants as C
from blackbox.codec.crc32 import checksum_parts, crc32

_HEADER = struct.Struct("<4sIqqqHHHHII")
_TRAILER = struct.Struct("<4sI")
assert _HEADER.size == C.FRAME_HEADER_BYTES, _HEADER.size
assert _TRAILER.size == C.FRAME_TRAILER_BYTES, _TRAILER.size


class FrameError(ValueError):
    pass


def padded_len(payload_len: int) -> int:
    rem = payload_len % C.FRAME_LEN_MULTIPLE
    return payload_len + (C.FRAME_LEN_MULTIPLE - rem if rem else 0)


def frame_size(payload_len: int) -> int:
    return C.FRAME_OVERHEAD_BYTES + padded_len(payload_len)


def encode_into(buf: bytearray | memoryview, off: int, seq: int, kind: int, payload: bytes | memoryview = b"", *, flags: int = 0,
                priority: int = 1, run_slot: int = 0, node_slot: int = 0, wall_ns: int | None = None,
                mono_ns: int | None = None, body_first: bool = True, hook: Any = None) -> int:
    """Write one frame at `off` inside `buf` (a bytearray or an mmap) and return its total length.

    Zero intermediate buffers: the header is packed straight into `buf`, the payload is a slice copy, and the
    CRC is folded over the three regions without joining them. Write order is payload → trailer → header
    (§8.3), so a crash mid-frame leaves a slot the backward walk refuses to treat as committed.
    """
    if priority < 0 or priority > C.PRIORITY_MAX:
        raise FrameError(f"priority out of range: {priority}")
    plen = len(payload)
    if plen > C.FRAME_MAX_PAYLOAD:
        raise FrameError(f"payload {plen} > {C.FRAME_MAX_PAYLOAD}")
    pad = padded_len(plen) - plen
    total = C.FRAME_OVERHEAD_BYTES + plen + pad
    flags = (flags & 0x0F) | (priority << C.PRIORITY_SHIFT)
    wall = time.time_ns() if wall_ns is None else wall_ns
    mono = time.perf_counter_ns() if mono_ns is None else mono_ns
    body_off = off + C.FRAME_HEADER_BYTES
    trail_off = off + total - C.FRAME_TRAILER_BYTES

    if body_first:
        if plen:
            buf[body_off:body_off + plen] = payload
        if pad:
            buf[body_off + plen:body_off + plen + pad] = _ZEROS[:pad]
        if hook is not None:
            hook("S1")
        struct.pack_into("<4sI", buf, trail_off, C.MAGIC_FRAME_TAIL, total)
        if hook is not None:
            hook("S2")

    if hook is not None:
        hook("S3")
    _HEADER.pack_into(buf, off, C.MAGIC_FRAME, total if body_first else 0, seq, wall, mono, kind, flags,
                      run_slot, node_slot, plen, 0)
    if not body_first:                      # header-only pre-pass, used when the payload arrives later
        return total
    # CRC scope is header[0,44) then payload+trailer (the 4-byte CRC field itself is excluded). Folding two
    # views of the same frame beats building three temporary buffers, and zlib.crc32 takes the previous value.
    view = memoryview(buf)
    crc = crc32(view[off:off + C.FRAME_CRC_OFF])
    crc = crc32(view[off + C.FRAME_HEADER_BYTES:off + total], crc)
    struct.pack_into("<I", buf, off + C.FRAME_CRC_OFF, crc)
    if total != frame_size(plen):            # cannot happen; keeps the invariant explicit
        raise FrameError("frame length mismatch")
    return total


_ZEROS = b"\x00" * 8


def encode(seq: int, kind: int, payload: bytes = b"", *, flags: int = 0, priority: int = 1,
           run_slot: int = 0, node_slot: int = 0, wall_ns: int | None = None,
           mono_ns: int | None = None) -> bytes:
    """Convenience wrapper that allocates the frame (tests, tools, small batches)."""
    out = bytearray(frame_size(len(payload)))
    encode_into(out, 0, seq, kind, payload, flags=flags, priority=priority, run_slot=run_slot,
                node_slot=node_slot, wall_ns=wall_ns, mono_ns=mono_ns)
    return bytes(out)


def decode(buf: bytes, off: int = 0) -> tuple[dict[str, Any], int]:
    """Decode one frame at `off`; returns (fields, next_offset). Raises FrameError."""
    end = len(buf)
    if off + C.FRAME_HEADER_BYTES + C.FRAME_TRAILER_BYTES > end:
        raise FrameError("truncated frame header")
    magic, total, seq, wall, mono, kind, flags, run_slot, node_slot, plen, crc = _HEADER.unpack_from(buf, off)
    if magic != C.MAGIC_FRAME:
        raise FrameError("bad frame magic")
    if plen > C.FRAME_MAX_PAYLOAD or total != frame_size(plen):
        raise FrameError("inconsistent frame length")
    if off + total > end:
        raise FrameError("frame runs past end of buffer")
    tmagic, tlen = _TRAILER.unpack_from(buf, off + total - C.FRAME_TRAILER_BYTES)
    if tmagic != C.MAGIC_FRAME_TAIL or tlen != total:
        raise FrameError("bad or missing trailer")
    if checksum_parts((memoryview(buf)[off:off + C.FRAME_CRC_OFF],
                       memoryview(buf)[off + C.FRAME_HEADER_BYTES:off + total])) != crc:
        raise FrameError("crc mismatch")
    payload = bytes(buf[off + C.FRAME_HEADER_BYTES:off + C.FRAME_HEADER_BYTES + plen])
    return {
        "seq": seq, "kind": kind, "kind_name": C.KIND_NAMES.get(kind, f"KIND_{kind}"),
        "flags": flags & 0x0F, "priority": (flags >> C.PRIORITY_SHIFT) & 0x0F,
        "run_slot": run_slot, "node_slot": node_slot, "wall_ns": wall, "mono_ns": mono,
        "payload": payload, "len": total, "offset": off,
    }, off + total


def scan_backwards(buf: bytes, cursor: int, *, min_offset: int = C.SEGMENT_HEADER_BYTES) -> list[dict[str, Any]]:
    """Walk frames backwards from `cursor` (exclusive end). Stops at the first invalid frame."""
    frames: list[dict[str, Any]] = []
    off = cursor
    while off - C.FRAME_TRAILER_BYTES >= min_offset:
        tmagic, tlen = _TRAILER.unpack_from(buf, off - C.FRAME_TRAILER_BYTES)
        if tmagic != C.MAGIC_FRAME_TAIL or tlen < C.FRAME_OVERHEAD_BYTES:
            break
        start = off - tlen
        if start < min_offset:
            break
        try:
            fields, nxt = decode(buf, start)
        except FrameError:
            break
        if nxt != off:
            break
        frames.append(fields)
        off = start
    frames.reverse()
    return frames
