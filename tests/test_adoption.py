"""P1-09 reopen path + P2-05: an interrupted roll is adopted, twice, without changing a byte the second time.

§16.2a S15: "head still names n; n+1 exists with a header and an empty body; recovery adopts it as active
(idempotent)". This module is that assertion, for both paths the plan names: the writer's reopen path and
`rec/discover.py`.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.rec import discover
from blackbox.tape import head as head_mod
from blackbox.tape import scanner, segments
from blackbox.tape import seal as seal_mod
from blackbox.tape.writer import TapeWriter

CHILD = ROOT / "tests" / "_child.py"


@pytest.fixture()
def crashed_roll(tmp_path) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env.pop("BLACKBOX_DIE_AT", None)
    proc = subprocess.run([sys.executable, str(CHILD), str(tmp_path), "S15", "8", "4", "roll"],
                          capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 137, proc.stderr
    dirs = [p.name for p in (tmp_path / "tape").iterdir() if p.is_dir()]
    assert len(dirs) == 1
    return dirs[0]


def _digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_s15_state_is_what_the_plan_says(tmp_path, crashed_roll):
    tape_dir = tmp_path / "tape" / crashed_roll
    head = head_mod.read_best_in(str(tape_dir))
    assert head.active_segment == 1
    segs = segments.list_segments(str(tape_dir))
    assert segs == [1, 2]
    state = discover.classify_tape(str(tape_dir))
    assert state is not None
    assert state.orphan_segment == 2
    assert "adopt_orphan_segment" in state.actions


def test_discover_is_deterministic(tmp_path, crashed_roll):
    first = [e.to_dict() for e in discover.discover(str(tmp_path))]
    second = [e.to_dict() for e in discover.discover(str(tmp_path))]
    assert first == second
    assert first[0]["orphan_segment"] == 2


def test_writer_reopen_adopts_the_orphan(tmp_path, crashed_roll):
    tape_dir = tmp_path / "tape" / crashed_roll
    before = head_mod.read_best_in(str(tape_dir))

    with TapeWriter(str(tmp_path), writer_uuid=bytes.fromhex(crashed_roll), segment_bytes=64 * 1024) as writer:
        assert writer.adopted is not None, "reopen must complete the roll, not raise"
        assert writer.head.active_segment == 2
        assert writer.head.head_off == 4096
        assert writer.head.gen >= before.gen + 1

    # roll() seals segment n *before* creating n+1, so the seal already exists at S15 and adoption must keep
    # it byte-identical rather than re-seal it
    seal = seal_mod.read(str(tape_dir / "00000001.seal"))
    assert seal is not None and seal["reason"] == 1          # SEAL_REASON_ROLLED
    seal_mod.verify(str(tape_dir / "00000001.seg"), str(tape_dir / "00000001.seal"))

    result = scanner.scan(str(tmp_path), crashed_roll)
    assert result.completeness() == "full", result.summary()
    assert result.last_seq >= 8


def test_adoption_is_idempotent(tmp_path, crashed_roll):
    tape_dir = tmp_path / "tape" / crashed_roll
    writer = TapeWriter(str(tmp_path), writer_uuid=bytes.fromhex(crashed_roll), segment_bytes=64 * 1024).open()
    assert writer.adopted is not None
    writer.abort()                                   # leave without committing, so the state is untouched

    head_digest = _digest(str(tape_dir / "head"))
    seal_digest = _digest(str(tape_dir / "00000001.seal"))
    gen_before = head_mod.read_best_in(str(tape_dir)).gen
    segments_before = segments.list_segments(str(tape_dir))

    again = TapeWriter(str(tmp_path), writer_uuid=bytes.fromhex(crashed_roll), segment_bytes=64 * 1024).open()
    try:
        assert again.adopted is None, "a completed roll must be a no-op on the next open"
        assert again.head.active_segment == 2
        assert again.head.head_off == 4096
    finally:
        again.abort()

    assert _digest(str(tape_dir / "head")) == head_digest               # not one byte republished
    assert _digest(str(tape_dir / "00000001.seal")) == seal_digest      # never re-sealed
    assert segments.list_segments(str(tape_dir)) == segments_before     # no segment invented
    assert head_mod.read_best_in(str(tape_dir)).gen == gen_before


def test_append_after_adoption_keeps_seq_dense(tmp_path, crashed_roll):
    with TapeWriter(str(tmp_path), writer_uuid=bytes.fromhex(crashed_roll), segment_bytes=64 * 1024) as writer:
        first = writer.head.head_seq
        for _ in range(3):
            writer.append_and_publish(9, b"after-adoption")
        assert writer.head.head_seq == first + 6          # 3 data frames + 3 CHAIN frames

    result = scanner.scan(str(tmp_path), crashed_roll)
    seqs = [f["seq"] for f in result.frames]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), "no duplicate or dropped seq after adoption"


def test_adoption_writes_a_missing_seal_as_recovered(tmp_path, crashed_roll):
    """When the crash also lost the footer, adoption writes it with reason RECOVERED and it verifies."""
    tape_dir = tmp_path / "tape" / crashed_roll
    seal_path = str(tape_dir / "00000001.seal")
    os.unlink(seal_path)

    writer = TapeWriter(str(tmp_path), writer_uuid=bytes.fromhex(crashed_roll), segment_bytes=64 * 1024).open()
    try:
        assert writer.adopted is not None and writer.adopted["sealed"] == 1
    finally:
        writer.abort()

    seal = seal_mod.read(seal_path)
    assert seal is not None and seal["reason"] == 3          # SEAL_REASON_RECOVERED
    seal_mod.verify(str(tape_dir / "00000001.seg"), seal_path)
    assert scanner.scan(str(tmp_path), crashed_roll).completeness() == "full"


def test_adoption_does_not_require_a_seg_end_frame(tmp_path):
    """Found by the randomized kill matrix (seed 352): `roll()` skips `SEG_END` when it cannot fit.

    The adopted body is then the committed prefix only, and the seal must cover exactly `[4096, head_off)` —
    refusing this state left a legal crash unrecoverable, which is worse than the crash.
    """
    from blackbox import constants as C
    from blackbox.tape import meta as meta_mod
    from blackbox.tape import repair

    data_dir = tmp_path
    writer = TapeWriter(str(data_dir), segment_bytes=64 * 1024).open()
    uuid = writer.writer_uuid.hex()
    for _ in range(5):
        writer.append_and_publish(C.KIND_MODEL_REQUEST, b"x" * 64)
    writer.abort()                      # leave the head naming segment 1 with no SEG_END and no roll

    tape_dir = str(data_dir / "tape" / uuid)
    with open(os.path.join(tape_dir, "meta"), "rb") as fh:
        meta = meta_mod.decode(fh.read(C.META_BYTES))
    head = head_mod.read_best_in(tape_dir)

    # create <n+1>.seg exactly as an interrupted roll would have: valid header, empty body, no seal
    from blackbox.tape import segments
    segments.create(tape_dir, head.active_segment + 1, writer_uuid=bytes.fromhex(uuid),
                    segment_bytes=meta["segment_bytes"], prev_chain=b"\x00" * 32)

    state = repair.segment_state(tape_dir, head.active_segment)
    assert state["last_kind"] != C.KIND_SEG_END, "this fixture must not end with SEG_END"

    report = repair.adopt_orphan(tape_dir)
    assert report is not None and report["adopted"] == head.active_segment + 1
    seal = seal_mod.read(os.path.join(tape_dir, f"{head.active_segment:08d}.seal"))
    assert seal is not None
    assert seal["body_len"] == head.head_off - C.SEGMENT_HEADER_BYTES
    seal_mod.verify(os.path.join(tape_dir, f"{head.active_segment:08d}.seg"),
                    os.path.join(tape_dir, f"{head.active_segment:08d}.seal"))
    assert head_mod.read_best_in(tape_dir).active_segment == head.active_segment + 1
    assert scanner.scan(str(data_dir), uuid).completeness() == "full"
