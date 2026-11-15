"""Segment file creation, roll, and reading (P1-09, docs/format.md §5)."""
from __future__ import annotations

import contextlib
import os
import struct
import time
import zlib

from blackbox import constants as C
from blackbox.errors import bbx
from blackbox.platform import current as _plat_current
from blackbox.tape import prealloc

_plat = _plat_current()


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
    from blackbox.codec.crc32 import crc32
    struct.pack_into("<I", buf, C.SEGMENT_HEADER_CRC_OFF, crc32(bytes(buf[:C.SEGMENT_HEADER_CRC_OFF])))
    return bytes(buf)


def read_header(tape_dir: str, n: int) -> dict:
    """Decode segment n's header from disk (raises OSError when the segment is gone or short)."""
    with open(seg_path(tape_dir, n), "rb") as fh:
        buf = fh.read(C.SEGMENT_HEADER_BYTES)
    if len(buf) != C.SEGMENT_HEADER_BYTES:
        raise OSError(f"segment {n}: short header ({len(buf)} B)")
    return decode_header(buf)


def decode_header(buf: bytes) -> dict:
    from blackbox.codec.crc32 import crc32
    if len(buf) < C.SEGMENT_HEADER_BYTES or buf[0:8] != C.MAGIC_SEG:
        raise bbx("BBX-E224", "bad segment magic")
    stored = struct.unpack_from("<I", buf, C.SEGMENT_HEADER_CRC_OFF)[0]
    if crc32(bytes(buf[:C.SEGMENT_HEADER_CRC_OFF])) != stored:
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


def reserve(tape_dir: str, n: int, *, segment_bytes: int) -> str:
    """Reserve space for segment `n` in `n.seg.tmp` — **before** the current segment is closed down.

    S17 (C5): a roll that discovers ENOSPC only *after* sealing and unmapping the current segment leaves the
    writer with no mapping and nowhere to write. Reserving first means a failed roll leaves the active segment
    exactly as it was, still open and still appendable. Raises `prealloc.PreallocError`.
    """
    path = seg_path(tape_dir, n)
    if os.path.exists(path):
        raise bbx("BBX-E133", f"segment {n} already exists (an interrupted roll; recovery adopts it)")
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.unlink(tmp)                      # a stale tmp from a previous attempt is never adopted
    prealloc.preallocate(tmp, segment_bytes)
    return tmp


def finish(tape_dir: str, n: int, tmp: str, *, writer_uuid: bytes, segment_bytes: int,
           prev_chain: bytes) -> str:
    """Write the header into a reserved segment and publish it atomically (fdatasync -> replace -> fsync)."""
    path = seg_path(tape_dir, n)
    header = encode_header(writer_uuid=writer_uuid, segment_no=n, start_seq=0,
                           segment_bytes=segment_bytes, prev_chain=prev_chain)
    fd = _plat.open_fd(tmp, os.O_RDWR)
    try:
        _plat.pwrite(fd, header, 0)
        _plat.fdatasync(fd)                   # the dispatch handles the Windows fallback, not the caller
    finally:
        os.close(fd)
    os.replace(tmp, path)
    # directory fsync is best effort on filesystems that refuse it
    with contextlib.suppress(OSError):
        _plat.fsync_dir(tape_dir)
    return path


def create(tape_dir: str, n: int, *, writer_uuid: bytes, segment_bytes: int, prev_chain: bytes) -> str:
    """Create segment `n` fully preallocated and atomically publish it (§8.6 step 2-3).

    The file appears at `<n>.seg.tmp`, is preallocated and headered and `fdatasync`ed there, and only then is it
    `os.replace`d into place. Without that, a kill during creation leaves a `<n>.seg` whose header is half
    written — a segment that can never be rolled onto again (found by the randomized kill matrix, seed 388).

    Raises `prealloc.PreallocError`; the writer converts that into a degraded state (never SIGBUS).
    """
    tmp = reserve(tape_dir, n, segment_bytes=segment_bytes)
    return finish(tape_dir, n, tmp, writer_uuid=writer_uuid, segment_bytes=segment_bytes,
                  prev_chain=prev_chain)


def seg_path_any(tape_dir: str, n: int) -> str | None:
    """The segment wherever it lives: the live `<n>.seg`, else its compacted copy under `arch/` (P1-13)."""
    live = seg_path(tape_dir, n)
    if os.path.exists(live):
        return live
    from blackbox.tape import arch  # local import: arch imports this module for `seg_path`
    return arch.find(tape_dir, n)


def list_segments(tape_dir: str) -> list[int]:
    """Every segment number a reader must account for, live **and** archived (P1-13 compaction)."""
    out: set[int] = set()
    try:
        names = os.listdir(tape_dir)
    except FileNotFoundError:
        names = []
    for name in names:
        if name.endswith(".seg") and name[:-4].isdigit():
            out.add(int(name[:-4]))
    from blackbox.tape import arch
    out.update(arch.archived_numbers(tape_dir))
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


def codec_of(raw: bytes) -> int:
    codec: int = struct.unpack_from("<I", raw, C.SEG_CODEC_OFF)[0]
    return codec


def logical_bytes(path: str, *, limit: int | None = None) -> bytes:
    """The segment as a reader wants it: header, then the **uncompressed** frames region (P1-13).

    A compacted segment stores its body with `codec = zlib` and says so in the header, so every reader goes through
    here instead of `open().read()`; the limit applies to logical bytes, which keeps prefix reads identical on both
    layouts.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    if len(raw) < C.SEGMENT_HEADER_BYTES:
        return raw if limit is None else raw[:limit]
    codec = codec_of(raw)
    header = raw[:C.SEGMENT_HEADER_BYTES]
    body = raw[C.SEGMENT_HEADER_BYTES:]
    if codec == C.CODEC_ZLIB:
        try:
            body = zlib.decompress(body)
        except zlib.error as exc:
            raise bbx("BBX-E224", f"{path}: compacted body does not decompress: {exc}") from None
    elif codec != C.CODEC_RAW:
        raise bbx("BBX-E221", f"{path}: segment codec {codec} is not readable by this reader")
    out = header + body
    return out if limit is None else out[:C.SEGMENT_HEADER_BYTES + max(0, limit - C.SEGMENT_HEADER_BYTES)]
