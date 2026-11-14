"""Frame encode/decode (P1-05, docs/format.md §4).

Write order inside a frame is payload → trailer → header, so a crash mid-frame leaves no parseable
header and the backward scanner can never treat a partial frame as committed data.
"""
from __future__ import annotations

import struct
import time

from blackbox import constants as C
from blackbox.codec.crc32c import checksum as _checksum

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


def encode(seq: int, kind: int, payload: bytes = b"", *, flags: int = 0, priority: int = 1,
           run_slot: int = 0, node_slot: int = 0, wall_ns: int | None = None,
           mono_ns: int | None = None, algo: int = C.CHECKSUM_CRC32C) -> bytes:
    if priority < 0 or priority > C.PRIORITY_MAX:
        raise FrameError(f"priority out of range: {priority}")
    if len(payload) > C.FRAME_MAX_PAYLOAD:
        raise FrameError(f"payload {len(payload)} > {C.FRAME_MAX_PAYLOAD}")
    pad = padded_len(len(payload)) - len(payload)
    total = C.FRAME_OVERHEAD_BYTES + len(payload) + pad
    flags = (flags & 0x0F) | (priority << C.PRIORITY_SHIFT)
    wall = time.time_ns() if wall_ns is None else wall_ns
    mono = time.perf_counter_ns() if mono_ns is None else mono_ns
    head = _HEADER.pack(C.MAGIC_FRAME, total, seq, wall, mono, kind, flags, run_slot, node_slot,
                        len(payload), 0)
    body = payload + b"\x00" * pad
    trail = _TRAILER.pack(C.MAGIC_FRAME_TAIL, total)
    crc = _checksum(head[:C.FRAME_CRC_OFF] + body + trail, algo)
    out = bytearray(total)
    out[0:C.FRAME_HEADER_BYTES] = head[:C.FRAME_CRC_OFF] + struct.pack("<I", crc)
    out[C.FRAME_HEADER_BYTES:C.FRAME_HEADER_BYTES + len(payload) + pad] = body
    out[total - C.FRAME_TRAILER_BYTES:total] = trail
    return bytes(out)


def decode(buf: bytes, off: int = 0, *, algo: int = C.CHECKSUM_CRC32C) -> tuple[dict, int]:
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
    scope = bytes(buf[off:off + C.FRAME_CRC_OFF]) + bytes(buf[off + C.FRAME_HEADER_BYTES:off + total])
    if _checksum(scope, algo) != crc:
        raise FrameError("crc mismatch")
    payload = bytes(buf[off + C.FRAME_HEADER_BYTES:off + C.FRAME_HEADER_BYTES + plen])
    return {
        "seq": seq, "kind": kind, "kind_name": C.KIND_NAMES.get(kind, f"KIND_{kind}"),
        "flags": flags & 0x0F, "priority": (flags >> C.PRIORITY_SHIFT) & 0x0F,
        "run_slot": run_slot, "node_slot": node_slot, "wall_ns": wall, "mono_ns": mono,
        "payload": payload, "len": total, "offset": off,
    }, off + total


def scan_backwards(buf: bytes, cursor: int, *, algo: int = C.CHECKSUM_CRC32C,
                   min_offset: int = C.SEGMENT_HEADER_BYTES) -> list[dict]:
    """Walk frames backwards from `cursor` (exclusive end). Stops at the first invalid frame."""
    frames: list[dict] = []
    off = cursor
    while off - C.FRAME_TRAILER_BYTES >= min_offset:
        tmagic, tlen = _TRAILER.unpack_from(buf, off - C.FRAME_TRAILER_BYTES)
        if tmagic != C.MAGIC_FRAME_TAIL or tlen < C.FRAME_OVERHEAD_BYTES:
            break
        start = off - tlen
        if start < min_offset:
            break
        try:
            fields, nxt = decode(buf, start, algo=algo)
        except FrameError:
            break
        if nxt != off:
            break
        frames.append(fields)
        off = start
    frames.reverse()
    return frames
