"""The dry run explains every deletion, and deletes nothing.

Retention is the operation a user is most entitled to be nervous about: it removes evidence, on purpose, and the only
thing that makes that honest is that the removal is *declared* first (a `GAP` frame) and that the reason is one the person
can check. Two properties follow, and this file is both of them:

* **explained.** Every sealed segment gets a row: `delete`, `keep`, `pinned` or `active`, with the reason and the numbers
  behind it — the keep window, the budget, the tape's size. "3 segments would go" is not checkable; "segment 2 (seq
  1–40, 64 KiB) — older than the 1 h keep window" is.
* **inert.** A dry run must not commit a `GAP` and must not unlink. This used to be false: `apply(dry_run=True)`
  appended the `GAP` before it noticed it was a dry run, so looking at the plan declared a hole over data that was still
  on disk. The next scan then reported a retention hole whose frames were sitting right there — a tape that lies about
  itself, produced by the one command meant to be safe to run.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cli import cmd_retention
from blackbox.codec import cbor
from blackbox.tape import retention as retention_mod
from blackbox.tape import scanner, segments
from blackbox.tape.writer import TapeWriter


def _rolled_tape(directory: str, *, segments: int = 3) -> tuple[TapeWriter, str]:
    """A tape with several sealed segments: roll after every couple of frames, close at the end."""
    writer = TapeWriter(directory, segment_bytes=C.MIN_SEGMENT_BYTES).open()
    writer.append(C.KIND_RUN_START, cbor.encode({"run_id": "r_00000000000000f1"}), priority=0)
    for index in range(segments):
        writer.publish()
        writer.roll()
        for inner in range(2):
            writer.append(C.KIND_MODEL_REQUEST, cbor.encode({"segment": index, "i": inner}), priority=0, run_slot=1)
    writer.publish()
    uuid = writer.writer_uuid.hex()
    writer.close()
    return writer, uuid


def _seg_files(tape_dir: str) -> list[str]:
    return sorted(name for name in os.listdir(tape_dir) if name.endswith(".seg"))


def test_every_segment_is_explained_and_the_victims_say_why(tmp_path):
    writer, uuid = _rolled_tape(str(tmp_path / "data"))
    plan = retention_mod.select(writer.tape_dir, active_segment=scanner.scan(str(tmp_path / "data"), uuid).head.active_segment,
                                max_bytes=1, keep_hours=0.001, now_ns=time.time_ns() + 10**13)
    rows = retention_mod.explain(plan)
    numbers = [row["segment"] for row in rows]
    assert numbers == sorted(numbers)
    explained = {row["segment"] for row in rows}
    assert {c["segment"] for c in plan["candidates"]} <= explained, "a sealed segment went unmentioned"
    assert any(row["verdict"] == "active" for row in rows), "the active segment must be named as such"
    for row in rows:
        assert row["reasons"], f"segment {row['segment']} has no reason"
        assert row["verdict"] in {"delete", "keep", "pinned", "active"}
    victims = [row for row in rows if row["verdict"] == "delete"]
    assert victims, "with a 1-byte budget and an expired window something must be a victim"
    for row in victims:
        assert row["bytes"] > 0 and (row["first_seq"], row["last_seq"]) != (None, None)


def test_a_dry_run_commits_no_gap_and_deletes_no_file(tmp_path):
    writer, uuid = _rolled_tape(str(tmp_path / "data"))
    data_dir = str(tmp_path / "data")
    tape_dir = writer.tape_dir
    before = _seg_files(tape_dir)
    before_frames = scanner.scan(data_dir, uuid).frame_count
    before_tail = scanner.scan(data_dir, uuid).head.tail_seq

    owner = TapeWriter(data_dir, writer_uuid=bytes.fromhex(uuid), segment_bytes=C.MIN_SEGMENT_BYTES).open()
    try:
        plan = retention_mod.apply(owner, max_bytes=1, dry_run=True)
    finally:
        owner.abort()

    assert plan["dry_run"] is True
    assert plan["deleted"], plan
    assert plan["would_declare"] == [{"from": plan["deleted"][0]["first_seq"],
                                      "to": max(entry["last_seq"] for entry in plan["deleted"]),
                                      "segments": sorted(entry["segment"] for entry in plan["deleted"])}]
    assert _seg_files(tape_dir) == before, "a dry run unlinked a segment"
    after = scanner.scan(data_dir, uuid)
    assert after.frame_count == before_frames, "a dry run wrote a frame"
    assert after.head.tail_seq == before_tail
    assert not [frame for frame in after.frames if frame["kind"] == C.KIND_GAP], "a dry run committed a GAP"


def test_the_real_pass_declares_the_hole_the_dry_run_named(tmp_path):
    writer, uuid = _rolled_tape(str(tmp_path / "data"))
    data_dir = str(tmp_path / "data")
    tape_dir = writer.tape_dir

    owner = TapeWriter(data_dir, writer_uuid=bytes.fromhex(uuid), segment_bytes=C.MIN_SEGMENT_BYTES).open()
    try:
        dry = retention_mod.apply(owner, max_bytes=1, dry_run=True)
        real = retention_mod.apply(owner, max_bytes=1, dry_run=False)
    finally:
        owner.abort()

    assert dry["would_declare"] and real["deleted"]
    assert [entry["segment"] for entry in dry["deleted"]] == [entry["segment"] for entry in real["deleted"]]
    after = scanner.scan(data_dir, uuid)
    gaps = [frame for frame in after.frames if frame["kind"] == C.KIND_GAP]
    assert gaps, "the real pass deleted without declaring the hole"
    body = cbor.decode_exact(gaps[-1]["payload"])
    assert (body["from"], body["to"]) == (dry["would_declare"][0]["from"], dry["would_declare"][0]["to"])
    for gone in real["deleted"]:
        assert not os.path.exists(segments.seg_path(tape_dir, gone["segment"]))
    # And what remains still verifies: a declared hole is a fact the reader can see, not a broken tape.
    assert after.sealed_segments or after.frames


def test_the_command_prints_one_line_per_segment_and_writes_nothing(tmp_path, capsys):
    writer, _uuid = _rolled_tape(str(tmp_path / "data"))
    before = _seg_files(writer.tape_dir)
    assert cmd_retention.main(["--data", str(tmp_path / "data"), "--max-bytes", "1"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "never deletes" in out
    assert out.count("segment ") >= 3
    assert _seg_files(writer.tape_dir) == before


def test_the_json_form_is_a_plan_a_program_can_check(tmp_path, capsys):
    _writer, uuid = _rolled_tape(str(tmp_path / "data"))
    assert cmd_retention.main(["--data", str(tmp_path / "data"), "--max-bytes", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True and payload["writer_uuid"] == uuid
    assert payload["would_declare"] and payload["would_free_bytes"] > 0
    assert {row["verdict"] for row in payload["segments"]} <= {"delete", "keep", "pinned", "active"}
    assert all(row["reasons"] for row in payload["segments"])


def test_a_directory_with_no_tape_is_an_error_not_an_empty_plan(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    assert cmd_retention.main(["--data", str(tmp_path / "empty")]) == 2
    assert "no tape" in capsys.readouterr().err
