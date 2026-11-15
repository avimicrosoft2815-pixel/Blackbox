"""Compaction of sealed segments into `arch/` (P1-13, plan §8.7 / `format.md` §5).

A sealed segment can be rewritten with `codec = zlib` and moved under `arch/<writer>/<n>.seg`; the original is then
unlinked. Two facts from the format make this safe, and both are load-bearing:

* the **header** carries the codec (`SEG_CODEC_OFF`), so a reader knows how the frames region is stored before it
  tries to decode it;
* the `.seal` footer's `body_len`, `body_crc32` and `body_hash` are defined over the **uncompressed** body, so
  compaction cannot invalidate a footer. The footer file is not rewritten at all — "written once, immutable" stays
  true, and a compaction that changed a footer byte would be a bug visible as a changed hash.

The frames themselves are not touched: the same bytes come back out, so every chain value, offset and CRC that was
computed over them still holds. `tests/test_compact.py` asserts every frame comes back identical.

Compaction is `tmp → write → fdatasync → os.replace → fsync(dir)`, idempotent (same input ⇒ byte-identical output,
zlib level fixed), and it never runs on the active segment: an open run is not compacted (D-016's rule for
retention, applied here for the same reason).
"""
from __future__ import annotations

import os
import struct
import zlib
from typing import Any

from blackbox import constants as C
from blackbox.codec.crc32 import crc32
from blackbox.errors import bbx
from blackbox.platform import current as _plat_current
from blackbox.tape import seal as seal_mod
from blackbox.tape import segments

_plat = _plat_current()

ARCH = "arch"
LEVEL = 6


def arch_root(tape_dir: str) -> str:
    return os.path.join(tape_dir, ARCH)


def arch_path(tape_dir: str, writer_uuid_hex: str, n: int) -> str:
    return os.path.join(arch_root(tape_dir), writer_uuid_hex, f"{n:08d}.seg")


def archived_numbers(tape_dir: str) -> list[int]:
    """Segment numbers present under `arch/`, for any writer (one tape dir holds one writer, but be tolerant)."""
    out: set[int] = set()
    root = arch_root(tape_dir)
    try:
        writers = os.listdir(root)
    except FileNotFoundError:
        return []
    for writer in writers:
        try:
            names = os.listdir(os.path.join(root, writer))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for name in names:
            if name.endswith(".seg") and name[:-4].isdigit():
                out.add(int(name[:-4]))
    return sorted(out)


def find(tape_dir: str, n: int) -> str | None:
    """The live `.seg` if it is still there, else the archived copy, else None."""
    live = segments.seg_path(tape_dir, n)
    if os.path.exists(live):
        return live
    root = arch_root(tape_dir)
    try:
        writers = os.listdir(root)
    except FileNotFoundError:
        return None
    for writer in writers:
        candidate = os.path.join(root, writer, f"{n:08d}.seg")
        if os.path.exists(candidate):
            return candidate
    return None


codec_of = segments.codec_of
logical_bytes = segments.logical_bytes


def compact(tape_dir: str, n: int, *, writer_uuid_hex: str | None = None, level: int = LEVEL) -> dict[str, Any]:
    """Compact sealed segment `n` into `arch/`. Idempotent; refuses an unsealed or already-archived segment."""
    live = segments.seg_path(tape_dir, n)
    if not os.path.exists(live):
        if find(tape_dir, n) is not None:
            return {"segment": n, "compacted": False, "reason": "already archived"}
        raise bbx("BBX-E224", f"segment {n} does not exist in {tape_dir}")
    seal = seal_mod.read(segments.seal_path(tape_dir, n))
    if seal is None:
        raise bbx("BBX-E225", f"segment {n} is not sealed: compaction only touches sealed segments")
    raw = logical_bytes(live)
    header = raw[:C.SEGMENT_HEADER_BYTES]
    body = raw[C.SEGMENT_HEADER_BYTES:C.SEGMENT_HEADER_BYTES + seal["body_len"]]
    if len(body) != seal["body_len"] or crc32(body) != seal["body_crc32"]:
        raise bbx("BBX-E224", f"segment {n}: body does not match its footer, refusing to compact")
    writer = writer_uuid_hex or bytes(header[C.SEG_WRITER_UUID_OFF:C.SEG_WRITER_UUID_OFF + 16]).hex()
    dest = arch_path(tape_dir, writer, n)
    packed_body = zlib.compress(body, level)
    new_header = bytearray(header)
    struct.pack_into("<I", new_header, C.SEG_CODEC_OFF, C.CODEC_ZLIB)
    struct.pack_into("<I", new_header, C.SEGMENT_HEADER_CRC_OFF,
                     crc32(bytes(new_header[:C.SEGMENT_HEADER_CRC_OFF])))
    payload = bytes(new_header) + packed_body
    if os.path.exists(dest):
        with open(dest, "rb") as fh:
            if fh.read() == payload:
                os.unlink(live)
                _plat.fsync_dir(tape_dir)
                return {"segment": n, "compacted": True, "reason": "already identical", "path": dest,
                        "ratio": round(len(payload) / len(body), 4), "bytes": len(payload)}
        # a different archived copy: never overwrite a sealed artefact with a second opinion
        raise bbx("BBX-E224", f"{dest} exists with different bytes; refusing to replace it")
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    tmp = f"{dest}.tmp-{os.getpid()}"
    fd = _plat.open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _plat.pwrite(fd, memoryview(payload), 0)
        _plat.fdatasync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dest)
    _plat.fsync_dir(os.path.dirname(dest))
    _die("S10")                      # archived copy is durable; the original is still present
    os.unlink(live)
    _plat.fsync_dir(tape_dir)
    return {"segment": n, "compacted": True, "path": dest, "codec": C.CODEC_ZLIB,
            "body_bytes": len(body), "bytes": len(payload), "ratio": round(len(payload) / len(body), 4)}


def _die(stage: str) -> None:
    """Crash hook, same shape as `blobs._die`: `BLACKBOX_DIE_AT=S10` kills the process at the named stage."""
    if os.environ.get("BLACKBOX_DIE_AT") == stage:
        os._exit(int(os.environ.get("BLACKBOX_DIE_CODE", "137")))
