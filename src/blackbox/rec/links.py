"""§11.10's `LINK` records: how a call written by one vantage point is tied to a run written by another.

Three vantage points write one tape format — the in-process SDK, the loopback proxy, the hook adapters — and the question
a capsule has to answer is *which model call belongs to which agent step*. There are exactly three honest ways to answer:

    exact      the same run id on both sides. The proxy read it from `x-blackbox-run`, which only the launcher's SDK ever
               sends, so the two writers are describing one run by construction.
    declared   the user said so: `blackbox link <a> <b>`, or `--run <id>` on a standalone proxy. Their claim, recorded as
               theirs, not laundered into "the system worked it out".
    inferred   neither of the above: a time window (±2 s by default) **and** a content fingerprint match, with the evidence
               kept and a confidence that is never 1.0.

**The rule under all three: an inference never merges runs.** A link is a record *about* two runs; it does not rename
them, it does not move a frame, and it does not change a status. A recorder that silently decides two tapes are one run is
a recorder whose totals cannot be checked, and this module has no API that could do it — `resolve()` returns links and
nothing else. `test_an_inference_never_merges_two_runs` asserts exactly that, because "the code has no such function" is a
weaker guarantee than "a test would fail if someone added one".
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

EXACT = "exact"
DECLARED = "declared"
INFERRED = "inferred"
CLASSES = (EXACT, DECLARED, INFERRED)

DEFAULT_WINDOW_NS = 2 * 1_000_000_000            # ±2 s, configurable per §11.10

# An inference is never presented as a fact. The ceiling is below 1.0 on purpose, and the floor keeps a weak-but-real
# match visible instead of rounding it out of existence; the viewer renders the number, not just the dashed edge.
MAX_INFERRED_CONFIDENCE = 0.9
MIN_INFERRED_CONFIDENCE = 0.5


@dataclass(frozen=True)
class CallView:
    """One call as a resolver sees it: which writer, which run (if it said), when, and what it was."""

    writer_uuid: str
    vantage: str
    wall_ns: int
    fingerprint: str
    run_id: str | None = None
    node_slot: int | None = None


@dataclass(frozen=True)
class RunView:
    """One tape's identity plus the calls it recorded. `run_id` is what the tape says it is, not what we hope."""

    writer_uuid: str
    vantage: str
    run_id: str | None
    calls: tuple[CallView, ...] = ()

    @classmethod
    def of(cls, writer_uuid: str, vantage: str, run_id: str | None,
           calls: Iterable[CallView] = ()) -> RunView:
        return cls(writer_uuid=writer_uuid, vantage=vantage, run_id=run_id, calls=tuple(calls))


@dataclass(frozen=True)
class Link:
    """One `LINK` record, in the shape the frame carries and a capsule lists under `links[]`."""

    klass: str
    left_writer: str
    right_writer: str
    left_run: str | None
    right_run: str | None
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"class": self.klass, "left_writer": self.left_writer, "right_writer": self.right_writer,
                "left_run": self.left_run, "right_run": self.right_run,
                "confidence": round(self.confidence, 3), "evidence": dict(self.evidence)}

    @property
    def merges_runs(self) -> bool:
        """Always False. It exists so that a test can assert the invariant instead of trusting a docstring."""
        return False


def declared(left: RunView, right: RunView, *, by: str = "user") -> Link:
    """A link the user asserted. Confidence 1.0 because it is a statement, not a measurement."""
    return Link(klass=DECLARED, left_writer=left.writer_uuid, right_writer=right.writer_uuid,
                left_run=left.run_id, right_run=right.run_id, confidence=1.0,
                evidence={"asserted_by": by, "reason": "the user stated that these tapes describe one run"})


def _confidence(delta_ns: int, window_ns: int) -> float:
    ratio = min(1.0, abs(delta_ns) / window_ns) if window_ns > 0 else 1.0
    return round(MAX_INFERRED_CONFIDENCE - (MAX_INFERRED_CONFIDENCE - MIN_INFERRED_CONFIDENCE) * ratio, 3)


def resolve(*, proxy: RunView, others: Sequence[RunView], window_ns: int = DEFAULT_WINDOW_NS,
            declared_pairs: Sequence[tuple[str, str]] = ()) -> list[Link]:
    """Every link the evidence supports between a proxy tape and the runs beside it.

    Order of authority: a declared pair wins, then an exact run id, and only then inference — and a pair that got an
    exact link never also gets an inferred one, because two records for one relation is how a graph grows a second edge
    that disagrees with the first.
    """
    links: list[Link] = []
    claimed: set[str] = set()

    for left_run, right_run in declared_pairs:
        for other in others:
            if other.run_id in (left_run, right_run) and other.writer_uuid != proxy.writer_uuid:
                links.append(declared(proxy, other))
                claimed.add(other.writer_uuid)

    for other in others:
        if other.writer_uuid in claimed or other.writer_uuid == proxy.writer_uuid:
            continue
        if proxy.run_id is not None and other.run_id == proxy.run_id:
            links.append(Link(klass=EXACT, left_writer=proxy.writer_uuid, right_writer=other.writer_uuid,
                              left_run=proxy.run_id, right_run=other.run_id, confidence=1.0,
                              evidence={"rule": "same run id", "run_id": proxy.run_id,
                                        "carried_by": "x-blackbox-run"}))
            claimed.add(other.writer_uuid)

    for other in others:
        if other.writer_uuid in claimed or other.writer_uuid == proxy.writer_uuid:
            continue
        best: tuple[int, CallView, CallView] | None = None
        for mine in proxy.calls:
            for theirs in other.calls:
                if mine.fingerprint != theirs.fingerprint:
                    continue
                delta = mine.wall_ns - theirs.wall_ns
                if abs(delta) > window_ns:
                    continue
                if best is None or abs(delta) < abs(best[0]):
                    best = (delta, mine, theirs)
        if best is None:
            continue
        delta, mine, theirs = best
        links.append(Link(klass=INFERRED, left_writer=proxy.writer_uuid, right_writer=other.writer_uuid,
                          left_run=proxy.run_id, right_run=other.run_id,
                          confidence=_confidence(delta, window_ns),
                          evidence={"rule": "time window and content fingerprint",
                                    "window_ns": window_ns, "delta_ns": delta,
                                    "fingerprint": mine.fingerprint,
                                    "proxy_call_wall_ns": mine.wall_ns, "other_call_wall_ns": theirs.wall_ns,
                                    "why_not_exact": "neither tape names the other's run id"}))
    return links


def summary(links: Sequence[Link]) -> dict[str, int]:
    """The capsule's `links_summary`: a count per class, with all three always present so zero is visible."""
    counts = {klass: 0 for klass in CLASSES}
    for link in links:
        counts[link.klass] = counts.get(link.klass, 0) + 1
    return counts
