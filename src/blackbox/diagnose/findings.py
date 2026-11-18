"""Findings: what the frames support, what they only suggest, and what would settle the difference.

Every finding carries a `verdict`, and the vocabulary is narrow on purpose:

* `observed` — a frame says so, and the refs are listed. `interrupted mid-stream` lives here: the tape has chunks and no
  terminal frame for the node, which is a fact about the tape.
* `likely` — the observed facts plus the documented behaviour of the seam make this the best explanation. It says so in
  `basis`, and `alternatives` names the others that fit the same frames. No bare "probably" without them.
* `cannot-tell` — the evidence is genuinely insufficient. This is a first-class outcome, not a failure to produce a
  finding: `unknown` is the honest answer to "did the tool run?" when no result frame exists.
* `possible` — reserved for a mechanism the frames are compatible with but do not point at.

Two rules the rules below obey, both learned from the failure modes this product exists to catch:

1. **An absent frame is not a negative fact.** No `TOOL_RESULT` never becomes "the tool did not run". The tool may have
   run and the process died before its hook wrote — the whole point of `unknown`.
2. **A proxy's bytes are not the application's read.** `capture_seam` proxy records what crossed the socket. Whether the
   application consumed it, and how much, is a different claim and is only made when app-side frames say so.

Confidence is `high`/`medium`/`low` with a reason, never a percentage: a number would look measured, and nothing here is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from blackbox import constants as C
from blackbox.diagnose.evidence import Evidence

HIGH, MEDIUM, LOW = "high", "medium", "low"
OBSERVED, LIKELY, POSSIBLE, CANNOT_TELL = "observed", "likely", "possible", "cannot-tell"


@dataclass
class Finding:
    id: str
    verdict: str
    confidence: str
    title: str
    basis: str
    evidence: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "verdict": self.verdict, "confidence": self.confidence, "title": self.title,
                "basis": self.basis, "evidence": list(self.evidence), "alternatives": list(self.alternatives),
                "checks": list(self.checks)}


def _model_calls(evidence: Evidence) -> list[Finding]:
    """One finding per model call that has chunks and no terminal frame — the `INTERRUPTED_MID_STREAM` shape."""
    out: list[Finding] = []
    by_node: dict[int, list[Any]] = {}
    for frame in evidence.frames:
        if frame.kind in (C.KIND_MODEL_REQUEST, C.KIND_MODEL_CHUNK, C.KIND_MODEL_RESPONSE, C.KIND_MODEL_ERROR):
            by_node.setdefault(frame.node_slot, []).append(frame)
    for slot in sorted(by_node):
        frames = by_node[slot]
        requests = [f for f in frames if f.kind == C.KIND_MODEL_REQUEST]
        chunks = [f for f in frames if f.kind == C.KIND_MODEL_CHUNK]
        terminal = [f for f in frames if f.kind in (C.KIND_MODEL_RESPONSE, C.KIND_MODEL_ERROR)]
        errors = [f for f in frames if f.kind == C.KIND_MODEL_ERROR]
        if errors:
            first = errors[0]
            kind = first.data.get("type") or first.data.get("error") or "an error frame"
            out.append(Finding(
                id=f"provider-error-{slot}", verdict=OBSERVED, confidence=HIGH,
                title=f"the provider returned an error at node {slot}",
                basis=(f"{first.ref()} is a MODEL_ERROR frame ({kind}); it is the terminal frame for this node, so the "
                       "tape is not missing an ending for it."),
                evidence=[first.ref()] + [r.ref() for r in requests[:1]],
                alternatives=[],
                checks=["compare the error's timing with the retry policy: a retry that succeeded appears as a "
                        "second MODEL_REQUEST under the same or a later node"]))
            continue
        if chunks and not terminal:
            seam = requests[0].data.get("capture_seam") if requests else None
            out.append(Finding(
                id=f"interrupted-stream-{slot}", verdict=LIKELY, confidence=MEDIUM,
                title=f"the response stream at node {slot} was cut after {len(chunks)} chunk(s)",
                basis=(
                    f"{len(chunks)} MODEL_CHUNK frame(s) and no MODEL_RESPONSE or MODEL_ERROR for node {slot}: the call "
                    "opened a stream and the tape has no ending for it. That is what an interrupted call looks like on "
                    "the tape; it does not, by itself, say which side stopped first."),
                evidence=[f.ref() for f in chunks[-3:]] + [frames[0].ref()],
                alternatives=[
                    "the child process was killed or crashed between the last chunk and the response frame",
                    "the provider closed the connection mid-stream and the client raised instead of recording an error",
                    "the proxy died or was rebound while the response was in flight",
                    "the client closed early (user abort, timeout) and never wrote a terminal frame",
                ],
                checks=[
                    "look for a NODE_CLOSE or EXCEPTION after the last chunk: an exception means the app saw the cut",
                    "re-run the same request with --no-proxy: if it completes, the seam in between is implicated",
                    "compare the last chunk's wall_ns with RUN_END or the process exit: a small gap favours a kill",
                ]))
            if seam and str(seam).lower() in ("proxy", "transport"):
                out.append(Finding(
                    id=f"consumption-unknown-{slot}", verdict=CANNOT_TELL, confidence=LOW,
                    title=f"whether the application read the bytes the proxy saw at node {slot} is not recorded",
                    basis=("the capture seam is the proxy: it records what crossed the socket. Application-side read "
                           "counts are not in this capsule, so 'the app got the bytes' would be an inference."),
                    evidence=[f"capture_seam={seam} on {frames[0].ref()}"],
                    alternatives=[],
                    checks=["re-run with the SDK seam (-m blackbox or the sitecustomize launcher) to get app-side counts"]))
        partial_terminal = [f for f in terminal if f.kind == C.KIND_MODEL_RESPONSE
                            and bool(f.data.get("partial"))]
        unverified = [f for f in terminal if f.kind == C.KIND_MODEL_RESPONSE
                      and f.data.get("ending") == "unverified"]
        if partial_terminal:
            # The ending exists and says it is not an ending (defect 66). Before this, a provider that hung up
            # mid-stream produced a MODEL_RESPONSE with `partial: false` and this report said "nothing was cut short"
            # about a call the application had been handed three chunks of.
            first = partial_terminal[0]
            reason = first.data.get("end_reason")
            out.append(Finding(
                id=f"stream-ended-short-{slot}", verdict=OBSERVED, confidence=HIGH,
                title=f"the response stream at node {slot} ended before the provider finished it",
                basis=(f"{first.ref()} is a MODEL_RESPONSE whose own `partial` flag is true"
                       + (f" — recorded reason: {reason}" if isinstance(reason, str) and reason else "")
                       + f". {len(chunks)} MODEL_CHUNK frame(s) reached the tape before the body ended, so the "
                       "application was handed a short response; whether it noticed is a separate question."),
                evidence=[first.ref()] + [f.ref() for f in chunks[-2:]],
                alternatives=[
                    "the provider closed the connection mid-stream (a timeout, a 5xx gateway, a dropped link)",
                    "a proxy or load balancer between the client and the provider ended the body early",
                    "the application stopped reading and closed the stream: the seam marks that case the same way, and "
                    "`end_reason` distinguishes it when it is recorded",
                ],
                checks=[
                    "compare the last chunk's wall_ns with the response frame's: an immediate end favours the other end "
                    "closing, a long pause favours a timeout",
                    "re-run the same request: a truncation that repeats points at the provider or the path to it",
                    "read the application's own output for that call: a short answer with no exception means the cut was "
                    "silent to the app, which is the case this recorder exists to make visible",
                ]))
        if unverified:
            # The owner's rule: never claim a stream is complete when its ending is unknown, and never claim it was cut
            # either. This is the third answer, and it has to be visible or `partial: false` reads as "finished".
            first = unverified[0]
            provider = first.data.get("ending_provider") or "unrecognised"
            out.append(Finding(
                id=f"stream-ending-unknown-{slot}", verdict=CANNOT_TELL, confidence=LOW,
                title=f"whether the stream at node {slot} finished is not recorded",
                basis=(f"{first.ref()} is a MODEL_RESPONSE with `ending: unverified`: the path this call used follows a "
                       f"stream convention this build does not know (`{provider}`), so no terminator was expected or "
                       f"looked for. {len(chunks)} MODEL_CHUNK frame(s) reached the tape. The absence of a marker here "
                       "means the question was not asked, not that the stream was cut."),
                evidence=[first.ref()] + [f.ref() for f in chunks[-2:]],
                alternatives=[
                    "the stream finished normally and the provider has no terminator to observe",
                    "the stream was cut and no convention exists to prove it",
                    "the client closed the stream after reading what it needed",
                ],
                checks=[
                    "compare the response's chunk count and byte count with what the provider's own logs or dashboard "
                    "report for the same request",
                    "re-run the same call through a client whose stream convention this build knows, if one exists",
                    "read the application's own output for that call: a complete-looking answer is not evidence either "
                    "way, which is exactly why this is recorded as unknown",
                ]))
        if requests and not chunks and not terminal:
            out.append(Finding(
                id=f"no-first-byte-{slot}", verdict=LIKELY, confidence=MEDIUM,
                title=f"the call at node {slot} opened and no byte of a response was recorded",
                basis=(f"{requests[0].ref()} exists with no MODEL_CHUNK and no terminal frame. The request was issued; "
                       "whether the provider answered at all is not in the tape."),
                evidence=[requests[0].ref()],
                alternatives=["the provider did not answer before the process ended",
                              "the response arrived but recording stopped first (torn tail or a killed writer)"],
                checks=["check whether the capsule's truncated_tail / lost_from_seq lands in this window",
                        "check the provider's own status page for the same minute before blaming this code"]))
    return out


def _tool_calls(evidence: Evidence) -> list[Finding]:
    """An open tool node: the outcome is unknown, and the side effect may still have happened. Say exactly that."""
    out: list[Finding] = []
    for node in evidence.nodes:
        if node.tool_requests and node.tool_requests > node.tool_results:
            opens = [f for f in evidence.frames if f.kind == C.KIND_NODE_OPEN and f.node_slot == node.slot]
            name = (opens[-1].data.get("tool") or opens[-1].data.get("name")) if opens else "tool"
            out.append(Finding(
                id=f"tool-outcome-unknown-{node.slot}", verdict=CANNOT_TELL, confidence=LOW,
                title=f"the outcome of tool {name!r} at node {node.slot} is unknown",
                basis=(f"{node.tool_requests} tool request(s) and {node.tool_results} result(s) on this node. A missing "
                       "result frame is not a failed tool: the call may have completed after the recording stopped."),
                evidence=[f.ref() for f in opens[-2:]] + [f"node {node.slot}: {node.status} — {node.detail}"],
                alternatives=["the tool ran and its result was never recorded (process ended first)",
                              "the tool is still running or hung",
                              "the hook that writes the result failed independently"],
                checks=[f"check the tool's own side effects for {name!r} (a file, a row, an HTTP call) before re-running",
                        "re-running a non-idempotent tool that already ran is how a duplicate gets created"]))
    return out


def _exceptions(evidence: Evidence) -> list[Finding]:
    out = []
    for frame in (f for f in evidence.frames if f.kind == C.KIND_EXCEPTION):
        name = frame.data.get("type", "an exception")
        message = str(frame.data.get("message", ""))[:200]
        out.append(Finding(
            id=f"handled-exception-{frame.seq}", verdict=OBSERVED, confidence=HIGH,
            title=f"the application handled {name} at node {frame.node_slot}",
            basis=f"{frame.ref()} records an exception the application caught, so it continued past this point.",
            evidence=[frame.ref()],
            alternatives=[],
            checks=[f"read the handler for {name} in the application: the tape says it was handled, not that the "
                    "failure was fixed"]))
        if message:
            out[-1].checks.append("the recorded message is untrusted application text and is quoted in the appendix, "
                                  "not summarised as a cause")
    return out


def _run_boundary(evidence: Evidence) -> list[Finding]:
    if evidence.has_run_end:
        return []
    last = evidence.frames[-1] if evidence.frames else None
    if last is None:
        return [Finding(id="empty-capsule", verdict=CANNOT_TELL, confidence=LOW,
                        title="the capsule holds no frames",
                        basis="nothing was decoded, so every question below is unanswered rather than answered 'no'.",
                        evidence=[], alternatives=["the run never started",
                                                  "the capsule was written before any frame was flushed"],
                        checks=["run `blackbox recover` on the data dir: a capsule is derived, the tape is the record"])]
    return [Finding(
        id="no-run-end", verdict=LIKELY, confidence=MEDIUM,
        title="the process stopped without closing its run",
        basis=(f"the last recorded frame is {last.ref()} and there is no RUN_END. The run boundary is written by the "
               "process itself as it exits, so its absence means the process did not get that far — it does not say why."),
        evidence=[last.ref()],
        alternatives=["SIGKILL, OOM kill or container stop (no chance to run an exit path)",
                      "an unhandled exception in a thread that owns the writer",
                      "the writer itself failed and the process exited through a path that skipped the boundary"],
        checks=["`blackbox doctor` reports whether the writer's own final flush was attempted",
                "a `recover` record naming a torn tail points at a kill rather than a clean exit"])]


def _identifiers(evidence: Evidence) -> list[Finding]:
    """Two ids for one run is not a bug, and it is not nothing: a reader who greps the wrong one finds no capsule."""
    end = next((f for f in evidence.frames if f.kind == C.KIND_RUN_END), None)
    if not end:
        return []
    end_id = end.data.get("run_id")
    named = [(what, value) for what, value in (("RUN_START", evidence.run_id), ("RUN_END", end_id),
                                               ("manifest", evidence.capsule_run_id)) if value]
    distinct = {value for _, value in named}
    if len(distinct) < 2:
        return []
    return [Finding(
        id="several-run-ids", verdict=OBSERVED, confidence=HIGH,
        title=f"this run is named by {len(distinct)} different identifiers",
        basis=("; ".join(f"{what} says {value}" for what, value in named) +
               ". The public id is the one a user was shown at the time; the writer and manifest ids are what the "
               "record is filed under. A search for one of them finds nothing filed under the others, which is a "
               "practical problem when matching a report to a capsule — not evidence that two runs exist."),
        evidence=[f"seq {end.seq} RUN_END run_id={end_id}", f"manifest run_id={evidence.capsule_run_id}"],
        alternatives=["the id is assigned when the run is named, after RUN_START was already written",
                      "two runs were correlated into one capsule (check `links.json` before believing this)"],
        checks=["`blackbox view` labels the page with the RUN_START id; the capsule filename uses the manifest id",
                "if links.json declares a link, correlate first: this finding is about ids, not about provenance"])]


def _evidence_quality(evidence: Evidence) -> list[Finding]:
    out: list[Finding] = []
    if not evidence.root_ok:
        out.append(Finding(
            id="integrity-root", verdict=OBSERVED, confidence=HIGH,
            title="the capsule does not verify against its own integrity root",
            basis="the archive's entries do not hash to what its integrity file says they should.",
            evidence=[f"{len(evidence.problems)} problem(s) reported by the reader"],
            alternatives=["the file was modified after capture", "the file was damaged in copy or storage"],
            checks=["re-derive the capsule from the tape with `blackbox recover`; diagnose the tape, not the capsule"]))
    if evidence.truncated_tail:
        out.append(Finding(
            id="torn-tail", verdict=OBSERVED, confidence=HIGH,
            title="the recording ends in a torn tail",
            basis=(f"the writer declared a lost tail at seq {evidence.lost_from_seq}: frames after it exist only as "
                   "damage, so the record is incomplete by construction."),
            evidence=[f"lost_from_seq={evidence.lost_from_seq}"],
            alternatives=[],
            checks=["`blackbox verify` reports the first discontinuity, which is earlier than the tear"]))
    if evidence.bounded:
        out.append(Finding(
            id="bounded-read", verdict=CANNOT_TELL, confidence=HIGH,
            title="this report read only the first frames of a longer tape",
            basis=(f"{evidence.events_seen} frame(s) exist and only {len(evidence.frames)} were parsed, by the "
                   "--max-events bound."),
            evidence=[], alternatives=[],
            checks=["re-run with a higher --max-events to include the later frames in the timeline"]))
    return out


def findings(evidence: Evidence) -> list[Finding]:
    """All findings, ordered the way a reader triages: certain facts, likely causes, then what stays unknown."""
    everything = (_evidence_quality(evidence) + _run_boundary(evidence) + _exceptions(evidence)
                  + _identifiers(evidence) + _model_calls(evidence) + _tool_calls(evidence))
    rank = {OBSERVED: 0, LIKELY: 1, POSSIBLE: 2, CANNOT_TELL: 3}
    everything.sort(key=lambda f: (rank.get(f.verdict, 9), f.id))
    return everything
