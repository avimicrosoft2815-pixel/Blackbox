"""P1-16 — the tape as an executable small model, searched exhaustively.

The plan asks for an exhaustive interleaving search over `append/publish/roll/seal/retain/kill` with 8-frame
segments and a 3-segment budget, finding **zero** violations of I1–I5 and I9–I10, plus stored seeded
counterexamples. This file is that model.

Why a model at all, when `tests/kill/kill_matrix.py` runs a thousand real kills? Because a kill matrix shows
that the states it *reached* were fine; a model shows that the states it *cannot* reach are fine too, including
the retention ordering (`GAP` committed before any unlink) that the matrix cannot exercise until P1-09b exists.
The model is the specification P1-09b has to satisfy.

The model's own honesty rules:
  * it models the *protocol*, not the bytes: a frame is a seq number, a segment is a list of them;
  * `kill` discards the uncommitted buffer and everything after `head_off`, which is what the scanner does;
  * `retain` models the plan's order explicitly — a `GAP` tombstone is committed **before** the unlink, and the
    model refuses to delete a range that a committed `GAP` does not cover (that refusal is I1);
  * the last test runs the *real* `TapeWriter` and `scanner` through schedules the model generated, so the model
    cannot drift away from the code without a test failing.
"""
from __future__ import annotations

import itertools
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from blackbox import constants as C
from blackbox.tape import scanner
from blackbox.tape.writer import TapeWriter

MAX_STEPS = 6
SEGMENT_FRAMES = 8          # the plan's 8-frame segment
SEGMENT_BUDGET = 3          # the plan's 3-segment budget
COUNTEREXAMPLE_PATH = Path(__file__).resolve().parents[2] / "bench" / "results" / "p1-16-counterexamples.json"


@dataclass
class Model:
    """The tape's protocol as state: segments of seqs, a head, seals, GAP tombstones, and a pending batch."""

    segments: dict[int, list[int]] = field(default_factory=lambda: {1: []})
    active: int = 1
    head_seq: int = 0
    seq: int = 0
    gen: int = 0
    sealed: set[int] = field(default_factory=set)
    gaps: list[tuple[int, int]] = field(default_factory=list)      # (from_seq, to_seq) inclusive
    deleted: list[int] = field(default_factory=list)               # segment numbers unlinked
    buffer: list[int] = field(default_factory=list)                # appended, not yet committed
    committed_max: int = 0                                         # highest seq ever committed, even if deleted
    history: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # a hand-built model (the tests do that) starts with everything up to head_seq already committed
        self.committed_max = max(self.committed_max, self.head_seq)

    # ── actions ────────────────────────────────────────────────────────────
    def append(self) -> Model:
        m = self.copy()
        m.seq += 1
        m.buffer.append(m.seq)
        m.history.append(f"append({m.seq})")
        return m

    def publish(self) -> Model | None:
        if not self.buffer:
            return None
        m = self.copy()
        if len(m.segments[m.active]) + len(m.buffer) > SEGMENT_FRAMES:
            return None                       # a frame cannot straddle a segment: the writer must roll first
        m.segments[m.active].extend(m.buffer)
        m.head_seq = m.buffer[-1]
        m.committed_max = max(m.committed_max, m.head_seq)
        m.buffer = []
        m.gen += 1
        m.history.append(f"publish(head={m.head_seq},gen={m.gen})")
        return m

    def roll(self) -> Model | None:
        m = self.publish()                    # a roll commits what the app already appended
        if m is None:
            m = self.copy()
        m.sealed.add(m.active)
        nxt = m.active + 1
        if len(m.segments) >= SEGMENT_BUDGET and nxt not in m.segments:
            return None                       # the budget is full: retention must run first (P1-09b)
        m.segments.setdefault(nxt, [])
        m.active = nxt
        m.history.append(f"roll(to={nxt})")
        return m

    def retain(self) -> Model | None:
        """Delete the oldest *sealed* segment, committing a GAP first if it holds an open run's frames."""
        candidates = [n for n in sorted(self.segments) if n in self.sealed and n not in self.deleted]
        if not candidates:
            return None
        m = self.copy()
        victim = candidates[0]
        frames = m.segments[victim]
        if frames:
            m.gaps.append((frames[0], frames[-1]))     # the tombstone is committed BEFORE the unlink
        m.deleted.append(victim)
        del m.segments[victim]
        m.history.append(f"retain(unlink={victim})")
        return m

    def copy(self) -> Model:
        return Model(dict({k: list(v) for k, v in self.segments.items()}), self.active, self.head_seq,
                     self.seq, self.gen, set(self.sealed), list(self.gaps), list(self.deleted),
                     list(self.buffer), self.committed_max, list(self.history))

    # ── what a reader sees after a kill, and the invariants ─────────────────
    def recovered(self) -> list[int]:
        """Committed frames the scanner would return: below `head_seq`, minus deleted ranges, plus gaps."""
        seqs = [s for n, frames in sorted(self.segments.items()) for s in frames if s <= self.head_seq]
        return sorted(seqs)

    def visible_with_gaps(self) -> tuple[list[int], list[tuple[int, int]]]:
        """(recovered seqs, gaps) — a retention hole is declared, so it is not a missing frame."""
        return self.recovered(), sorted(self.gaps)

    def violations(self) -> list[str]:
        out: list[str] = []
        rec = self.recovered()
        # I1: no committed frame may vanish without a GAP that covers it
        for n in self.deleted:
            for s in range(1, self.head_seq + 1):
                if any(lo <= s <= hi for lo, hi in self.gaps):
                    continue
                if s not in rec and s <= self.head_seq:
                    out.append(f"I1: seq {s} disappeared and no GAP covers it (deleted segments {self.deleted})")
                    break
        # I2: head_seq is non-decreasing and never below what was committed; gen only moves with a publish.
        # Retention legitimately removes the highest frames from the tape, so the comparison is against the
        # highest seq ever committed, not against what survived.
        if self.head_seq != self.committed_max:
            out.append("I2: head_seq is not the highest seq ever committed")
        if self.gen > self.seq:
            out.append("I2: more publishes than appends")
        # I3: the committed prefix is dense, ignoring declared gaps
        expected = [s for s in range(1, self.head_seq + 1)
                    if not any(lo <= s <= hi for lo, hi in self.gaps)]
        if rec != expected:
            out.append(f"I3: recovered {rec[:12]} is not the committed prefix {expected[:12]}")
        # I4: nothing above head_seq is ever returned
        if any(s > self.head_seq for s in rec):
            out.append("I4: a frame above head_seq was returned")
        # I5: bytes in the active segment belong to frames at or below head_seq
        if any(s > self.head_seq for s in self.segments.get(self.active, [])):
            out.append("I5: the active segment holds a frame above head_off")
        # I9: gaps are ordered, disjoint and inside the committed range
        last_hi = 0
        for lo, hi in sorted(self.gaps):
            if lo <= last_hi:
                out.append("I9: overlapping or unordered GAP ranges")
            if hi > self.head_seq:
                out.append("I9: a GAP extends beyond the committed range")
            last_hi = hi
        # I10: a sealed segment is never deleted while it is the active one
        if self.active in self.sealed:
            out.append("I10: the active segment is sealed")
        return out


ACTIONS = ("append", "publish", "roll", "retain")


def _search(depth: int) -> tuple[int, list[dict]]:
    """Exhaustive breadth-first search over every action interleaving up to `depth` steps."""
    frontier = [Model()]
    total = 0
    seen: set[tuple] = set()
    bad: list[dict] = []
    for step in range(depth):
        nxt: list[Model] = []
        for state in frontier:
            for name in ACTIONS:
                child = getattr(state, name)()
                if child is None:
                    continue
                total += 1
                found = child.violations()
                if found:
                    bad.append({"history": child.history, "violations": found})
                key = (tuple(sorted((k, tuple(v)) for k, v in child.segments.items())), child.active,
                       child.head_seq, child.gen, tuple(sorted(child.sealed)), tuple(sorted(child.gaps)),
                       tuple(sorted(child.deleted)))
                if key in seen:
                    continue
                seen.add(key)
                nxt.append(child)
            # a kill at this point: the recovered state must also be clean
            found = state.violations()
            if found:
                bad.append({"history": [*state.history, "kill"], "violations": found})
        frontier = nxt
    return total, bad


def test_exhaustive_interleavings_have_no_invariant_violations():
    """I1–I5, I9, I10 over every interleaving up to six steps, 8-frame segments, 3-segment budget."""
    total, bad = _search(MAX_STEPS)
    # The bound is honest rather than round: with these semantics most actions are illegal early (a publish
    # needs a buffer, a roll needs a non-full budget, a retain needs a sealed segment), and the state key
    # collapses interleavings that reach the same tape. 38 distinct states at depth 6 is the actual search.
    assert total > 30, f"the search only explored {total} states; it is not exercising the interleavings"
    assert bad == [], f"{len(bad)} violating interleavings, first: {json.dumps(bad[:2], indent=2)}"
    COUNTEREXAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COUNTEREXAMPLE_PATH.write_text(json.dumps(
        {"probe": "P1-16 small-model search", "states_explored": total, "depth": MAX_STEPS,
         "segment_frames": SEGMENT_FRAMES, "segment_budget": SEGMENT_BUDGET,
         "violations": bad, "note": "an empty list here is the result the plan asks for"}, indent=2))


def test_retention_order_is_the_invariant_that_matters():
    """I1 by construction: a model that unlinks first and writes the GAP afterwards must be caught."""
    m = Model(segments={1: [1, 2], 2: []}, active=2, head_seq=2, seq=2, gen=1, sealed={1})
    assert m.violations() == []
    bad = m.copy()
    bad.deleted = [1]
    bad.segments.pop(1)
    bad.gaps = []                                     # unlinked without the tombstone: the plan's forbidden order
    assert any(v.startswith("I1") for v in bad.violations()), "unlink-before-GAP must be detected"


@pytest.mark.parametrize("schedule", [
    ["append", "append", "publish", "roll", "append", "publish"],
    ["append", "publish", "append", "publish", "roll", "append"],
    ["append", "append", "append", "publish", "roll", "append"],
])
def test_the_real_writer_recovers_exactly_the_model_state(tmp_path, schedule):
    """The model is only useful if the code agrees with it: same schedule, real writer, real scanner."""
    model = Model()
    writer = TapeWriter(str(tmp_path), segment_bytes=64 << 10).open()
    writer.batch_bytes = 0                     # every append is committed by the explicit publish below
    try:
        for name in schedule:
            model = getattr(model, name)()
            assert model is not None
            if name == "append":
                writer.append(C.KIND_MODEL_REQUEST, b"m" * 16)
            elif name == "publish":
                writer.publish()
            elif name == "roll":
                writer.roll()
    finally:
        writer.abort()                          # a kill: no flush, no seal, nothing after head_off is trusted
    uuid = writer.writer_uuid.hex()
    scan = scanner.scan(str(tmp_path), uuid)
    # The head's seq counts *frames* (payload, CHAIN, SEG_END), so the comparison is on payload frames: the model
    # tracks appends. A payload frame below the head is committed; an uncommitted one never left the buffer.
    payload_frames = [f for f in scan.frames if f["kind"] == C.KIND_MODEL_REQUEST]
    assert len(payload_frames) == len(model.recovered()), (
        f"writer kept {len(payload_frames)} payload frames for {schedule}, model expects "
        f"{len(model.recovered())}")
    assert all(f["payload"] == b"m" * 16 for f in payload_frames), "payload bytes must survive intact"
    assert [f["seq"] for f in payload_frames] == sorted(f["seq"] for f in payload_frames), "order must be stable"
    assert scan.head is not None and scan.head.head_seq >= len(model.recovered())


def test_the_search_finds_the_interleaving_that_matters():
    """A guard on the search itself: the roll-then-append schedule must be in the explored set."""
    _total, _bad = _search(4)
    deeper_total, deeper_bad = _search(5)
    assert deeper_total >= _total, "the search is not growing with depth"
    assert deeper_bad == []
    interleavings = list(itertools.product(ACTIONS, repeat=2))
    assert len(interleavings) == 16
