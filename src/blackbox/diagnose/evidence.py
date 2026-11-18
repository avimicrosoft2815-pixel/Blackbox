"""What a capsule can actually tell you, and where it stops — read bounded, parsed defensively, labelled honestly.

The rule this module is written to, and the one the rest of the package depends on:

> **an absence of evidence is never evidence of absence.**

A tool call with no result frame does not mean the tool did not run; a proxy that saw 900 bytes does not mean the
application consumed them; a stream with no final response does not mean the provider failed. So `Evidence` carries
three separate things — what was **observed** (frames, with their `seq`), what was **not observed** (expected frames that
are missing, named), and what this capsule **cannot know** (no vantage recorded it, content was redacted, the tape was
torn). The findings are built on those three lists, never on an inference dressed up as the first one.

Boundaries that are enforced here rather than trusted:

* **the capsule is read as an archive, not executed.** `cap.read_capsule` verifies entry names, sizes and the integrity
  root; this module only decodes frames out of entry bytes, and a frame that will not decode ends the segment quietly
  (a torn tail is a fact, not an exception).
* **the read is bounded.** `--max-events` caps how many frames are kept in memory, and the capsule has a byte ceiling.
  A viewer for evidence must not be the thing that OOMs on a hostile 4 GiB capsule.
* **payloads are summarised, never carried.** `summary` is a short string; the raw bytes stay behind. Text that came
  from the recorded application is *untrusted* and is quoted, fenced and marked as such, never parsed for instructions.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.codec import cbor
from blackbox.errors import BlackBoxError
from blackbox.tape import frame as frame_mod

#: A capsule larger than this is refused rather than parsed: the viewer's job is to survive a hostile file.
MAX_CAPSULE_BYTES = 512 << 20
DEFAULT_MAX_EVENTS = 5_000
MAX_SUMMARY = 300
VANTAGE = {1: "sdk", 2: "proxy", 3: "hook", 4: "recovery"}


class EvidenceError(BlackBoxError):
    """Raised when there is nothing to diagnose — a missing file, an unreadable archive, a capsule that fails."""

    code = "BBX-E230"

    def __init__(self, detail: str = "") -> None:
        super().__init__(type(self).code, detail)


@dataclass
class Frame:
    """One frame, as much as a diagnosis needs and no more."""

    seq: int
    kind: int
    kind_name: str
    node_slot: int
    wall_ns: int
    payload_len: int
    summary: str
    data: dict[str, Any] = field(default_factory=dict)      # decoded CBOR, when the payload is a map

    def ref(self) -> str:
        return f"seq {self.seq} {self.kind_name}" + (f" (node {self.node_slot})" if self.node_slot else "")


@dataclass
class Node:
    """A node as recovery classifies it: only the *unclosed* ones come back, which is the point of the §14.4 table."""

    slot: int
    status: str
    detail: str
    model_requests: int = 0
    chunks: int = 0
    tool_requests: int = 0
    tool_results: int = 0


@dataclass
class Evidence:
    capsule_path: str
    capsule_id: str | None
    run_id: str | None                      # the run the tape names (what the user was shown)
    capsule_run_id: str | None              # the id derived from this writer; the capsule file's own name
    status: str | None
    status_cause: str | None
    completeness: str | None
    truncated_tail: bool
    lost_from_seq: int | None
    durability_class: str | None
    root_ok: bool
    problems: list[str]
    content_mode: str
    masks: int
    coverage: str
    capture_seams: list[str]
    gaps: list[dict[str, Any]]
    links: dict[str, Any]
    frames: list[Frame]
    nodes: list[Node]
    node_count: int
    events_seen: int
    bounded: bool
    observed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    cannot_know: list[str] = field(default_factory=list)

    # ── convenience queries used by the rules ────────────────────────────────────────────────────────────────────────
    def of_kind(self, *kinds: int) -> list[Frame]:
        return [f for f in self.frames if f.kind in kinds]

    @property
    def has_run_end(self) -> bool:
        return any(f.kind == C.KIND_RUN_END for f in self.frames)

    def node(self, slot: int) -> Node | None:
        return next((n for n in self.nodes if n.slot == slot), None)


def _summary(kind: int, data: dict[str, Any], payload_len: int) -> str:
    """A short, non-markup rendering of a payload. Values are quoted as data; keys are a closed vocabulary."""
    if not data:
        return f"{payload_len} B" if payload_len else ""
    # `text` is deliberately absent: recorded application text is never rendered inline, only quoted in the report's
    # fenced appendix. The marker says it exists, so a reader knows to look there.
    keep = ("name", "kind", "tool", "outcome", "type", "message", "provider", "model", "path", "stream", "partial",
            "capture_seam", "reason", "status", "error", "exit_code", "signaled", "hash", "run_id", "source")
    parts = []
    for key in keep:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bytes):
            value = value.hex()[:32]
        text = str(value).replace("\n", " ")[:120]
        parts.append(f"{key}={text}")
    if data.get("text"):
        parts.append("text=(quoted in the appendix, untrusted)")
    return "; ".join(parts)[:MAX_SUMMARY]


def _decode(blob: bytes) -> list[Frame]:
    """Every decodable frame in one logical segment; a torn tail ends the loop rather than raising."""
    out: list[Frame] = []
    offset = C.SEGMENT_HEADER_BYTES
    while offset < len(blob):
        try:
            fields, offset = frame_mod.decode(blob, offset)
        except Exception:
            break
        data: dict[str, Any] = {}
        raw_payload = fields.get("payload") or b""
        # JSON first, then CBOR — the order `cap/export_jsonl._payload_of` uses, and for the same reason: the model
        # frames (request, chunk, response, error) carry **JSON** payloads, everything else carries CBOR. Trying CBOR
        # only left `data` empty for exactly the frames a diagnosis is about, so the report's timeline showed
        # `MODEL_REQUEST  312 B` with no provider, path, model or `partial` flag, and every rule that reads those keys
        # (interrupted streams, provider errors, capture seams) was reading an empty dict (defect 67).
        try:
            decoded_json = json.loads(raw_payload.decode("utf-8")) if raw_payload.startswith(b"{") else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded_json = None
        if isinstance(decoded_json, dict):
            data = decoded_json
        else:
            try:
                decoded = cbor.decode_exact(raw_payload)
                data = decoded if isinstance(decoded, dict) else {}
            except Exception:
                data = {}
        out.append(Frame(seq=int(fields.get("seq") or 0), kind=fields["kind"],
                         kind_name=C.KIND_NAMES.get(fields["kind"], f"KIND_{fields['kind']}"),
                         node_slot=int(fields.get("node_slot") or 0), wall_ns=int(fields.get("wall_ns") or 0),
                         payload_len=len(fields.get("payload") or b""),
                         summary=_summary(fields["kind"], data, len(fields.get("payload") or b"")), data=data))
    return out


def _nodes(frames: list[Frame]) -> tuple[list[Node], int]:
    """The §14.4 classification, from the same function recovery uses — not a second opinion about it."""
    from blackbox.rec.recovery import classify_nodes, node_count

    raw = [{"seq": f.seq, "kind": f.kind, "kind_name": f.kind_name, "node_slot": f.node_slot,
            "wall_ns": f.wall_ns, "payload": b""} for f in frames]
    # `classify_nodes` reads `payload` for a few counters; the summary is enough for the ones this package reports, so
    # the payload is rebuilt from the decoded map rather than re-encoded from the frame's bytes.
    for entry, original in zip(raw, frames, strict=True):
        if original.data:
            entry["payload"] = cbor.encode(original.data)
    records = classify_nodes(raw)
    return ([Node(slot=r.node_slot, status=r.status, detail=r.detail, model_requests=r.model_requests,
                  chunks=r.chunks, tool_requests=r.tool_requests, tool_results=r.tool_results) for r in records],
            node_count(raw))


def load(path: str, *, max_events: int = DEFAULT_MAX_EVENTS) -> Evidence:
    """Read one capsule as evidence. Raises `EvidenceError` when there is nothing to read."""
    if not os.path.exists(path):
        raise EvidenceError(f"{path} does not exist")
    size = os.path.getsize(path)
    if size > MAX_CAPSULE_BYTES:
        raise EvidenceError(f"{path} is {size} B, over the {MAX_CAPSULE_BYTES} B ceiling this reader will parse")
    try:
        capsule = cap.read_capsule(path, max_bytes=MAX_CAPSULE_BYTES)
    except Exception as exc:
        raise EvidenceError(f"{os.path.basename(path)} could not be read as a capsule: {type(exc).__name__}: {exc}") from exc
    manifest = capsule.manifest
    frames: list[Frame] = []
    for name in sorted(n for n in capsule.entries if n.startswith("events/")):
        frames.extend(_decode(capsule.entry(name)))
    bounded = len(frames) > max_events
    kept = frames[:max_events]
    try:
        links = json.loads(capsule.entry(cap.LINKS_NAME).decode("utf-8"))
    except Exception:
        links = {"links": [], "summary": {"declared": 0, "exact": 0, "inferred": 0}}
    run = manifest.get("run") or {}
    start = next((f for f in kept if f.kind == C.KIND_RUN_START), None)
    nodes, node_count = _nodes(kept)
    seams = sorted({str(f.data.get("capture_seam")) for f in kept
                    if f.kind == C.KIND_MODEL_REQUEST and f.data.get("capture_seam")})
    evidence = Evidence(
        capsule_path=path, capsule_id=manifest.get("capsule_id"),
        run_id=(start.data.get("run_id") if start else None) or run.get("run_id"),
        capsule_run_id=run.get("run_id"), status=run.get("status"), status_cause=run.get("status_cause"),
        completeness=run.get("completeness"), truncated_tail=bool(run.get("truncated_tail")),
        lost_from_seq=run.get("lost_from_seq"), durability_class=run.get("durability_class"),
        root_ok=bool(capsule.root_ok), problems=list(capsule.problems),
        content_mode=str((manifest.get("redaction") or {}).get("content_mode") or "unknown"),
        masks=int((manifest.get("redaction") or {}).get("masks") or 0),
        coverage=str((manifest.get("capture") or {}).get("coverage") or "unknown"),
        capture_seams=seams, gaps=list(manifest.get("gaps") or []), links=links,
        frames=kept, nodes=nodes, node_count=node_count, events_seen=len(frames), bounded=bounded,
    )
    _label(evidence)
    return evidence


def _label(evidence: Evidence) -> None:
    """Fill the three lists — observed / missing / cannot know — before any rule looks at them."""
    e = evidence
    e.observed.append(f"{e.events_seen} frame(s) decoded from capsule {e.capsule_id or '?'}"
                      + (" (the read was bounded; older frames are not shown)" if e.bounded else ""))
    if e.run_id:
        e.observed.append(f"run id named by the tape: {e.run_id}")
    if e.has_run_end:
        end = next(f for f in e.frames if f.kind == C.KIND_RUN_END)
        e.observed.append(f"the run reached its end boundary ({end.ref()}"
                          + (f", exit_code={end.data.get('exit_code')}" if "exit_code" in end.data else "") + ")")
    else:
        e.missing.append("no RUN_END frame: the process stopped before it closed its own run boundary")
    if e.status:
        e.observed.append(f"manifest run.status is {e.status}"
                          + (f" ({e.status_cause})" if e.status_cause else ""))
    if e.status == "SEAL_INTERRUPTED":
        e.cannot_know.append(
            "run.status SEAL_INTERRUPTED / SEAL_NEVER_FINISHED is what every capsule from this build says: the capsule "
            "is written but never sealed. It describes the build, not this run — it is not a crash indicator.")
    if e.truncated_tail:
        e.observed.append(f"the tape's tail was torn; the record ends at or before seq {e.lost_from_seq}")
        e.cannot_know.append(f"frames after seq {e.lost_from_seq} were never recorded, so nothing can be said about them")
    for gap in e.gaps:
        e.observed.append(f"declared gap {gap.get('from')}..{gap.get('to')}: {gap.get('reason')}")
        e.cannot_know.append(f"events in gap {gap.get('from')}..{gap.get('to')} are missing by declaration, not by inference")
    if e.content_mode == "hash":
        e.cannot_know.append("content is stored as keyed or unkeyed hashes: prompt and response text cannot be recovered "
                             "from this capsule, and no claim here is about their wording")
    if e.content_mode == "off":
        e.cannot_know.append("content recording was off: only counts and names exist")
    if e.masks:
        e.observed.append(f"{e.masks} value(s) were masked by the redaction profile before the bytes reached the tape")
    if e.coverage == "NONE":
        # The most common real-user confusion this build can produce: the app set its own base URL, the launcher adopted
        # it rather than overwriting it, and the capsule is therefore empty of the calls the user wanted explained. The
        # report used to call that "completeness full" and say nothing (defect 65).
        e.observed.append("no model call was recorded in this capsule, so nothing here describes a request or a response")
        e.cannot_know.append(
            "why nothing was recorded: the likeliest causes are an application that sets its own base URL (the launcher "
            "adopts a base URL you set instead of overwriting it, so the call never reached the recorder) and a client "
            "the capture seams do not cover. `blackbox run --force-proxy` points every base-URL variable at the "
            "recorder and names each one it displaces")
    elif e.coverage and e.coverage != "FULL":
        e.cannot_know.append(f"capture coverage is {e.coverage}: some bytes were not observed at all")
    if e.capture_seams:
        e.observed.append("capture seam(s) that wrote these calls: " + ", ".join(e.capture_seams))
        if any(seam.endswith("C") or seam == "C" for seam in e.capture_seams):
            e.cannot_know.append("seam C records calls and chunks but not byte counts (partial=True by construction)")
    if not e.root_ok:
        e.observed.append("the capsule's own integrity root does not match its entries")
        e.cannot_know.append("bytes that fail their own checksum are not evidence; treat every claim below as suspect")
    for node in e.nodes:
        e.observed.append(f"node {node.slot} closed without a NODE_CLOSE: {node.status} — {node.detail}")
