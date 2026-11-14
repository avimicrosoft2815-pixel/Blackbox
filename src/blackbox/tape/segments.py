"""Segment file creation, roll, and reading (P1-09, docs/format.md §5)."""
from __future__ import annotations

import os
import struct
import time

from blackbox import constants as C
from blackbox.errors import bbx
from blackbox.tape import prealloc


def seg_path(tape_dir: str, n: int) -> str:
    return os.path.join(tape_dir, f"{n:08d}.seg")


def seal_path(tape_dir: str, n: int) -> str:
    return os.path.join(tape_dir, f"{n:08d}.seal")


def encode_header(*, writer_uuid: bytes, segment_no: int, start_seq: int, segment_bytes: int,
                  prev_chain: bytes, created_wall_ns: int | None = None,
                  created_mono_ns: int | None = None, codec: int = C.CODEC_RAW) -> bytes:
    buf = bytearray(C.SEGMENT_HEADER_BYTES)
    buf[0:8] = C.MAGIC_SEG
    struct.pack_into("<HH", buf, C.SEG_VERSION_OFF, C.FORMAT_VERSION, 0)
    buf[C.SEG_WRITER_UUID_OFF:C.SEG_WRITER_UUID_OFF + 16] = writer_uuid
    struct.pack_into("<IQ", buf, C.SEG_NO_OFF, segment_no, start_seq)
    struct.pack_into("<II", buf, C.SEG_SIZE_OFF, segment_bytes, codec)
    struct.pack_into("<q", buf, C.SEG_CREATED_WALL_OFF, created_wall_ns or time.time_ns())
    struct.pack_into("<q", buf, C.SEG_CREATED_MONO_OFF, created_mono_ns or time.perf_counter_ns())
    buf[C.SEG_PREV_CHAIN_OFF:C.SEG_PREV_CHAIN_OFF + 32] = prev_chain
    from blackbox.codec.crc32c import crc32c
    struct.pack_into("<I", buf, C.SEGMENT_HEADER_CRC_OFF, crc32c(bytes(buf[:C.SEGMENT_HEADER_CRC_OFF])))
    return bytes(buf)


def decode_header(buf: bytes) -> dict:
    from blackbox.codec.crc32c import crc32c
    if len(buf) < C.SEGMENT_HEADER_BYTES or buf[0:8] != C.MAGIC_SEG:
        raise bbx("BBX-E224", "bad segment magic")
    stored = struct.unpack_from("<I", buf, C.SEGMENT_HEADER_CRC_OFF)[0]
    if crc32c(bytes(buf[:C.SEGMENT_HEADER_CRC_OFF])) != stored:
        raise bbx("BBX-E224", "segment header checksum mismatch")
    version, _flags = struct.unpack_from("<HH", buf, C.SEG_VERSION_OFF)
    if version > C.MAX_READER_FORMAT_VERSION:
        raise bbx("BBX-E221", f"segment version {version}")
    segment_no, start_seq = struct.unpack_from("<IQ", buf, C.SEG_NO_OFF)
    segment_bytes, codec = struct.unpack_from("<II", buf, C.SEG_SIZE_OFF)
    return {
        "version": version, "writer_uuid": bytes(buf[C.SEG_WRITER_UUID_OFF:C.SEG_WRITER_UUID_OFF + 16]),
        "segment_no": segment_no, "start_seq": start_seq, "segment_bytes": segment_bytes,
        "codec": codec, "created_wall_ns": struct.unpack_from("<q", buf, C.SEG_CREATED_WALL_OFF)[0],
        "created_mono_ns": struct.unpack_from("<q", buf, C.SEG_CREATED_MONO_OFF)[0],
        "prev_chain": bytes(buf[C.SEG_PREV_CHAIN_OFF:C.SEG_PREV_CHAIN_OFF + 32]),
    }


def create(tape_dir: str, n: int, *, writer_uuid: bytes, segment_bytes: int, prev_chain: bytes) -> str:
    """Create segment `n` fully preallocated, write its header, return the path.

    Raises `prealloc.PreallocError`; the writer converts that into a degraded state (never SIGBUS).
    """
    path = seg_path(tape_dir, n)
    if os.path.exists(path):
        raise bbx("BBX-E133", f"segment {n} already exists (an interrupted roll; recovery adopts it)")
    prealloc.preallocate(path, segment_bytes)
    header = encode_header(writer_uuid=writer_uuid, segment_no=n, start_seq=0,
                           segment_bytes=segment_bytes, prev_chain=prev_chain)
    fd = os.open(path, os.O_RDWR)
    try:
        os.pwrite(fd, header, 0)
        os.fsync(fd)      # once per segment: cheap and makes the header durable before it is mapped
    finally:
        os.close(fd)
    return path


def list_segments(tape_dir: str) -> list[int]:
    out = []
    try:
        names = os.listdir(tape_dir)
    except FileNotFoundError:
        return out
    for name in names:
        if name.endswith(".seg") and name[:-4].isdigit():
            out.append(int(name[:-4]))
    return sorted(out)


def body_of(path: str, *, end_offset: int | None = None) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(C.SEGMENT_HEADER_BYTES)
        return fh.read() if end_offset is None else fh.read(max(0, end_offset - C.SEGMENT_HEADER_BYTES))


def seal_segment(tape_dir: str, n: int, *, reason: int, body: bytes, prev_chain: bytes,
                 start_seq: int, end_seq: int, frame_count: int, sealer: bytes) -> dict:
    from blackbox.tape import seal as seal_mod
    return seal_mod.write(seal_path(tape_dir, n), segment_no=n, reason=reason, start_seq=start_seq,
                          end_seq=end_seq, frame_count=frame_count, body=body,
                          prev_chain=prev_chain, sealer=sealer)
