"""Forward reader (P1-12): yields frames in file order and stops at an explicit gap marker."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from blackbox import constants as C
from blackbox.tape import frame as frame_mod
from blackbox.tape import seal as seal_mod
from blackbox.tape import segments


def iter_frames(tape_dir: str, segment_no: int, *, end_offset: int | None = None) -> Iterator[dict[str, Any]]:
    path = segments.seg_path_any(tape_dir, segment_no)
    if path is None:
        return
    buf = segments.logical_bytes(path)
    seal = seal_mod.read(segments.seal_path(tape_dir, segment_no))
    if seal is not None:
        # A sealed segment is preallocated, so the bytes after `body_len` are padding, not frames: bounding the read
        # by the footer is what makes a compacted segment (which stores exactly `body_len`) read back identical.
        end_offset = C.SEGMENT_HEADER_BYTES + seal["body_len"] if end_offset is None else end_offset
    end = len(buf) if end_offset is None else min(end_offset, len(buf))
    off = C.SEGMENT_HEADER_BYTES
    while off + C.FRAME_OVERHEAD_BYTES <= end:
        try:
            fields, nxt = frame_mod.decode(buf, off)
        except frame_mod.FrameError:
            yield {"kind_name": "GAP", "reason": "unreadable", "offset": off}
            return
        yield fields
        off = nxt


def iter_tape(tape_dir: str) -> Iterator[dict[str, Any]]:
    for n in segments.list_segments(tape_dir):
        yield from iter_frames(tape_dir, n)
