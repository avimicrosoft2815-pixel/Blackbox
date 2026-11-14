"""Immutable `meta` page (P1-06, docs/format.md §2)."""
from __future__ import annotations

import struct
import time

from blackbox import constants as C
from blackbox.codec.crc32 import crc32
from blackbox.codec.ids import boot_id_hash, host_fingerprint
from blackbox.errors import bbx


class MetaError(ValueError):
    pass


def encode(*, writer_uuid: bytes, segment_bytes: int, vantage: int,
           pid: int | None = None, created_wall_ns: int | None = None,
           created_mono_ns: int | None = None, schema_ver: int = C.SCHEMA_VER) -> bytes:
    import os

    buf = bytearray(C.META_BYTES)
    buf[0:8] = C.MAGIC_META
    flags = 0                          # bit 2 (reserved checksum profile) must stay 0 in v1
    struct.pack_into("<HHI", buf, C.META_VERSION_OFF, C.FORMAT_VERSION, flags, segment_bytes)
    buf[C.META_WRITER_UUID_OFF:C.META_WRITER_UUID_OFF + 16] = writer_uuid
    struct.pack_into("<q", buf, C.META_CREATED_WALL_OFF, created_wall_ns or time.time_ns())
    struct.pack_into("<q", buf, C.META_CREATED_MONO_OFF, created_mono_ns or time.perf_counter_ns())
    buf[C.META_BOOT_ID_HASH_OFF:C.META_BOOT_ID_HASH_OFF + 8] = boot_id_hash()
    struct.pack_into("<I", buf, C.META_PID_OFF, pid if pid is not None else os.getpid())
    struct.pack_into("<I", buf, C.META_VANTAGE_OFF, vantage)
    struct.pack_into("<Q", buf, C.META_PROC_START_OFF, 0)      # filled by platform layer when known
    fp = host_fingerprint()
    buf[C.META_HOST_FP_OFF:C.META_HOST_FP_OFF + len(fp)] = fp
    struct.pack_into("<Q", buf, C.META_ATTRS_REF_OFF, 0)
    struct.pack_into("<H", buf, C.META_SCHEMA_VER_OFF, schema_ver)
    struct.pack_into("<I", buf, C.META_CRC_OFF, crc32(bytes(buf[:C.META_CRC_OFF])))
    return bytes(buf)


def decode(buf: bytes) -> dict:
    if len(buf) != C.META_BYTES:
        raise bbx("BBX-E222", f"meta is {len(buf)} B, expected {C.META_BYTES}")
    if buf[0:8] != C.MAGIC_META:
        raise bbx("BBX-E222", "bad meta magic")
    version, flags, segment_bytes = struct.unpack_from("<HHI", buf, C.META_VERSION_OFF)
    if version > C.MAX_READER_FORMAT_VERSION:
        raise bbx("BBX-E221", f"format_version {version} > {C.MAX_READER_FORMAT_VERSION}")
    if flags & C.META_FLAG_RESERVED_CHECKSUM:
        raise bbx("BBX-E221", "meta sets the reserved checksum-profile bit; v1 knows one algorithm")
    stored_crc = struct.unpack_from("<I", buf, C.META_CRC_OFF)[0]
    if crc32(bytes(buf[:C.META_CRC_OFF])) != stored_crc:
        raise bbx("BBX-E222", "meta checksum mismatch")
    return {
        "format_version": version,
        "layout_ring": bool(flags & C.META_FLAG_LAYOUT_RING),
        "segment_bytes": segment_bytes,
        "writer_uuid": bytes(buf[C.META_WRITER_UUID_OFF:C.META_WRITER_UUID_OFF + 16]),
        "created_wall_ns": struct.unpack_from("<q", buf, C.META_CREATED_WALL_OFF)[0],
        "created_mono_ns": struct.unpack_from("<q", buf, C.META_CREATED_MONO_OFF)[0],
        "boot_id_hash": bytes(buf[C.META_BOOT_ID_HASH_OFF:C.META_BOOT_ID_HASH_OFF + 8]),
        "pid": struct.unpack_from("<I", buf, C.META_PID_OFF)[0],
        "vantage": struct.unpack_from("<I", buf, C.META_VANTAGE_OFF)[0],
        "proc_start_ns": struct.unpack_from("<Q", buf, C.META_PROC_START_OFF)[0],
        "host_fingerprint": bytes(buf[C.META_HOST_FP_OFF:C.META_HOST_FP_OFF + 32]),
        "schema_ver": struct.unpack_from("<H", buf, C.META_SCHEMA_VER_OFF)[0],
    }
