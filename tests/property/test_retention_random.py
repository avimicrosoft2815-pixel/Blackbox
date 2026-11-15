"""P1-09b — randomized retain/kill test: I1 and I9 hold on every snapshot a kill could leave behind.

The retention rule is one sentence — *a deleted range is always covered by a `GAP` committed before the unlink*
— and the only way to believe it is to keep killing the writer at random points and asking a reader what it
sees. So each step of a random schedule is followed by an optional kill: the tape directory is copied (the copy
is exactly the on-disk state at that instant, which is what a kill leaves), the copy is scanned, and two
invariants are checked against it:

  I1  every committed seq is either recovered or inside a *declared* hole. A hole that no `GAP` covers is a
      violation — it means a frame vanished without a tombstone.
  I9  the declared holes are ordered, disjoint and inside the committed range, and the chain still verifies
      across them: the recovered `CHAIN` frames link end to end, and every `GAP`'s `prev` is a value that walk
      passes through. A retention hole is therefore part of the chain, not a break in it.

A seeded record of what was run is written to `bench/results/p1-09b-retention.json` so the numbers in
`docs/progress.md` are reproducible rather than remembered.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from blackbox import constants as C
from blackbox.tape import retention, scanner, segments
from blackbox.tape.writer import TapeWriter

SEEDS = tuple(range(8))
STEPS = 60
SEGMENT_BYTES = C.MIN_SEGMENT_BYTES
BUDGETS = (0, SEGMENT_BYTES, 2 * SEGMENT_BYTES, 8 * SEGMENT_BYTES, None)
RESULT_PATH = Path(__file__).resolve().parents[2] / "bench" / "results" / "p1-09b-retention.json"


def _check_snapshot(data_dir: str, uuid_hex: str, where: str) -> dict:
    """I1 + I9 over one instant of the tape. Returns counters; raises AssertionError on a violation."""
    res = scanner.scan(data_dir, uuid_hex)
    counts = {"frames": res.frame_count, "declared": len(res.gaps_declared), "holes": len(res.gaps)}
    if res.head is None:
        return counts
    recovered = {f["seq"] for f in res.frames}
    declared = [(d["from"], d["to"]) for d in res.gaps_declared]

    # I1: nothing committed may be missing without a declared hole covering it.
    for seq in range(1, res.head.head_seq + 1):
        if seq in recovered:
            continue
        if any(lo <= seq <= hi for lo, hi in declared):
            continue
        raise AssertionError(f"{where}: I1 violated — seq {seq} vanished with no GAP "
                             f"(declared {declared}, head {res.head.head_seq})")
    for g in res.gaps:
        assert g["reason"] != "MISSING", f"{where}: I1 violated — undeclared hole {g}"

    # I9: holes are ordered, disjoint, inside the committed range, and the chain walks across them.
    last_hi = 0
    for lo, hi in sorted(declared):
        assert lo <= hi, f"{where}: I9 violated — inverted GAP range {(lo, hi)}"
        assert lo > last_hi, f"{where}: I9 violated — overlapping or unordered GAP {(lo, hi)}"
        assert hi <= res.head.head_seq, f"{where}: I9 violated — GAP extends past head_seq"
        last_hi = hi
    walk = scanner.verify_chain(res, os.path.join(data_dir, "tape", uuid_hex))
    assert walk["ok"], f"{where}: I9 violated — {walk['problems'][:4]}"
    if declared:
        assert walk["declared_links"], f"{where}: the holes were not linked, so the walk proved nothing"
        assert all(link["linked"] for link in walk["declared_links"]), f"{where}: unlinked GAP"
    counts["chain_frames"] = walk["chain_frames"]
    return counts


def _kill_and_check(data_dir: str, uuid_hex: str, where: str) -> dict:
    """Copy the tape as a crash would leave it, then check the copy (never the live files)."""
    tmp = tempfile.mkdtemp(prefix="bbx-kill-")
    try:
        copy = str(Path(tmp) / "data")
        shutil.copytree(data_dir, copy)
        return _check_snapshot(copy, uuid_hex, where)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fill_until_sealed(w: TapeWriter, sealed: int, limit: int = 400) -> None:
    for _ in range(limit):
        if len(segments.list_segments(w.tape_dir)) > sealed:
            return
        w.append(C.KIND_RUN_START, b"z" * 4000)
        w.publish()


def _schedule(seed: int) -> list[str]:
    rng = random.Random(seed)      # noqa: S311 — a seeded PRNG is the point: the schedule must be replayable
    return [rng.choices(("append", "append", "append", "roll", "retain", "kill"),
                        weights=(50, 0, 0, 10, 15, 25))[0] for _ in range(STEPS)]


@pytest.mark.parametrize("seed", SEEDS)
def test_i1_and_i9_hold_under_random_retain_and_kill(seed: int, tmp_path: Path) -> None:
    rng = random.Random(seed ^ 0x5EED)      # noqa: S311 — seeded on purpose, see `_schedule`
    data_dir = str(tmp_path)
    w = TapeWriter(data_dir, segment_bytes=SEGMENT_BYTES)
    w.batch_bytes = 0                  # one publish per append: the kill can land between any two frames
    w.batch_ms = 0.0
    w.open()
    stats = {"seed": seed, "steps": 0, "kills": 0, "retains": 0, "deleted": 0, "declared_max": 0,
             "frames_max": 0}
    try:
        for step, op in enumerate(_schedule(seed)):
            stats["steps"] = step + 1
            if op == "append":
                w.append(C.KIND_RUN_START, b"y" * rng.choice((16, 512, 4000)))
                w.publish()
            elif op == "roll":
                w.roll()
            elif op == "retain":
                budget = rng.choice(BUDGETS)
                keep = rng.choice((None, None, 0.0, 24.0))
                out = retention.apply(w, max_bytes=budget, keep_hours=keep,
                                      now_ns=rng.choice((None,)) if keep is None else None)
                assert out["blocked"] == [], out["blocked"]
                stats["retains"] += 1
                stats["deleted"] += len(out["deleted"])
                active = w.head.active_segment
                assert active not in [d["segment"] for d in out["deleted"]], "F2: the active segment was pruned"
            else:
                counts = _kill_and_check(data_dir, w.writer_uuid.hex(), f"seed {seed} step {step}")
                stats["kills"] += 1
                stats["declared_max"] = max(stats["declared_max"], counts["declared"])
                stats["frames_max"] = max(stats["frames_max"], counts["frames"])
            if step % 7 == 0:
                _check_snapshot(data_dir, w.writer_uuid.hex(), f"seed {seed} live step {step}")
        final = _kill_and_check(data_dir, w.writer_uuid.hex(), f"seed {seed} final")
        stats["declared_max"] = max(stats["declared_max"], final["declared"])
        stats["frames_max"] = max(stats["frames_max"], final["frames"])
    finally:
        w.close()
    stats["violations"] = 0
    with open(RESULT_PATH, "a" if RESULT_PATH.exists() else "w", encoding="utf-8") as fh:
        fh.write(json.dumps(stats, sort_keys=True) + "\n")


def test_a_retention_hole_is_declared_not_silently_missing(tmp_path: Path) -> None:
    """The control case for I1: remove a segment behind retention's back and the scanner must say MISSING."""
    w = TapeWriter(str(tmp_path), segment_bytes=SEGMENT_BYTES)
    w.batch_bytes = 0
    w.batch_ms = 0.0
    w.open()
    try:
        _fill_until_sealed(w, 3)
        victim = sorted(segments.list_segments(w.tape_dir))[0]
        assert victim != w.head.active_segment
        os.unlink(segments.seg_path(w.tape_dir, victim))
        os.unlink(segments.seal_path(w.tape_dir, victim))
        res = scanner.scan(str(tmp_path), w.writer_uuid.hex())
        assert res.gaps, "a deleted segment with no GAP must show up as a hole"
        assert all(g["reason"] == "MISSING" for g in res.gaps), res.gaps
        assert res.gaps_declared == []
        with pytest.raises(AssertionError, match="I1 violated"):
            _check_snapshot(str(tmp_path), w.writer_uuid.hex(), "control")
    finally:
        w.close()
