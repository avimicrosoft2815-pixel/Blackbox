"""P1-09b — retention: delete the oldest sealed segments, and declare the hole before it exists.

The acceptance criterion is "I1 and I9 hold under a randomized retain/kill test", which lives in
`tests/property/test_retention_random.py`. This file pins the individual rules that the randomized test relies on:

  * the active segment is never a victim, whatever the budget says (F2, decided here — see `retention.py`);
  * a `GAP{RETENTION}` frame is committed and published **before** the unlink, and carries the chain link
    (`prev`) that makes the hole a declared one rather than a break in the chain (F3);
  * a file that cannot be unlinked (a reader's open view, which on Windows is `WinError 32`) is reported as
    `blocked` with the attempts made, never silently treated as pruned;
  * a degraded writer deletes nothing at all.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blackbox import constants as C
from blackbox.tape import retention, scanner, segments
from blackbox.tape.writer import TapeWriter

SEG = C.MIN_SEGMENT_BYTES


def _writer(tmp_path: Path, **kw) -> TapeWriter:
    w = TapeWriter(str(tmp_path), segment_bytes=SEG, **kw)
    w.batch_bytes = 0          # publish on every append: one frame per commit, so a kill can land anywhere
    w.batch_ms = 0.0
    return w.open()


BODY = 4000      # one frame is ~4 KiB, so a 64 KiB segment seals after ~15 of them


def _fill_segments(w: TapeWriter, *, segments_to_fill: int = 3, limit: int = 400) -> list[int]:
    """Append until `segments_to_fill` *sealed* segments exist in front of the active one."""
    seqs: list[int] = []
    for _ in range(limit):
        if len(segments.list_segments(w.tape_dir)) > segments_to_fill:
            break
        seqs.append(w.append(C.KIND_RUN_START, b"x" * BODY))
        w.publish()
    assert seqs, "nothing was written"
    return seqs


def test_active_segment_is_never_a_candidate(tmp_path: Path) -> None:
    """F2: the budget can be blown to zero and the active segment still survives. Retention stops short."""
    w = _writer(tmp_path)
    try:
        _fill_segments(w, segments_to_fill=2)
        active = w.head.active_segment
        plan = retention.select(w.tape_dir, active_segment=active, max_bytes=0)
        assert active not in [v["segment"] for v in plan["victims"]]
        assert plan["under_budget"] is False, "a 0-byte budget cannot be met without the active segment"
        assert plan["victims"], "the sealed segments should still have been chosen"
    finally:
        w.close()


def test_gap_is_committed_and_published_before_the_unlink(tmp_path: Path, monkeypatch) -> None:
    """I1: at the instant of every unlink, a committed GAP already covers the range being deleted."""
    w = _writer(tmp_path)
    real = retention.unlink_with_retries
    seen: list[dict] = []

    def spy(path: str, **kw):
        victim_no = int(os.path.basename(path).split(".")[0].rsplit("-", 1)[-1])
        result = scanner.scan(str(tmp_path), w.writer_uuid.hex())
        covered = [(g["from"], g["to"], g["reason"]) for g in result.gaps_declared]
        seen.append({"victim": victim_no, "path": path, "declared": covered,
                     "head_covered": result.head is not None and all(
                         g["to"] <= result.head.head_seq for g in result.gaps_declared)})
        return real(path, **kw)

    monkeypatch.setattr(retention, "unlink_with_retries", spy)
    try:
        _fill_segments(w, segments_to_fill=3)
        out = retention.apply(w, max_bytes=0)
        assert out["deleted"], "nothing was pruned, so the ordering claim was never tested"
        assert out["blocked"] == []
        for entry in seen:
            assert entry["declared"], f"unlink of segment {entry['victim']} had no committed GAP"
            assert all(r != "MISSING" for _, _, r in entry["declared"])
            assert entry["head_covered"], "the GAP must be inside the committed range before the unlink"
    finally:
        w.close()


def test_declared_gap_survives_a_rescan_as_a_retention_hole(tmp_path: Path) -> None:
    """I9: after pruning, the scanner reports a declared hole with the chain link — not an unexplained break."""
    w = _writer(tmp_path)
    try:
        _fill_segments(w, segments_to_fill=3)
        out = retention.apply(w, max_bytes=0)
        assert out["deleted"]
        result = scanner.scan(str(tmp_path), w.writer_uuid.hex())
        assert result.gaps_declared, "the GAP frame should still be on the tape"
        for d in result.gaps_declared:
            assert d["reason"] == "RETENTION"
            assert isinstance(d["prev"], bytes) and len(d["prev"]) == 32
            assert d["to"] <= result.head.head_seq
        assert all(g["reason"] != "MISSING" for g in result.gaps), result.gaps
        assert result.completeness() == "gapped"
    finally:
        w.close()


def test_retention_by_age_only_takes_segments_older_than_the_horizon(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    try:
        _fill_segments(w, segments_to_fill=3)
        cands = retention.candidates(w.tape_dir, active_segment=w.head.active_segment)
        assert len(cands) >= 2
        newest = max(c["mtime_ns"] for c in cands)
        plan = retention.select(w.tape_dir, active_segment=w.head.active_segment,
                                keep_hours=1.0, now_ns=newest + 2 * 3600 * 10**9)
        assert [v["segment"] for v in plan["victims"]] == [c["segment"] for c in cands]
        # ... and a horizon in the far future keeps everything
        plan = retention.select(w.tape_dir, active_segment=w.head.active_segment,
                                keep_hours=1.0, now_ns=newest - 10**18)
        assert plan["victims"] == []
    finally:
        w.close()


def test_a_degraded_writer_deletes_nothing(tmp_path: Path) -> None:
    """Fail closed: no GAP can be committed, so no file may be removed."""
    w = _writer(tmp_path)
    try:
        _fill_segments(w, segments_to_fill=3)
        cands = retention.candidates(w.tape_dir, active_segment=w.head.active_segment)
        assert cands
        w.recording = False
        out = retention.apply(w, max_bytes=0)
        assert out["deleted"] == []
        assert "aborted" in out and "degraded" in out["aborted"]
        assert all(os.path.exists(c["path"]) for c in cands), "a degraded writer unlinked a segment"
    finally:
        w.close()


def test_sharing_violation_is_retried_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "victim"
    path.write_bytes(b"x")
    real_unlink = os.unlink
    calls = {"n": 0}

    def flaky(p):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "sharing violation")
        return real_unlink(p)

    monkeypatch.setattr(retention.os, "unlink", flaky)
    res = retention.unlink_with_retries(str(path), attempts=5, delay_s=0.0)
    assert res["ok"] is True
    assert res["attempts"] == 3
    assert not path.exists()


def test_a_file_that_stays_open_is_blocked_with_its_attempts(tmp_path: Path, monkeypatch) -> None:
    """C26: never a silent `None` — the file is still there and the result says so, with the attempts made."""
    path = tmp_path / "held"
    path.write_bytes(b"x")

    def always_busy(p):
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(retention.os, "unlink", always_busy)
    res = retention.unlink_with_retries(str(path), attempts=3, delay_s=0.0)
    assert res["ok"] is False
    assert res["attempts"] == 3
    assert "PermissionError" in res["error"]
    assert path.exists()

    w = _writer(tmp_path / "tape-root")
    try:
        _fill_segments(w, segments_to_fill=3)
        out = retention.apply(w, max_bytes=0)
        assert out["deleted"] == [], "a blocked unlink must not be counted as a pruned segment"
        assert out["blocked"], "the caller must learn which file is holding the range open"
        assert out["blocked"][0]["attempts"] == retention.SHARING_VIOLATION_RETRIES
    finally:
        w.close()


@pytest.mark.skipif(os.name != "nt", reason="POSIX permits unlinking a file another open fd still holds; "
                                            "the open-view case only exists on Windows (WinError 32)")
def test_an_open_view_blocks_the_unlink_only_on_windows(tmp_path: Path) -> None:
    path = tmp_path / "open"
    path.write_bytes(b"x")
    with open(path, "rb"):
        res = retention.unlink_with_retries(str(path), attempts=2, delay_s=0.0)
    assert res["ok"] is False
    assert res["attempts"] == 2


def test_gap_chain_link_matches_the_writer_chain(tmp_path: Path) -> None:
    """F3: the GAP's `prev` is a real position in the chain, so verify walks across the hole.

    Two retention passes, the second one after the tombstone segment sealed: the merged `GAP` must cover the
    whole pruned prefix, and the walk (anchored on each segment's header `prev_chain`) must pass through every
    declared hole's `prev`.
    """
    w = _writer(tmp_path)
    try:
        _fill_segments(w, segments_to_fill=3)
        first = retention.apply(w, max_bytes=0)
        assert first["deleted"]
        _fill_segments(w, segments_to_fill=2)          # the tombstone segment seals; a second pass follows
        second = retention.apply(w, max_bytes=0)
        result = scanner.scan(str(tmp_path), w.writer_uuid.hex())
        assert result.gaps_declared
        walk = scanner.verify_chain(result, w.tape_dir)
        assert walk["ok"], walk["problems"]
        assert walk["declared_links"] and all(link["linked"] for link in walk["declared_links"])
        span = (min(d["from"] for d in result.gaps_declared), max(d["to"] for d in result.gaps_declared))
        assert span[0] == 1, "the pruned prefix must be declared from the first frame"
        assert all(g["reason"] == "RETENTION" for g in result.gaps), result.gaps
        assert second["deleted"], "the second pass had nothing to do, so merging was not exercised"
    finally:
        w.close()


def test_a_one_byte_mutation_breaks_the_chain_walk(tmp_path: Path) -> None:
    """The negative control for I9: without it, `verify_chain` returning `ok` would mean nothing."""
    w = _writer(tmp_path)
    try:
        _fill_segments(w, segments_to_fill=2)
        assert scanner.verify_chain(scanner.scan(str(tmp_path), w.writer_uuid.hex()), w.tape_dir)["ok"]
    finally:
        w.close()
    sealed = sorted(segments.list_segments(w.tape_dir))[0]
    path = segments.seg_path(w.tape_dir, sealed)
    data = bytearray(Path(path).read_bytes())
    off = C.SEGMENT_HEADER_BYTES + 40          # inside the first frame's payload
    data[off] ^= 0x01
    Path(path).write_bytes(bytes(data))
    result = scanner.scan(str(tmp_path), w.writer_uuid.hex())
    walk = scanner.verify_chain(result, w.tape_dir)
    # The frame with the flipped byte fails its CRC, so it is not recovered: the chain then has a hole that no
    # GAP declares, and that is exactly what the walk has to complain about (or the segment stops early).
    assert walk["ok"] is False or result.gaps, (walk, result.gaps)


def test_segments_and_seals_are_unlinked_segment_first(tmp_path: Path, monkeypatch) -> None:
    """Ordering: the `.seg` goes before the `.seal`, so a reader never sees a seal whose segment is gone."""
    w = _writer(tmp_path)
    order: list[str] = []
    real = retention.unlink_with_retries

    def spy(path: str, **kw):
        order.append(os.path.basename(path).rsplit(".", 1)[-1])
        return real(path, **kw)

    monkeypatch.setattr(retention, "unlink_with_retries", spy)
    try:
        _fill_segments(w, segments_to_fill=3)
        retention.apply(w, max_bytes=0)
        assert order, "nothing was unlinked"
        for i in range(0, len(order) - 1, 2):
            assert order[i] == "seg", order
            assert order[i + 1] == "seal", order
    finally:
        w.close()


def test_dry_run_plans_without_touching_the_tape(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    try:
        _fill_segments(w, segments_to_fill=3)
        before = segments.list_segments(w.tape_dir)
        out = retention.apply(w, max_bytes=0, dry_run=True)
        assert out["deleted"] and all(d["dry_run"] for d in out["deleted"])
        assert segments.list_segments(w.tape_dir) == before
    finally:
        w.close()


def test_limits_from_env_reads_both_names_and_refuses_nonsense() -> None:
    assert retention.limits_from_env({}) == {"max_bytes": None, "keep_hours": None}
    assert retention.limits_from_env({retention.MAX_BYTES_ENV: "8388608",
                                      retention.KEEP_HOURS_ENV: "12.5"}) == {"max_bytes": 8388608,
                                                                              "keep_hours": 12.5}
    with pytest.raises(ValueError):
        retention.limits_from_env({retention.MAX_BYTES_ENV: "-1"})
    with pytest.raises(ValueError):
        retention.limits_from_env({retention.KEEP_HOURS_ENV: "-0.5"})
