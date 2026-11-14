"""Kill matrix seed 120 — a kill during head creation must not leave a tape that refuses to reopen."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C  # noqa: E402
from blackbox.tape import head as head_mod  # noqa: E402
from blackbox.tape import scanner, segments  # noqa: E402
from blackbox.tape.writer import TapeWriter  # noqa: E402


def test_head_file_is_created_atomically(tmp_path):
    """The name appears only when the file is complete: no zero-length window for a kill to land in."""
    w = TapeWriter(str(tmp_path), segment_bytes=64 << 10).open()
    w.emit(C.KIND_MARK, b"x")
    w.close()
    tape = Path(tmp_path) / "tape" / w.writer_uuid.hex()
    assert (tape / "head").stat().st_size == C.HEAD_BYTES
    assert not (tape / "head.tmp").exists()
    assert head_mod.read_best(str(tape / "head")) is not None


def test_unreadable_head_is_rebuilt_from_the_segments(tmp_path):
    """Zero-length head + a segment with committed frames = a legal crash state, and it must reopen."""
    w = TapeWriter(str(tmp_path), segment_bytes=64 << 10).open()
    for i in range(20):
        w.emit(C.KIND_MODEL_REQUEST, b"payload-%02d" % i)
    w.flush()
    uuid, seq = w.writer_uuid.hex(), w.head.head_seq
    w.abort()

    tape = Path(tmp_path) / "tape" / uuid
    with open(tape / "head", "r+b") as fh:      # what a kill during creation used to leave behind
        fh.truncate(0)
    assert head_mod.read_best(str(tape / "head")) is None

    reopened = TapeWriter(str(tmp_path), writer_uuid=bytes.fromhex(uuid), segment_bytes=64 << 10).open()
    assert reopened.adopted and reopened.adopted.get("rebuilt_head") == 1
    assert reopened.head.head_seq == seq
    assert reopened.head.head_off > C.SEGMENT_HEADER_BYTES
    reopened.close()

    scan = scanner.scan(str(tmp_path), uuid)
    assert scan.last_seq == seq + 1            # the SEG_END that close() writes


def test_rebuild_is_idempotent(tmp_path):
    w = TapeWriter(str(tmp_path), segment_bytes=64 << 10).open()
    w.emit(C.KIND_MARK, b"a")
    w.flush()
    uuid = w.writer_uuid.hex()
    w.abort()
    tape = Path(tmp_path) / "tape" / uuid
    with open(tape / "head", "r+b") as fh:
        fh.truncate(0)
    from blackbox.tape import repair

    first = repair.rebuild_head(str(tape))
    second = repair.rebuild_head(str(tape))
    assert first is not None and second is not None
    # same reconstruction every time; only `gen` advances, because a publish is a publish
    for key in ("rebuilt_head", "head_seq", "head_off", "frames"):
        assert first[key] == second[key], key
    assert second["head_gen_after"] == first["head_gen_after"] + 1
    assert segments.list_segments(str(tape)) == [1]
