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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.errors import bbx

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


# ── the record as it sits on a tape ─────────────────────────────────────────────
# A `LINK` frame's payload is `Link.as_dict()`. Reading one back is not the same problem as writing it: the writer is
# this module, the reader is whatever tape the user opened, and a tape is untrusted input like any other. So `parse`
# checks every field that the resolver's invariants depend on and refuses rather than guessing — an `inferred` link
# claiming confidence 1.0 is refused precisely because the whole class exists to say "nobody asserted this".
PAYLOAD_KEYS = ("class", "left_writer", "right_writer", "left_run", "right_run", "confidence", "evidence")


def parse(payload: Mapping[str, Any]) -> Link:
    """Read one `LINK` payload. Unknown *extra* keys are tolerated (the format is append-only); bad ones are not."""
    if not isinstance(payload, Mapping):
        raise bbx("BBX-E155", f"a LINK payload must be a map, not {type(payload).__name__}")
    missing = [k for k in PAYLOAD_KEYS if k not in payload]
    if missing:
        raise bbx("BBX-E155", f"a LINK payload is missing {', '.join(missing)}")
    klass = payload["class"]
    if klass not in CLASSES:
        raise bbx("BBX-E155", f"unknown link class {klass!r}; the three classes are {', '.join(CLASSES)}")
    left, right = payload["left_writer"], payload["right_writer"]
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        raise bbx("BBX-E155", "a LINK names two writers as non-empty strings")
    if left == right:
        raise bbx("BBX-E155", f"a LINK from {left} to itself is not a link")
    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise bbx("BBX-E155", f"confidence {confidence!r} is not a number")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise bbx("BBX-E155", f"confidence {confidence} is outside [0, 1]")
    if klass == INFERRED and confidence >= 1.0:
        # The one place where a number is a claim: an inference is never presented as a fact (§11.10).
        raise bbx("BBX-E155", "an inferred LINK cannot carry confidence 1.0 — nobody asserted it")
    evidence = payload["evidence"]
    if not isinstance(evidence, Mapping):
        raise bbx("BBX-E155", f"evidence must be a map, not {type(evidence).__name__}")
    for name, run in (("left_run", payload["left_run"]), ("right_run", payload["right_run"])):
        if run is not None and not isinstance(run, str):
            raise bbx("BBX-E155", f"{name} must be a string or null")
    return Link(klass=klass, left_writer=left, right_writer=right,
                left_run=payload["left_run"], right_run=payload["right_run"],
                confidence=confidence, evidence=dict(evidence))


def frames_for(records: Sequence[Link]) -> list[tuple[int, bytes]]:
    """The `(kind, payload)` pairs a writer appends for these links — the writer's shape, not the reader's."""
    return [(C.KIND_LINK, cbor.encode(record.as_dict())) for record in records]


def from_frames(frames: Iterable[Mapping[str, Any]]) -> list[Link]:
    """Every `LINK` on a scanned tape, in tape order. Other kinds are ignored; a malformed one is an error."""
    out: list[Link] = []
    for frame in frames:
        if frame.get("kind") != C.KIND_LINK:
            continue
        payload = frame.get("payload") or b""
        try:
            body = cbor.decode_exact(payload)
        except Exception as exc:
            raise bbx("BBX-E155", f"LINK frame at seq {frame.get('seq')}: {exc}") from None
        out.append(parse(body))
    return out
