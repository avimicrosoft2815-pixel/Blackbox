"""`.seal` footer sidecar (P1-13, docs/format.md §6).

Amendment 2026-11-14 (review finding F4, owner-approved as confined to P1-13 and layout-preserving):
`body_len`, `body_crc32` and `body_hash` are defined over the **uncompressed** body, and the codec lives in
the *segment header*. Compaction therefore rewrites `<n>.seg` only and can never invalidate a `.seal`,
which is what makes "written once, immutable" true. Offsets and sizes are unchanged.
"""
from __future__ import annotations

import os
import struct

from blackbox import constants as C
from blackbox.codec.crc32 import crc32
from blackbox.codec.ids import blake2b
from blackbox.errors import bbx


def encode(*, segment_no: int, reason: int, start_seq: int, end_seq: int, frame_count: int,
           body_len: int, body_crc32: int, body_hash: bytes, chain: bytes, sealer: bytes,
           closed_wall_ns: int) -> bytes:
    if len(body_hash) != 32 or len(chain) != 32:
        raise ValueError("body_hash and chain must be 32 bytes")
    if len(sealer) > 16:
        raise ValueError("sealer token must be <= 16 bytes")
    buf = bytearray(C.SEAL_BYTES)
    buf[0:8] = C.MAGIC_SEAL
    struct.pack_into("<II", buf, C.SEAL_NO_OFF, segment_no, reason)
    struct.pack_into("<QQQ", buf, C.SEAL_START_SEQ_OFF, start_seq, end_seq, frame_count)
    struct.pack_into("<QI", buf, C.SEAL_BODY_LEN_OFF, body_len, body_crc32)
    buf[C.SEAL_BODY_HASH_OFF:C.SEAL_BODY_HASH_OFF + 32] = body_hash
    buf[C.SEAL_CHAIN_OFF:C.SEAL_CHAIN_OFF + 32] = chain
    struct.pack_into("<q", buf, C.SEAL_CLOSED_WALL_OFF, closed_wall_ns)
    buf[C.SEAL_SEALER_OFF:C.SEAL_SEALER_OFF + len(sealer)] = sealer
    struct.pack_into("<I", buf, C.SEAL_CRC_OFF, crc32(bytes(buf[:C.SEAL_CRC_OFF])))
    return bytes(buf)


def decode(buf: bytes) -> dict:
    if len(buf) != C.SEAL_BYTES:
        raise bbx("BBX-E224", f"seal is {len(buf)} B, expected {C.SEAL_BYTES}")
    if buf[0:8] != C.MAGIC_SEAL:
        raise bbx("BBX-E224", "bad seal magic")
    if struct.unpack_from("<I", buf, C.SEAL_CRC_OFF)[0] != crc32(bytes(buf[:C.SEAL_CRC_OFF])):
        raise bbx("BBX-E224", "seal checksum mismatch")
    segment_no, reason = struct.unpack_from("<II", buf, C.SEAL_NO_OFF)
    start_seq, end_seq, frame_count = struct.unpack_from("<QQQ", buf, C.SEAL_START_SEQ_OFF)
    body_len, body_crc = struct.unpack_from("<QI", buf, C.SEAL_BODY_LEN_OFF)
    return {
        "segment_no": segment_no, "reason": reason,
        "start_seq": start_seq, "end_seq": end_seq, "frame_count": frame_count,
        "body_len": body_len, "body_crc32": body_crc,
        "body_hash": bytes(buf[C.SEAL_BODY_HASH_OFF:C.SEAL_BODY_HASH_OFF + 32]),
        "chain": bytes(buf[C.SEAL_CHAIN_OFF:C.SEAL_CHAIN_OFF + 32]),
        "closed_wall_ns": struct.unpack_from("<q", buf, C.SEAL_CLOSED_WALL_OFF)[0],
    }


def write(path: str, *, segment_no: int, reason: int, start_seq: int, end_seq: int, frame_count: int,
          body: bytes, prev_chain: bytes, sealer: bytes, closed_wall_ns: int | None = None) -> dict:
    """Write `<path>` (…/00000001.seal) atomically: tmp + fsync + replace."""
    import time

    body_hash = blake2b(body)
    chain = blake2b(prev_chain + body_hash)
    payload = encode(segment_no=segment_no, reason=reason, start_seq=start_seq, end_seq=end_seq,
                     frame_count=frame_count, body_len=len(body), body_crc32=crc32(body),
                     body_hash=body_hash, chain=chain,
                     sealer=sealer, closed_wall_ns=closed_wall_ns or time.time_ns())
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return decode(payload)


def read(path: str) -> dict | None:
    try:
        with open(path, "rb") as fh:
            return decode(fh.read(C.SEAL_BYTES))
    except FileNotFoundError:
        return None


def verify(segment_path: str, seal_path: str) -> dict:
    """Verify a sealed segment against its footer. Raises BBX-E224 on mismatch."""
    seal = read(seal_path)
    if seal is None:
        raise bbx("BBX-E224", f"no seal for {segment_path}")
    with open(segment_path, "rb") as fh:
        fh.seek(C.SEGMENT_HEADER_BYTES)
        body = fh.read(seal["body_len"])
    if len(body) != seal["body_len"]:
        raise bbx("BBX-E224", "body shorter than the footer declares")
    if crc32(body) != seal["body_crc32"] or blake2b(body) != seal["body_hash"]:
        raise bbx("BBX-E224", "body does not match the footer")
    return seal
