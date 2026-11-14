"""S1–S5 / S14 / S15: every named write stage must leave a readable tape.

Each case kills a real child process (`os._exit`, no cleanup) and then scans the directory. The shape of the
assertion is always the same:

* every frame whose `publish()` returned is present;
* nothing torn is ever reported as committed.

Each event costs two seq numbers (the DATA frame and the batch CHAIN frame), so `k` published events commit
`2k` seqs.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.tape import scanner, segments
from blackbox.tape.writer import TapeWriter

CHILD = ROOT / "tests" / "_child.py"


def run_child(data_dir: Path, stage: str, events: int, die_on: int, mode: str = "append"):
    env = dict(os.environ)
    env.pop("BLACKBOX_DIE_AT", None)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, str(CHILD), str(data_dir), stage, str(events), str(die_on), mode],
                          capture_output=True, text=True, env=env, timeout=180)


def scan_dir(data_dir: Path):
    dirs = [p.name for p in (data_dir / "tape").iterdir() if p.is_dir()]
    assert len(dirs) == 1, dirs
    return scanner.scan(str(data_dir), dirs[0])


@pytest.mark.parametrize("stage", ["S1", "S2", "S3"])
def test_torn_frame_is_never_committed(tmp_path, stage):
    """A frame torn inside its own write is invisible; every earlier commit is intact."""
    proc = run_child(tmp_path, stage, events=5, die_on=3)
    assert proc.returncode == 137, proc.stderr
    result = scan_dir(tmp_path)
    assert result.last_seq == 4, result.summary()          # events 1 and 2, nothing of event 3
    assert result.completeness() == "full", result.summary()
    assert result.truncated_tail is False                  # torn bytes live beyond head_off, never read


def test_s4_torn_head_slot_falls_back_to_previous_commit(tmp_path):
    """The head slot is written in two halves; a crash between them loses at most the in-flight commit."""
    proc = run_child(tmp_path, "S4", events=5, die_on=3)
    assert proc.returncode == 137, proc.stderr
    result = scan_dir(tmp_path)
    assert result.head is not None
    assert result.last_seq == 4, result.summary()
    assert result.completeness() == "full", result.summary()


def test_s5_after_head_publish_the_commit_survives(tmp_path):
    proc = run_child(tmp_path, "S5", events=5, die_on=3)
    assert proc.returncode == 137, proc.stderr
    result = scan_dir(tmp_path)
    assert result.last_seq == 6, result.summary()          # all three events committed
    assert result.completeness() == "full"


def test_s14_after_seg_end_before_next_segment(tmp_path):
    proc = run_child(tmp_path, "S14", events=8, die_on=4, mode="roll")
    assert proc.returncode == 137, proc.stderr
    result = scan_dir(tmp_path)
    assert result.completeness() == "full", result.summary()
    assert result.last_seq >= 8


def test_s15_next_segment_durable_but_head_still_names_the_old_one(tmp_path):
    proc = run_child(tmp_path, "S15", events=8, die_on=4, mode="roll")
    assert proc.returncode == 137, proc.stderr
    result = scan_dir(tmp_path)
    assert result.completeness() == "full", result.summary()
    segs = segments.list_segments(str(tmp_path / "tape" / result.writer_uuid))
    # the orphan segment exists and is intact: recovery (P2-03) must adopt or reuse it, never lose frames
    assert segs == [1, 2], segs
    with open(segments.seg_path(str(tmp_path / "tape" / result.writer_uuid), 2), "rb") as fh:
        header = segments.decode_header(fh.read(4096))
    assert header["segment_no"] == 2


def test_clean_close_then_reopen_keeps_seq_monotonic(tmp_path):
    with TapeWriter(str(tmp_path), segment_bytes=64 * 1024) as w:
        uuid = w.writer_uuid.hex()
        for _ in range(3):
            w.append_and_publish(9, b"a")
        last = w.head.head_seq
    with TapeWriter(str(tmp_path), writer_uuid=bytes.fromhex(uuid), segment_bytes=64 * 1024) as w2:
        assert w2.head.head_seq >= last
    result = scanner.scan(str(tmp_path), uuid)
    assert result.completeness() == "full", result.summary()
