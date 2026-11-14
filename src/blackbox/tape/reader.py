"""Forward reader (P1-12): yields frames in file order and stops at an explicit gap marker."""
from __future__ import annotations

from collections.abc import Iterator

from blackbox import constants as C
from blackbox.tape import frame as frame_mod
from blackbox.tape import segments


def iter_frames(tape_dir: str, segment_no: int, *, end_offset: int | None = None) -> Iterator[dict]:
    path = segments.seg_path(tape_dir, segment_no)
    with open(path, "rb") as fh:
        buf = fh.read()
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


def iter_tape(tape_dir: str) -> Iterator[dict]:
    for n in segments.list_segments(tape_dir):
        yield from iter_frames(tape_dir, n)
