"""Recovery state machine (P2-06) and interrupted-node classification (P2-07).

The machine is the plan's §14.2 diagram, one method per transition, and every transition is a file:

    DISCOVERED → CLAIMED → SCANNED → GRAPHED → COSTED → SEALED → INDEXED → ARCHIVED

After each step the machine writes `runs/<run_id>/recovery.done.<STEP>` containing
`{at_ns, input_hash, output_hash, artifact}`. On restart a step is **skipped** when the marker exists, its
`input_hash` still describes the inputs, and the artifact it names still hashes to `output_hash`. That is the whole
idempotence story: a kill at any step leaves either the previous step's marker (so the step runs again from a clean
input) or a complete artifact plus its marker (so the step is skipped). `P2-12` turns it into a test: ten
recoveries, one capsule hash.

Run identity is derived from the tape (`writer_uuid` + the meta page's immutable `created_wall_ns`), not from the
clock, so a resumed recovery works in the same directory as the one the killed process used.

Nothing here mutates a tape. The recovery reads segments, writes under `runs/` and `capsules/`, and the only file it
replaces is its own state.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from blackbox import constants as C
from blackbox.cap import export_jsonl, export_otlp
from blackbox.cap import format as cap
from blackbox.cap import manifest as manifest_mod
from blackbox.cap_sdk.sink import chunk_bytes
from blackbox.codec import cbor
from blackbox.codec.ids import blake2b
from blackbox.errors import bbx
from blackbox.platform import current as _plat_current
from blackbox.rec import correlate, discover
from blackbox.tape import chain as chain_mod
from blackbox.tape import frame as frame_mod
from blackbox.tape import meta as meta_mod
from blackbox.tape import scanner, segments

_plat = _plat_current()

STEPS = ("DISCOVERED", "CLAIMED", "SCANNED", "GRAPHED", "COSTED", "SEALED", "INDEXED", "ARCHIVED")
DIE_ENV = "BLACKBOX_DIE_AT"

# §14.4 — the five status strings, spelled exactly as the capsule prints them.
INTERRUPTED_BEFORE_FIRST_BYTE = "INTERRUPTED_BEFORE_FIRST_BYTE"
INTERRUPTED_MID_STREAM = "INTERRUPTED_MID_STREAM"
INTERRUPTED_AFTER_RESPONSE = "INTERRUPTED_AFTER_RESPONSE"
INTERRUPTED_IN_TOOL = "INTERRUPTED_IN_TOOL"
SEAL_INTERRUPTED = "SEAL_INTERRUPTED"


def _die(stage: str) -> None:
    """Crash hook for the tests: `BLACKBOX_DIE_AT=RECOVERY.<STEP>.<mode>` kills the process here."""
    if os.environ.get(DIE_ENV) == stage:
        os._exit(137)


def _hash(*parts: object) -> str:
    h = blake2b(b"")
    data = b"|".join(p if isinstance(p, bytes) else repr(p).encode() for p in parts)
    return blake2b(h + data).hex()


def _write_atomic(path: str, blob: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{time.monotonic_ns()}"
    fd = _plat.open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _plat.pwrite(fd, memoryview(blob), 0)
        _plat.fdatasync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):        # Windows has no directory fsync; its shim says so
        _plat.fsync_dir(directory)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as fh:
            loaded = json.loads(fh.read().decode("utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


# ── P2-07: interrupted-node classification (§14.4) ──────────────────────────
@dataclass
class NodeRecord:
    node_slot: int
    status: str
    detail: str
    model_requests: int = 0
    chunks: int = 0
    partial_stream_bytes: int = 0
    last_chunk_wall_ns: int = 0
    tool_requests: int = 0
    tool_results: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = {"node_slot": self.node_slot, "status": self.status, "detail": self.detail,
               "model_requests": self.model_requests, "tool_requests": self.tool_requests,
               "tool_results": self.tool_results}
        if self.status == INTERRUPTED_MID_STREAM:
            out.update({"chunks": self.chunks, "partial_stream_bytes": self.partial_stream_bytes,
                        "last_chunk_age_ms": 0})
        return out


def node_kind(payload: bytes) -> str | None:
    """The `kind` field of a node's payload (`run`, `step`, `tool`, `turn`), or None if it will not decode.

    Needed because the plan's §14.4 table has a `TOOL_REQUEST` row and **no seam writes that frame kind**: the hook
    adapters open a *tool node* instead (`hooks/events.py`), so a classifier that looked only at frame kinds reported a
    killed `PreToolUse` as `INTERRUPTED_BEFORE_FIRST_BYTE` — "never touched a model or a tool" — which asserts that a
    side effect did not happen. That is the one thing this row must never say (defect 44).
    """
    if not payload:
        return None
    try:
        decoded = cbor.decode_exact(payload)
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None
    kind = decoded.get("kind")
    return kind if isinstance(kind, str) else None


def classify_nodes(frames: list[dict[str, Any]], *, now_wall_ns: int | None = None) -> list[NodeRecord]:
    """Apply the §14.4 table to the frames of one run.

    A node is open when it has no `NODE_CLOSE`; the classification then asks what the last thing we saw was. The
    `TOOL_REQUEST` row is the one that matters most: it says *the side effect may have happened*, and this function
    never claims otherwise.
    """
    now = time.time_ns() if now_wall_ns is None else now_wall_ns
    nodes: dict[int, NodeRecord] = {}
    closed: set[int] = set()

    def rec(slot: int) -> NodeRecord:
        if slot not in nodes:
            nodes[slot] = NodeRecord(node_slot=slot, status=INTERRUPTED_BEFORE_FIRST_BYTE, detail="")
        return nodes[slot]

    last_kind: dict[int, int] = {}
    for f in frames:
        slot = int(f.get("node_slot") or 0)
        kind = f["kind"]
        if kind == C.KIND_NODE_CLOSE:
            closed.add(slot)
            if node_kind(f.get("payload") or b"") == "tool":
                rec(slot).tool_results += 1
            continue
        if kind == C.KIND_NODE_OPEN:
            r = rec(slot)
            if node_kind(f.get("payload") or b"") == "tool":
                # Counted as a tool invocation, because that is what it is: the node is open until its result closes
                # it, and a tool node that never closes may have run.
                r.tool_requests += 1
                last_kind[slot] = C.KIND_TOOL_REQUEST
            else:
                last_kind[slot] = kind
            continue
        if kind == C.KIND_MODEL_REQUEST:
            r = rec(slot)
            r.model_requests += 1
            last_kind[slot] = kind
        elif kind == C.KIND_MODEL_CHUNK:
            r = rec(slot)
            r.chunks += 1
            payload = f.get("payload") or b""
            r.partial_stream_bytes += chunk_bytes(payload)
            r.last_chunk_wall_ns = int(f.get("wall_ns") or 0)
            last_kind[slot] = kind
        elif kind in (C.KIND_MODEL_RESPONSE, C.KIND_MODEL_ERROR):
            rec(slot)
            last_kind[slot] = kind
        elif kind == C.KIND_TOOL_REQUEST:
            r = rec(slot)
            r.tool_requests += 1
            last_kind[slot] = kind
        elif kind == C.KIND_TOOL_RESULT:
            r = rec(slot)
            r.tool_results += 1
            last_kind[slot] = kind

    # Slot 0 is the run itself, not a node (`node_count` says so in as many words). A model call made outside any
    # explicit node lands there, and it used to be reported as an interrupted node wherever it appeared — so a run that
    # ended normally, with its response fully recorded, was described as "the crash happened in the app's handling".
    # A clean `RUN_END` is the launcher's word that the child exited: the call is then complete, and there is nothing
    # interrupted to report. With no `RUN_END` the slot-0 call is the only evidence there is, and it stays.
    run_ended = any(f["kind"] == C.KIND_RUN_END for f in frames)
    out: list[NodeRecord] = []
    for slot, r in sorted(nodes.items()):
        if slot in closed or (slot == 0 and run_ended):
            continue
        seen = last_kind.get(slot)
        if seen == C.KIND_TOOL_REQUEST and r.tool_requests > 0:
            r.status = INTERRUPTED_IN_TOOL
            r.detail = "tool invoked; outcome unknown — the side effect may have happened"
        elif seen == C.KIND_MODEL_CHUNK:
            r.status = INTERRUPTED_MID_STREAM
            age = max(0, (now - r.last_chunk_wall_ns) // 1_000_000) if r.last_chunk_wall_ns else 0
            r.detail = (f"received {r.chunks} chunk(s), {r.partial_stream_bytes} B of an unknown total, "
                        f"last chunk {age} ms before death")
        elif seen in (C.KIND_MODEL_RESPONSE,):
            r.status = INTERRUPTED_AFTER_RESPONSE
            r.detail = "response fully recorded; the crash happened in the app's handling"
        elif seen == C.KIND_MODEL_ERROR:
            r.status = INTERRUPTED_AFTER_RESPONSE
            r.detail = "the model call ended in an error frame; the node was never closed"
        elif r.model_requests:
            r.status = INTERRUPTED_BEFORE_FIRST_BYTE
            r.detail = "model call started, provider never responded before the process died"
        else:
            r.status = INTERRUPTED_BEFORE_FIRST_BYTE
            r.detail = "node opened and never touched a model or a tool"
        if r.status == INTERRUPTED_MID_STREAM:
            age = max(0, (now - r.last_chunk_wall_ns) // 1_000_000) if r.last_chunk_wall_ns else 0
            r.detail = (f"received {r.chunks} chunk(s), {r.partial_stream_bytes} B of an unknown total, "
                        f"last chunk {age} ms before death")
        out.append(r)
    return out


def _coverage(scan: Any) -> str:
    """`NONE` when the tape holds no model call, `PARTIAL` when its tail was torn, `FULL` otherwise.

    A capsule with nothing in it is not a capsule with complete coverage. The distinction is the whole point of the
    field: `NONE` is what tells a user their app was not talking to the recorder (the common cause is a base-URL variable
    the launcher adopted instead of overwriting), and `diagnose` now says so in words (defect 65).
    """
    if manifest_mod.model_call_count(scan.frames) == 0:
        return "NONE"
    return "PARTIAL" if scan.truncated_tail else "FULL"


def _content_mode(scan: Any) -> str:
    """What the frames say the content mode was: one mode, `mixed` if they disagree, `unknown` if there are no frames."""
    modes = manifest_mod.content_modes_of(scan.frames)
    if not modes:
        return "unknown"
    return modes[0] if len(modes) == 1 else "mixed"


def node_count(frames: list[dict[str, Any]]) -> int:
    """How many nodes the run *had*, which is not how many are still open.

    `classify_nodes` returns only the **unclosed** nodes, because its job is the §14.4 question ("what was interrupted,
    and what may have happened"). The capsule's `counts.nodes` is the other question — the plan's example manifest says
    `"nodes": 44` for a run whose nodes all finished — and it was being filled from the interrupted list, so every
    clean run reported `nodes: 0` while its `graph.json` listed the nodes one directory away. Slot 0 is the run itself,
    not a node.
    """
    slots: set[int] = set()
    for f in frames:
        if f["kind"] == C.KIND_NODE_OPEN:
            slot = int(f.get("node_slot") or 0)
            if slot:
                slots.add(slot)
    return len(slots)


def graph_nodes(frames: list[dict[str, Any]], *, interrupted: list[NodeRecord] | None = None) -> list[dict[str, Any]]:
    """Every node the tape opened, with the status each one ended in — the list `graph.json` is supposed to hold.

    `classify_nodes` answers §14.4's question ("what was interrupted, and what may have happened") and therefore
    returns *only* unclosed nodes; using it for the graph made a clean run's `graph.json` list no nodes at all while the
    same tape's manifest said `nodes: 1` (defect 54). A node that closed carries the outcome its `NODE_CLOSE` recorded;
    a node that did not carries the interrupted classification that `classify_nodes` produced for it.
    """
    by_slot = {record.node_slot: record for record in (interrupted or [])}
    nodes: dict[int, dict[str, Any]] = {}
    for f in frames:
        slot = int(f.get("node_slot") or 0)
        kind = f["kind"]
        if kind == C.KIND_NODE_OPEN:
            if not slot:
                continue                              # slot 0 is the run itself, not a node
            payload = _payload(f.get("payload") or b"")
            nodes[slot] = {"node_slot": slot, "name": payload.get("name"), "kind": payload.get("kind"),
                           "parent": payload.get("parent"), "opened_ns": payload.get("opened_ns"),
                           "status": "unclosed", "outcome": None, "detail": ""}
        elif kind == C.KIND_NODE_CLOSE:
            node = nodes.get(slot)
            payload = _payload(f.get("payload") or b"")
            if node is None:
                # A close with no open in this scan: the segment holding the open was retained away. Say so instead of
                # inventing a status, and keep the slot because a reader can still see *a* node existed.
                node = nodes[slot] = {"node_slot": slot, "name": payload.get("name"), "kind": payload.get("kind"),
                                      "parent": None, "opened_ns": None, "status": "closed", "outcome": None,
                                      "detail": "NODE_OPEN is not in this scan"}
            node["status"] = "closed"
            node["outcome"] = payload.get("outcome")
            node["duration_us"] = payload.get("duration_us")
    for slot, node in nodes.items():
        record = by_slot.get(slot)
        if record is not None and node["status"] != "closed":
            node["status"] = record.status
            node["detail"] = record.detail
    return [nodes[slot] for slot in sorted(nodes)]


#: Why no edge in this build names a node. Kept as one constant so the graph, the tests and D-052 cannot drift apart.
NODE_JOIN_REASON = ("no per-call identifier is recorded on both sides: `node_slot` numbers nodes inside one tape and "
                    "the injected header carries only <run>/<node>, so two calls from one node are indistinguishable")


def trim_padding(raw: bytes) -> bytes:
    """Drop the preallocated tail of a segment; keep everything else, damage included (P4-21).

    A tape segment is preallocated — 16 MiB by default — and only the first frames are real. Shipping the whole file puts
    16 MiB into every capsule for a five-frame run, which is not a recording, it is a copy of a hole. The trim is safe
    because every capsule reader already stops at the first bytes it cannot parse, so the trimmed entry yields the same
    frames.

    Two steps, and the distinction matters. First the **all-zero** tail goes, which is padding. Then the walk back to the
    last complete frame boundary, because a frame's own payload can end in zero bytes and a naive zero-trim would cut
    into it. A **non-zero** tail is either a partially written frame or damage, and both are evidence: if anything other
    than zeros sits past the last good frame, the entry keeps that region (with the padding still dropped) and
    `completeness`/`truncated_tail` in the manifest say what the scan made of it. A trimmer that dropped "whatever does
    not parse" would quietly delete the wreckage a reader is looking for.
    """
    end = len(raw)
    while end > C.SEGMENT_HEADER_BYTES and raw[end - 1] == 0:
        end -= 1
    if end >= len(raw):
        return raw
    off = last = C.SEGMENT_HEADER_BYTES
    while off < len(raw):
        try:
            _fields, nxt = frame_mod.decode(raw, off)
        except Exception:  # a bad frame is evidence, not an error to raise
            break
        if nxt <= off:
            break
        last = off = nxt
    if last <= C.SEGMENT_HEADER_BYTES:
        return raw[:end]
    if last >= end:
        return raw[:last]
    if raw[last:end].strip(b"\x00"):
        return raw[:end]
    return raw[:last]


def graph_edges(links_document: bytes, writer_uuid: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """`graph.json`'s `edges`, derived from the exact `links.json` bytes the capsule is about to carry (D-052).

    Two kinds of record go in. A `LINK` joins two *tapes* (one run, or one pair of tapes) and names no node. A
    `call_link` joins two records of *one call*, matched by the id both sides wrote (D-053), so it names the call and,
    where that tape recorded the call inside a node, that node's slot. A node-to-node edge appears only when both ends
    recorded the call inside a node — an `exact` tape link never produces one, and neither does an ambiguous id, which
    is written down with its candidate count instead of being resolved by picking the first pair.
    """
    if not links_document.strip():
        records: list[dict[str, Any]] = []
        unreadable = False
    else:
        try:
            parsed = json.loads(links_document.decode("utf-8"))
            raw_records = parsed.get("links") if isinstance(parsed, dict) else None
            records = [r for r in raw_records if isinstance(r, dict)] if isinstance(raw_records, list) else []
            unreadable = not isinstance(parsed, dict)
        except Exception:
            records, unreadable = [], True
    call_records: list[dict[str, Any]] = []
    if links_document.strip():
        try:
            parsed_for_calls = json.loads(links_document.decode("utf-8"))
            raw_calls = parsed_for_calls.get("call_links") if isinstance(parsed_for_calls, dict) else None
            call_records = [r for r in raw_calls if isinstance(r, dict)] if isinstance(raw_calls, list) else []
        except Exception:
            call_records = []
    edges: list[dict[str, Any]] = []
    for record in records:
        left = str(record.get("left_writer") or "")
        right = str(record.get("right_writer") or "")
        left_run = record.get("left_run")
        right_run = record.get("right_run")
        mine = left == writer_uuid and right != writer_uuid
        my_run, their_run = (left_run, right_run) if mine else (right_run, left_run)
        edges.append({
            "class": record.get("class"),
            # "run" when both ends name a run (the link says they are one run); "tape" when one of them does not.
            "level": "run" if (my_run and their_run) else "tape",
            "from": {"writer": left if mine else right, "run_id": my_run, "node": None},
            "to": {"writer": right if mine else left, "run_id": their_run, "node": None},
            "nodes": {"from": None, "to": None},
            "node_join": "unknown",
            "node_join_reason": NODE_JOIN_REASON,
            "confidence": record.get("confidence"),
            "evidence": dict(record.get("evidence") or {}),
        })
    for record in call_records:
        left = str(record.get("left_writer") or "")
        right = str(record.get("right_writer") or "")
        mine = left == writer_uuid and right != writer_uuid
        left_node = record.get("left_node")
        right_node = record.get("right_node")
        ambiguous = bool(record.get("ambiguous"))
        my_node, their_node = (left_node, right_node) if mine else (right_node, left_node)
        level = "call" if ambiguous or not (my_node and their_node) else "node"
        edges.append({
            "class": "exact",
            "level": level,
            "from": {"writer": left if mine else right, "run_id": record.get("left_run") if mine else record.get("right_run"),
                     "call_id": record.get("call_id"), "node": None if ambiguous else my_node},
            "to": {"writer": right if mine else left, "run_id": record.get("right_run") if mine else record.get("left_run"),
                   "call_id": record.get("call_id"), "node": None if ambiguous else their_node},
            "nodes": {"from": None if ambiguous else my_node, "to": None if ambiguous else their_node},
            "node_join": "ambiguous" if ambiguous else "exact",
            # the number of candidate pairings, because "ambiguous" without a count is a shrug
            "candidates": int(record.get("candidates") or 1),
            "confidence": 1.0,
            "evidence": dict(record.get("evidence") or {"rule": "same call id"}),
        })
    named = [e for e in edges if (e["nodes"]["from"] or e["nodes"]["to"])]
    node_level = sum(1 for e in edges if e["level"] == "node")
    node_join = "exact" if node_level else ("partial" if named else "unknown")
    reason = NODE_JOIN_REASON if not named else (
        "the call is inside a node on one tape only: the proxy vantage records calls, not the app's nodes")
    summary: dict[str, Any] = {
        "total": len(edges),
        "run_level": sum(1 for e in edges if e["level"] == "run"),
        "tape_level": sum(1 for e in edges if e["level"] == "tape"),
        "call_level": sum(1 for e in edges if e["level"] == "call"),
        "node_level": node_level,
        "ambiguous": sum(1 for e in edges if e["node_join"] == "ambiguous"),
        "node_join": node_join, "node_join_reason": reason}
    if unreadable:
        summary["links_unreadable"] = True
    return edges, summary


def empty_edge_summary() -> dict[str, Any]:
    """The summary a graph carries before seal: no links have been resolved yet, and it says why the count is zero."""
    return {"total": 0, "run_level": 0, "tape_level": 0, "call_level": 0, "node_level": 0, "ambiguous": 0,
            "node_join": "unknown", "node_join_reason": NODE_JOIN_REASON}


def _payload(raw: bytes) -> dict[str, Any]:
    """A frame payload as a dict, or an empty dict when it will not decode: a graph entry is not worth an exception."""
    if not raw:
        return {}
    try:
        decoded = cbor.decode_exact(raw)
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def run_status(frames: list[dict[str, Any]], *, sealed: bool) -> tuple[str, str | None]:
    """`(status, status_cause)` for the run itself: `SEAL_INTERRUPTED` when the run ended but was never sealed."""
    has_end = any(f["kind"] == C.KIND_RUN_END for f in frames)
    if has_end and not sealed:
        return SEAL_INTERRUPTED, "SEAL_NEVER_FINISHED"
    return ("recovered", "PROCESS_DEATH")


#: A run id is `r_` and lowercase alphanumerics. Both conventions in the tree match (`r_` + 16 hex from
#: `run_id_for`, `r_` + 26 base32 from `codec.ids.new_run_id`); a bare writer uuid, which an unattached SDK tape puts
#: in its `RUN_START`, does not. Accepting only what looks like a run id keeps the capsule inside the format rules.
_RUN_ID_RE = re.compile(r"^r_[0-9a-z]{8,40}$")


def _declared_run_id(tape_dir: str) -> str | None:
    """The run id the tape's own `RUN_START` names, if it names one that is well-formed. Never raises."""
    try:
        from blackbox.tape import reader

        for frame in reader.iter_frames(tape_dir, 1):
            if frame["kind"] != C.KIND_RUN_START:
                continue
            payload = cbor.decode_exact(frame["payload"])
            declared = payload.get("run_id") if isinstance(payload, dict) else None
            return declared if isinstance(declared, str) and _RUN_ID_RE.match(declared) else None
    except Exception:                                  # a damaged first segment is DISCOVERED's problem, not this one
        return None
    return None


# ── the state machine ───────────────────────────────────────────────────────
@dataclass
class RecoveryResult:
    run_id: str
    run_dir: str
    steps_run: list[str] = field(default_factory=list)
    steps_skipped: list[str] = field(default_factory=list)
    capsule_path: str | None = None
    capsule_root: str | None = None
    state: str = "DISCOVERED"
    node_statuses: list[dict[str, Any]] = field(default_factory=list)
    run_status: str = "recovered"
    status_cause: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "run_dir": self.run_dir, "steps_run": self.steps_run,
                "steps_skipped": self.steps_skipped, "capsule_path": self.capsule_path,
                "capsule_root": self.capsule_root, "state": self.state,
                "node_statuses": self.node_statuses, "run_status": self.run_status,
                "status_cause": self.status_cause}


class Recovery:
    """One recovery of one tape. Construct, then `run()`; every step is separately callable and resumable."""

    def __init__(self, data_dir: str, writer_uuid_hex: str, *, now_ns: int | None = None) -> None:
        self.data_dir = data_dir
        self.writer_uuid_hex = writer_uuid_hex
        self.tape_dir = os.path.join(data_dir, "tape", writer_uuid_hex)
        self.now_ns = time.time_ns() if now_ns is None else now_ns
        meta = self._read_meta()
        # Two identities, and conflating them was defect 47.
        #
        # `tape_key` is *this tape's* work-directory name, derived from the writer and the tape's creation time: stable
        # across recoveries, and — the part that matters — never shared, because everything in a run directory belongs
        # to one tape (`index.sqlite`, `scan.json`, `graph.json`, `cost.json`, the `recovery.done.*` markers). Two
        # tapes that pointed at one directory would overwrite each other's state, which is a worse bug than the one
        # this fixes.
        #
        # `run_id` is the run the tape says it belongs to: the `RUN_START` payload the tape itself wrote. A child tape
        # attached to a launcher's run declares the launcher's id, so the two capsules name one run and the link that
        # joins them says the same thing the manifests do. A tape that names no run (or names something that is not a
        # run id at all — an unattached SDK tape writes its writer uuid there) falls back to `tape_key`, which is
        # exactly what a standalone recording is.
        self.tape_key = "r_" + _hash(writer_uuid_hex, meta.get("created_wall_ns", 0))[:16]
        self.declared_run_id = _declared_run_id(self.tape_dir)
        self.run_id = self.declared_run_id or self.tape_key
        self.run_dir = os.path.join(data_dir, "runs", self.tape_key)
        # The capsule is per *tape*, not per run: two tapes of one run are two capsules (that is what the link is
        # for), so the id has to distinguish them or the second seal would replace the first.
        self.capsule_id = manifest_mod.capsule_id_for(self.run_id, writer_uuid_hex)
        self.capsule_path = os.path.join(data_dir, "capsules", f"{self.capsule_id}.bbx")
        self.result = RecoveryResult(run_id=self.run_id, run_dir=self.run_dir)
        self._scan: scanner.ScanResult | None = None

    # ── infrastructure ──────────────────────────────────────────────────────
    def _read_meta(self) -> dict[str, Any]:
        try:
            with open(os.path.join(self.tape_dir, "meta"), "rb") as fh:
                return meta_mod.decode(fh.read(C.META_BYTES))
        except (FileNotFoundError, Exception):        # a damaged meta is the DISCOVERED step's problem
            return {}

    def _marker_path(self, step: str) -> str:
        return os.path.join(self.run_dir, f"recovery.done.{step}")

    def _marker(self, step: str) -> dict[str, Any] | None:
        return _read_json(self._marker_path(step))

    def _save_marker(self, step: str, *, input_hash: str, output_hash: str, artifact: str | None) -> None:
        blob = json.dumps({"step": step, "at_ns": time.time_ns(), "input_hash": input_hash,
                           "output_hash": output_hash, "artifact": artifact}, sort_keys=True).encode() + b"\n"
        _write_atomic(self._marker_path(step), blob)

    def _artifact_hash(self, path: str | None) -> str | None:
        if not path:
            return None
        try:
            with open(path, "rb") as fh:
                return blake2b(fh.read()).hex()
        except FileNotFoundError:
            return None

    def _skip(self, step: str, input_hash: str) -> bool:
        """Skip when the marker exists, the inputs are unchanged **and** the artifact still verifies."""
        marker = self._marker(step)
        if not marker or marker.get("input_hash") != input_hash:
            return False
        artifact = marker.get("artifact")
        if artifact is None:
            return True
        if artifact == "capsule":
            return os.path.exists(self.capsule_path)
        return self._artifact_hash(artifact) == marker.get("output_hash")

    def _already_archived(self) -> bool:
        marker = self._marker("ARCHIVED")
        return bool(marker) and os.path.exists(self.capsule_path)

    def _enter(self, step: str) -> None:
        self._die(f"{step}.before")
        self.result.state = step

    def _die(self, where: str) -> None:
        _die(f"RECOVERY.{where}")

    # ── steps ───────────────────────────────────────────────────────────────
    def step_discovered(self) -> str:
        self._enter("DISCOVERED")
        entries = discover.discover(self.data_dir)
        mine = [e for e in entries if e.writer_uuid == self.writer_uuid_hex]
        if not mine:
            raise bbx("BBX-E220", f"{self.tape_dir} is not a tape discover can classify")
        entry = mine[0]
        payload = json.dumps(entry.to_dict(), sort_keys=True).encode()
        out = os.path.join(self.run_dir, "discovered.json")
        input_hash = _hash(self.writer_uuid_hex, payload)
        digest = blake2b(payload).hex()
        if self._skip("DISCOVERED", input_hash):
            self.result.steps_skipped.append("DISCOVERED")
            return digest
        _write_atomic(out, payload)
        self._die("DISCOVERED.mid")
        self._save_marker("DISCOVERED", input_hash=input_hash, output_hash=digest, artifact=out)
        self._die("DISCOVERED.after")
        return digest

    def step_claimed(self) -> str:
        self._enter("CLAIMED")
        lock_path = os.path.join(self.run_dir, "recovery.lock")
        content = json.dumps({"pid": os.getpid(), "start_ticks": _plat.proc_start_ticks(os.getpid()),
                              "boot_id": _plat.boot_id(), "at_ns": time.time_ns()},
                             sort_keys=True).encode() + b"\n"
        input_hash = _hash(self.writer_uuid_hex, self.run_id)
        held = _read_json(lock_path)
        # Resume-in-the-same-process may skip the claim; a *different* recoverer may not, even though the marker
        # it left behind says the step is done — the lock is the only thing that decides who owns the run.
        if (self._skip("CLAIMED", input_hash) and held is not None
                and held.get("pid") == os.getpid() and held.get("boot_id") == _plat.boot_id()):
            self.result.steps_skipped.append("CLAIMED")
            return blake2b(content).hex()
        took = self._take_lock(lock_path, content)
        if not took:
            raise bbx("BBX-E210", f"{lock_path} is held by a live recoverer")
        self._die("CLAIMED.mid")
        self._save_marker("CLAIMED", input_hash=input_hash, output_hash=blake2b(content).hex(), artifact=lock_path)
        self._die("CLAIMED.after")
        return blake2b(content).hex()

    def _take_lock(self, lock_path: str, content: bytes) -> bool:
        """`O_EXCL`, with the same staleness rules as the writer lease: dead pid, other boot, or another start time."""
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        try:
            fd = _plat.open_fd(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            held = _read_json(lock_path)
            if held and self._lock_is_live(held):
                return False
            os.unlink(lock_path)                      # stale: a dead recoverer left it behind
            try:
                fd = _plat.open_fd(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return False                          # lost the race to another recoverer, which is fine
        try:
            _plat.pwrite(fd, memoryview(content), 0)
            _plat.fdatasync(fd)
        finally:
            os.close(fd)
        return True

    def _lock_is_live(self, held: dict[str, Any]) -> bool:
        try:
            pid = int(held["pid"])
        except (KeyError, TypeError, ValueError):
            return False
        if held.get("boot_id") != _plat.boot_id():
            return False
        if not _plat.pid_alive(pid):
            return False
        try:
            return bool(_plat.proc_start_ticks(pid) == held.get("start_ticks"))
        except (OSError, NotImplementedError):
            return True                                # cannot tell ⇒ never steal

    def step_scanned(self) -> str:
        self._enter("SCANNED")
        scan = scanner.scan(self.data_dir, self.writer_uuid_hex)
        self._scan = scan
        summary = scan.summary()
        payload = json.dumps(summary, sort_keys=True, default=str).encode()
        out = os.path.join(self.run_dir, "scan.json")
        input_hash = _hash(self.writer_uuid_hex, tuple(scan.segments_seen))
        if self._skip("SCANNED", input_hash):
            self.result.steps_skipped.append("SCANNED")
            return blake2b(payload).hex()
        _write_atomic(out, payload)
        self._die("SCANNED.mid")
        self._save_marker("SCANNED", input_hash=input_hash, output_hash=blake2b(payload).hex(), artifact=out)
        self._die("SCANNED.after")
        return blake2b(payload).hex()

    def step_graphed(self) -> str:
        self._enter("GRAPHED")
        scan = self._scan or scanner.scan(self.data_dir, self.writer_uuid_hex)
        nodes = classify_nodes(scan.frames, now_wall_ns=self.now_ns)
        status, cause = run_status(scan.frames, sealed=bool(scan.sealed_segments) and not scan.truncated_tail)
        graph: dict[str, object] = {"writer_uuid": self.writer_uuid_hex, "run_id": self.run_id,
                 "frames": scan.frame_count, "gaps": scan.gaps,
                 "nodes": graph_nodes(scan.frames, interrupted=nodes),
                 "run_status": status, "status_cause": cause,
                 "open_nodes": sum(1 for n in nodes), "edges": [], "edge_summary": empty_edge_summary()}
        payload = json.dumps(graph, sort_keys=True).encode()
        out = os.path.join(self.run_dir, "graph.json")
        input_hash = _hash(self.writer_uuid_hex, scan.frame_count, scan.last_seq, len(scan.gaps))
        self.result.node_statuses = [n.to_dict() for n in nodes]
        self.result.run_status = status
        self.result.status_cause = cause
        if self._skip("GRAPHED", input_hash):
            self.result.steps_skipped.append("GRAPHED")
            return blake2b(payload).hex()
        _write_atomic(out, payload)
        self._die("GRAPHED.mid")
        self._save_marker("GRAPHED", input_hash=input_hash, output_hash=blake2b(payload).hex(), artifact=out)
        self._die("GRAPHED.after")
        return blake2b(payload).hex()

    def step_costed(self) -> str:
        self._enter("COSTED")
        # No price table ships with v1.0 and this build will not invent one: the step records that every call is
        # unpriced, which is a fact a reader can act on, instead of printing a confident zero.
        cost = {"price_table_version": None, "currency": "USD", "micro_usd": None,
                "unknown_cost": True, "reason": "no price table in this build (P3-11 adds one)"}
        payload = json.dumps(cost, sort_keys=True).encode()
        out = os.path.join(self.run_dir, "cost.json")
        input_hash = _hash(self.writer_uuid_hex, "cost")
        if self._skip("COSTED", input_hash):
            self.result.steps_skipped.append("COSTED")
            return blake2b(payload).hex()
        _write_atomic(out, payload)
        self._die("COSTED.mid")
        self._save_marker("COSTED", input_hash=input_hash, output_hash=blake2b(payload).hex(), artifact=out)
        self._die("COSTED.after")
        return blake2b(payload).hex()

    def _segments_entries(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for n in segments.list_segments(self.tape_dir):
            path = segments.seg_path_any(self.tape_dir, n)
            if path is None:
                continue
            out[cap.seg_entry_name(n)] = trim_padding(segments.logical_bytes(path))
        return out

    def step_sealed(self) -> str:
        self._enter("SEALED")
        entries = self._segments_entries()
        scan = self._scan or scanner.scan(self.data_dir, self.writer_uuid_hex)
        manifest = {
            "capsule_id": self.capsule_id,
            "created_ns": 0,                      # fixed: the capsule must be byte-identical across recoveries
            "tool": {"name": "blackbox", "version": "0.1.0.dev0", "git": ""},
            "run": {"run_id": self.run_id, "status": self.result.run_status,
                    "status_cause": self.result.status_cause,
                    "durability_class": "F1",
                    "completeness": scan.completeness(),
                    "truncated_tail": scan.truncated_tail,
                    "lost_from_seq": scan.lost_from_seq},
            # Both blocks are **derived from the frames**, not asserted (defect 65). They used to be constants:
            # `coverage` said FULL for a capsule that recorded no model call at all, and `content_mode` said `full` for
            # capsules whose model frames carried `content: hash` — so `blackbox diagnose` told a user the text was in
            # the capsule when only its hash was, and called an empty recording complete. A manifest that describes
            # itself wrongly is worse than a missing field: every reader trusts it.
            "capture": {"coverage": _coverage(scan), "providers": manifest_mod.provider_hosts_of(scan.frames),
                        "model_calls": manifest_mod.model_call_count(scan.frames)},
            "redaction": {"profile_id": "none", "content_mode": _content_mode(scan), "masks": 0, "failures": 0},
            "gaps": scan.gaps,
            "counts": {"events": scan.frame_count, "nodes": node_count(scan.frames),
                       "nodes_interrupted": len(self.result.node_statuses), "segments": len(entries)},
            "derived_parts": [],
        }
        chain: dict[str, object] = {"chain_root": None, "per_segment": {}}
        for f in scan.frames:
            if f["kind"] == C.KIND_CHAIN:
                try:
                    body = chain_mod.decode_payload(f["payload"])
                except Exception:
                    continue
                chain["chain_root"] = body["batch"].hex()
                per_segment = chain["per_segment"]
                assert isinstance(per_segment, dict)
                per_segment[str(f.get("segment"))] = body["prev"].hex()
        entries[cap.CHAIN_NAME] = json.dumps(chain, sort_keys=True).encode() + b"\n"
        # §11.10, the resolver at seal time: which other tapes of this data directory this run is linked to, and on what
        # evidence. Written into the capsule, never into the tape — the tape's bytes are the evidence this entry's
        # siblings hash, so a resolver that appended to it would be editing what it was describing (D-038).
        entries[cap.LINKS_NAME] = correlate.for_capsule(self.data_dir, self.writer_uuid_hex)
        # D-052: the graph's edges come from *these* bytes, in this step, so the graph and the capsule's `links.json`
        # cannot disagree. Written before the capsule, because the capsule is the thing that must not change afterwards.
        self._write_graph_edges(entries[cap.LINKS_NAME])
        # P4-16: the manifest carries what `blackbox verify` compares, and each field is one the capsule can recompute —
        # except the vantage, which lives on the tape's meta page and is therefore declared and reported as unverified.
        manifest["chain_root"] = chain["chain_root"]
        manifest["capture_vantage"] = scan.meta.get("vantage")
        manifest["capture_seam"] = manifest_mod.seams_of(scan.frames)
        manifest["links_summary"] = manifest_mod.links_summary_of(entries[cap.LINKS_NAME])
        # D-053, §13.1: the graph ships *inside* the capsule, and as a declared derived part — the bytes the user opens
        # are the bytes `cap.derived` can recompute from the segments, so an edited graph is a detected one (BBX-E412).
        # P4-13: the timeline as one JSON object per frame, derived from the same scan the graph is, so both are inside
        # the hashed container and both can be recomputed from it by `blackbox capsule verify`.
        derived: dict[str, bytes] = {}
        graph_bytes = self._graph_bytes()
        if graph_bytes is not None:
            derived[cap.GRAPH_NAME] = graph_bytes
        derived[cap.EVENTS_NAME] = export_jsonl.render(scan.frames)
        # P4-14: the same frames as OTLP/JSON, from this capsule's own identity (the trace is the run's, the span ids are
        # this capsule's, and neither is random) so `capsule verify` can re-render it byte for byte.
        derived[cap.OTLP_NAME] = export_otlp.render(scan.frames, capsule_id=self.capsule_id, run_id=self.run_id)
        # P4-20: the same spans as OTLP protobuf. Phoenix-class receivers speak protobuf and only protobuf (D-056's
        # measurement: 415 to the JSON), so the capsule ships the encoding a real importer takes, from the same model.
        derived[cap.OTLP_PB_NAME] = export_otlp.render_pb(scan.frames, capsule_id=self.capsule_id,
                                                         run_id=self.run_id)
        for part_name, part_bytes in derived.items():
            entries[part_name] = part_bytes
        manifest["derived_parts"] = [{"name": part_name, "sha256": hashlib.sha256(part_bytes).hexdigest(),
                                      "generator": "blackbox 0.1.0.dev0"}
                                     for part_name, part_bytes in sorted(derived.items())]
        input_hash = _hash(*(f"{k}:{blake2b(v).hex()}" for k, v in sorted(entries.items())))
        if self._skip("SEALED", input_hash):
            existing = cap.read_capsule(self.capsule_path)
            self.result.steps_skipped.append("SEALED")
            self.result.capsule_path = self.capsule_path
            self.result.capsule_root = existing.manifest["integrity"]["root"]
            return self.result.capsule_root
        out = cap.write_capsule(self.capsule_path, entries, manifest)
        self._die("SEALED.mid")
        self._save_marker("SEALED", input_hash=input_hash, output_hash=out["root"], artifact="capsule")
        self.result.capsule_path = self.capsule_path
        self.result.capsule_root = str(out["root"])
        self._die("SEALED.after")
        return self.result.capsule_root

    def _graph_bytes(self) -> bytes | None:
        """This run's `graph.json` as it sits on disk — the same bytes the capsule entry carries."""
        path = os.path.join(self.run_dir, "graph.json")
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def _write_graph_edges(self, links_document: bytes) -> None:
        """Add `edges`/`edge_summary` to this run's `graph.json`. Best-effort: a graph that cannot be read keeps `[]`."""
        path = os.path.join(self.run_dir, "graph.json")
        try:
            with open(path, encoding="utf-8") as fh:
                graph = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(graph, dict):
            return
        edges, summary = graph_edges(links_document, self.writer_uuid_hex)
        graph["edges"] = edges
        graph["edge_summary"] = summary
        _write_atomic(path, json.dumps(graph, sort_keys=True).encode("utf-8") + b"\n")

    def step_indexed(self) -> str:
        self._enter("INDEXED")
        import sqlite3

        scan = self._scan or scanner.scan(self.data_dir, self.writer_uuid_hex)
        path = os.path.join(self.run_dir, "index.sqlite")
        input_hash = _hash(self.writer_uuid_hex, self.capsule_id)
        if not self._skip("INDEXED", input_hash):
            con = sqlite3.connect(path)
            try:
                con.execute("CREATE TABLE IF NOT EXISTS event (seq INTEGER PRIMARY KEY, kind INTEGER NOT NULL, "
                            "wall_ns INTEGER NOT NULL, len INTEGER NOT NULL)")
                con.execute("CREATE TABLE IF NOT EXISTS run (run_id TEXT PRIMARY KEY, writer_uuid TEXT NOT NULL, "
                            "status TEXT NOT NULL, capsule_id TEXT, capsule_root TEXT)")
                con.executemany("INSERT OR IGNORE INTO event (seq, kind, wall_ns, len) VALUES (?,?,?,?)",
                                [(f["seq"], f["kind"], int(f["wall_ns"]), int(f["len"]))
                                 for f in scan.frames])
                con.execute("INSERT OR REPLACE INTO run VALUES (?,?,?,?,?)",
                            (self.run_id, self.writer_uuid_hex, self.result.run_status,
                             self.capsule_id, self.result.capsule_root))
                con.commit()
            finally:
                con.close()
        self._die("INDEXED.mid")
        digest = self._artifact_hash(path) or ""
        self._save_marker("INDEXED", input_hash=input_hash, output_hash=digest, artifact=path)
        self._die("INDEXED.after")
        return digest

    def step_archived(self) -> str:
        self._enter("ARCHIVED")
        record = {"run_id": self.run_id, "writer_uuid": self.writer_uuid_hex, "state": "ARCHIVED",
                  "capsule_id": self.capsule_id, "capsule_root": self.result.capsule_root,
                  "capsule_path": self.capsule_path, "run_status": self.result.run_status,
                  "node_statuses": self.result.node_statuses, "at_ns": time.time_ns()}
        payload = json.dumps(record, sort_keys=True).encode()
        out = os.path.join(self.run_dir, "recovery.json")
        _write_atomic(out, payload)
        _write_atomic(os.path.join(self.run_dir, "run.json"),
                      json.dumps({"run_id": self.run_id, "state": "ARCHIVED",
                                  "capsule_id": self.capsule_id,
                                  "capsule_root": self.result.capsule_root}, sort_keys=True).encode() + b"\n")
        input_hash = _hash(self.run_id, self.result.capsule_root or "")
        self._die("ARCHIVED.mid")
        self._save_marker("ARCHIVED", input_hash=input_hash, output_hash=blake2b(payload).hex(), artifact=out)
        self.result.state = "ARCHIVED"
        self._die("ARCHIVED.after")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(self.run_dir, "recovery.lock"))
        return blake2b(payload).hex()

    # ── the machine ─────────────────────────────────────────────────────────
    def run(self) -> RecoveryResult:
        if self._already_archived():
            # An archived run has nothing left to do. Saying so is the difference between "idempotent" and
            # "re-runs everything and happens to agree".
            self.result.state = "ARCHIVED"
            self.result.steps_skipped = list(STEPS)
            archived = _read_json(os.path.join(self.run_dir, "recovery.json")) or {}
            self.result.capsule_path = archived.get("capsule_path")
            self.result.capsule_root = archived.get("capsule_root")
            self.result.run_status = archived.get("run_status", "recovered")
            self.result.node_statuses = archived.get("node_statuses") or []
            return self.result
        for step, fn in (("DISCOVERED", self.step_discovered), ("CLAIMED", self.step_claimed),
                         ("SCANNED", self.step_scanned), ("GRAPHED", self.step_graphed),
                         ("COSTED", self.step_costed), ("SEALED", self.step_sealed),
                         ("INDEXED", self.step_indexed), ("ARCHIVED", self.step_archived)):
            before = len(self.result.steps_skipped)
            self._die(f"{step}.guard")
            fn()
            if len(self.result.steps_skipped) == before:
                self.result.steps_run.append(step)
        return self.result


def run_id_for(data_dir: str, writer_uuid_hex: str) -> str:
    """The tape's canonical run id: the same value every recovery of this tape derives (so ids stay stable).

    The launcher needs it *before* the child starts (`BLACKBOX_RUN_ID` is how hooks and the proxy link to the run),
    and deriving it here rather than in the launcher keeps one definition of "which run is this".
    """
    tape_dir = os.path.join(data_dir, "tape", writer_uuid_hex)
    try:
        with open(os.path.join(tape_dir, "meta"), "rb") as fh:
            meta = meta_mod.decode(fh.read(C.META_BYTES))
    except FileNotFoundError:
        meta = {}
    return "r_" + _hash(writer_uuid_hex, meta.get("created_wall_ns", 0))[:16]


def recover(data_dir: str, writer_uuid_hex: str | None = None, *, now_ns: int | None = None) -> RecoveryResult:
    """Convenience entry point: pick the newest unsealed tape and recover it."""
    if writer_uuid_hex is None:
        candidates = discover.discover(data_dir)
        if not candidates:
            raise bbx("BBX-E220", f"{data_dir} holds no tape to recover")
        newest = max(candidates, key=lambda e: (e.created_wall_ns, e.writer_uuid))
        writer_uuid_hex = newest.writer_uuid
    return Recovery(data_dir, writer_uuid_hex, now_ns=now_ns).run()
