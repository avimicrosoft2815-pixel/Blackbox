"""Regression tests for the three states the randomized kill matrix found misjudged (defects 2-4).

Each of these was a crash state the recorder *creates* and then failed to read back:
  * a segment killed during creation (seed 388) — in-place creation left a half-written header;
  * an empty tape before `meta` (seeds 209/307/386) — initialisation mistaken for corruption;
  * a preallocated segment with no frames (seed 396) — "holds frames" decided by file size.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C  # noqa: E402
from blackbox.tape import repair, scanner, segments  # noqa: E402
from blackbox.tape.writer import TapeWriter  # noqa: E402


def test_segment_creation_publishes_the_header_atomically(tmp_path):
    """A kill during creation may leave `<n>.seg.tmp`; it must never leave a half-written `<n>.seg`."""
    path = segments.create(str(tmp_path), 1, writer_uuid=b"\x02" * 16, segment_bytes=64 << 10,
                           prev_chain=b"\x00" * 32)
    assert not Path(path + ".tmp").exists()
    with open(path, "rb") as fh:
        header = segments.decode_header(fh.read(C.SEGMENT_HEADER_BYTES))
    assert header["segment_no"] == 1 and header["segment_bytes"] == 64 << 10

    # a stale .tmp from an interrupted attempt is discarded, never adopted
    with open(path + ".tmp", "wb") as fh:
        fh.write(b"garbage")
    os.unlink(path)
    segments.create(str(tmp_path), 1, writer_uuid=b"\x02" * 16, segment_bytes=64 << 10,
                    prev_chain=b"\x00" * 32)
    with open(path, "rb") as fh:
        assert segments.decode_header(fh.read(C.SEGMENT_HEADER_BYTES))["segment_no"] == 1
    assert not Path(path + ".tmp").exists()


def test_scan_of_an_empty_tape_is_not_corruption(tmp_path):
    """A tape that died before its first frame is empty, not damaged."""
    w = TapeWriter(str(tmp_path), segment_bytes=64 << 10).open()
    uuid = w.writer_uuid.hex()
    w.abort()                                    # no frames, no SEG_END, head published by open()

    scan = scanner.scan(str(tmp_path), uuid)
    assert scan.frames == []
    assert scan.truncated_tail is False
    assert scan.gaps == []
    assert scan.head is not None and scan.head.head_seq == 0


def test_a_preallocated_segment_without_frames_is_not_treated_as_full(tmp_path):
    """seed 396: file size says "full", frame decode says "empty" — decode wins."""
    w = TapeWriter(str(tmp_path), segment_bytes=64 << 10).open()
    uuid = w.writer_uuid.hex()
    tape = Path(tmp_path) / "tape" / uuid
    w.abort()

    state = repair.segment_state(str(tape), 1, segment_bytes=64 << 10)
    assert state["frames"] == 0
    assert state["empty_body"] is True
    assert state["body_bytes"] == 0
    assert os.path.getsize(segments.seg_path(str(tape), 1)) == 64 << 10
    # and an empty next segment is an interrupted roll to be adopted, not a full one to skip
    assert repair.completes_roll(state) is True
