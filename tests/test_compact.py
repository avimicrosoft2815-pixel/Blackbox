"""P1-13 — compaction into `arch/`: the footer cannot change, and the frames must come back identical.

The plan's acceptance row is two sentences, so there are two tests that matter: *re-seal ⇒ identical footer bytes*,
and *a compacted segment reads back frame-identical*. Everything else here exists because those two are easy to
fake — a compaction that rewrote the footer, or that lost the last frame, would still "work" on a smoke test.

Why the footer cannot change: `body_len`, `body_crc32` and `body_hash` are defined over the **uncompressed** body and
the codec lives in the segment header, so compaction is a change to the frames region only. The `.seal` file is not
opened for writing at all, and `test_the_seal_file_is_byte_identical_after_compaction` checks that from the outside.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.tape import arch, reader, scanner, segments
from blackbox.tape import seal as seal_mod
from blackbox.tape.writer import TapeWriter


def _tape(tmp_path, n_events: int = 400, *, segment_bytes: int = 256 << 10) -> TapeWriter:
    """A writer with one sealed segment: roll once, so segment 1 exists sealed and segment 2 is active."""
    w = TapeWriter(str(tmp_path), segment_bytes=segment_bytes, mode="process").open()
    w.batch_bytes = C.DEFAULT_BATCH_BYTES
    for i in range(n_events):
        w.emit(C.KIND_MODEL_REQUEST, b"x" * 64 + i.to_bytes(4, "big"))
    w.flush()
    if segments.list_segments(w.tape_dir) == [1]:
        w.roll()                           # one roll, so segment 1 is sealed and segment 2 is active
        w.emit(C.KIND_MODEL_REQUEST, b"after the roll")
        w.flush()
    w.close()
    return w


def _sealed_numbers(tape_dir: str) -> list[int]:
    return [n for n in segments.list_segments(tape_dir) if segments.seal_path(tape_dir, n) and
            os.path.exists(segments.seal_path(tape_dir, n))]


def test_a_compacted_segment_is_archived_smaller_and_still_verifies(tmp_path):
    _tape(tmp_path)
    tape_dir = next((tmp_path / "tape").iterdir())
    tape_dir = str(tape_dir)
    n = _sealed_numbers(tape_dir)[0]
    live = segments.seg_path(tape_dir, n)
    before = os.path.getsize(live)

    out = arch.compact(tape_dir, n)
    assert out["compacted"] is True and out["codec"] == C.CODEC_ZLIB
    assert not os.path.exists(live), "the original must be gone: a second copy is a second truth"
    assert os.path.exists(out["path"]) and os.path.getsize(out["path"]) < before
    # the footer still verifies against the archived bytes, and the codec is in the header, not in the footer
    assert seal_mod.verify(out["path"], segments.seal_path(tape_dir, n))["body_len"] > 0
    assert arch.codec_of(Path(out["path"]).read_bytes()) == C.CODEC_ZLIB


def test_the_seal_file_is_byte_identical_after_compaction(tmp_path):
    _tape(tmp_path)
    tape_dir = str(next((tmp_path / "tape").iterdir()))
    n = _sealed_numbers(tape_dir)[0]
    seal_file = segments.seal_path(tape_dir, n)
    digest = hashlib.blake2b(Path(seal_file).read_bytes()).hexdigest()
    stat = os.stat(seal_file)
    arch.compact(tape_dir, n)
    assert hashlib.blake2b(Path(seal_file).read_bytes()).hexdigest() == digest, \
        "compaction rewrote the footer: the immutability claim is broken"
    assert os.stat(seal_file).st_size == stat.st_size


def test_frames_read_back_identical_after_compaction(tmp_path):
    _tape(tmp_path)
    tape_dir = str(next((tmp_path / "tape").iterdir()))
    n = _sealed_numbers(tape_dir)[0]
    before = list(reader.iter_frames(tape_dir, n))
    assert before, "the fixture produced no frames to compare"
    arch.compact(tape_dir, n)
    after = list(reader.iter_frames(tape_dir, n))
    assert after == before, "a compacted segment must decode to the same frames, byte for byte"


def test_compaction_is_idempotent_and_never_overwrites(tmp_path):
    _tape(tmp_path)
    tape_dir = str(next((tmp_path / "tape").iterdir()))
    n = _sealed_numbers(tape_dir)[0]
    first = arch.compact(tape_dir, n)
    digest = hashlib.blake2b(Path(first["path"]).read_bytes()).hexdigest()
    again = arch.compact(tape_dir, n)
    assert again["compacted"] is False and again["reason"] == "already archived"
    assert hashlib.blake2b(Path(first["path"]).read_bytes()).hexdigest() == digest


def test_an_unsealed_segment_is_refused(tmp_path):
    w = TapeWriter(str(tmp_path), segment_bytes=C.DEFAULT_SEGMENT_BYTES, mode="process").open()
    w.emit(C.KIND_MODEL_REQUEST, b"only")
    w.flush()
    tape_dir = w.tape_dir
    n = segments.list_segments(tape_dir)[-1]        # the active segment: no footer until roll or close
    with pytest.raises(Exception) as exc:
        arch.compact(tape_dir, n)
    w.close()
    assert "BBX-E225" in str(exc.value), "an active segment has no footer, so compaction must refuse it"
    assert arch.compact(tape_dir, n)["compacted"] is True, "after close it is sealed, and compaction applies"


def test_a_corrupted_body_is_refused_with_e224(tmp_path):
    _tape(tmp_path)
    tape_dir = str(next((tmp_path / "tape").iterdir()))
    n = _sealed_numbers(tape_dir)[0]
    live = segments.seg_path(tape_dir, n)
    with open(live, "r+b") as fh:
        fh.seek(C.SEGMENT_HEADER_BYTES + 8)
        byte = fh.read(1)
        fh.seek(C.SEGMENT_HEADER_BYTES + 8)
        fh.write(bytes([byte[0] ^ 0xFF]))
    with pytest.raises(Exception) as exc:
        arch.compact(tape_dir, n)
    assert "BBX-E224" in str(exc.value)


def test_the_scanner_sees_archived_segments_as_present(tmp_path):
    _tape(tmp_path)
    tape_dir = str(next((tmp_path / "tape").iterdir()))
    writer_uuid = Path(tape_dir).name
    data_dir = str(tmp_path)
    before = scanner.scan(data_dir, writer_uuid)
    n = _sealed_numbers(tape_dir)[0]
    arch.compact(tape_dir, n)
    after = scanner.scan(data_dir, writer_uuid)
    assert after.segments_seen == before.segments_seen
    assert after.last_seq == before.last_seq
    assert after.bad_segments == [] and after.truncated_tail is False
    assert after.completeness() == before.completeness()
