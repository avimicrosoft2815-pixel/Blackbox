"""`otlp/spans.json` — the capsule as OTLP/JSON ResourceSpans (P4-14).

The whole point of a derived part is that it can be recomputed from the frames, and this one keeps that promise while
speaking a standard: nodes become agent spans, model calls become inference spans, tools become tool spans, and every
span carries the same `gen_ai.*`/`bbx.*` discipline as `events.jsonl` (D-054) — a pinned vocabulary, and our own names
where the vocabulary has none.

**The pin is the same tag as the JSONL export**, OpenTelemetry semantic conventions **v1.39.0**
(`6d05e92070f0a5c9a5bbcd396d2ebe2103377f8c`), which is the version that defines `invoke_agent` and `execute_tool` and
that renamed the provider attribute to `gen_ai.provider.name`. Two exports of one capsule describing the same run in two
vocabularies would be worse than one export, so they share the constant rather than each pinning its own.

**What becomes what.**

| frames | span | name | kind |
|---|---|---|---|
| `RUN_START` … `RUN_END` | one root span | `bbx.run` | INTERNAL |
| `NODE_OPEN` … `NODE_CLOSE` with kind `tool` | tool span | `execute_tool {gen_ai.tool.name}` | INTERNAL |
| any other `NODE_OPEN` … `NODE_CLOSE` | agent span | `invoke_agent {gen_ai.agent.name}`, or `invoke_agent` when the node has no name | INTERNAL |
| `MODEL_REQUEST` … `MODEL_RESPONSE`/`MODEL_ERROR` | inference span | `{gen_ai.operation.name} {gen_ai.request.model}`, both halves optional | CLIENT |

A model call whose path the mapping does not know gets **no** `gen_ai.operation.name` and is therefore not given a
semconv span name: it is named `bbx.model.call`, which says what it is without borrowing a vocabulary that would claim
more. The same rule as D-054, one level up.

**Identity is derived, not random.** A trace id is the first 32 hex of `sha256(run_id)`, so **two vantages of one run
share a trace** — the property P3B-03 is after, at trace level rather than node level (docs/node-edges.md). A span id is
the first 16 hex of `sha256(capsule_id:seq)` of the frame that opened the span, so two capsules of one run cannot collide
while the same capsule always renders the same ids. The writer's uuid is deliberately not used: the capsule does not
carry it (only `graph.json` does), and a derived part must be recomputable from what the capsule itself holds.

**What is not claimed.** No token usage, no finish reasons, no request parameters: this build has no response decoder
(P3-08 is open), and a span attribute invented from a byte count would be the exact over-claim the rest of this product
refuses to make. No prompts or completions, by the same rule that keeps them out of `events.jsonl` — the export carries
counters (`bbx.model.chunks_raw`, `bbx.model.bytes_received`), never text. An unclosed node gets a span that ends at the
tape's last frame and says `bbx.span.truncated: true`, because a missing close is a fact about the run and hiding the
span would lose the node entirely.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from blackbox import constants as C
from blackbox.cap import export_jsonl

#: Same pin as the JSONL export, and for the same reason: one capsule, one vocabulary.
SEMCONV = export_jsonl.SEMCONV
SEMCONV_COMMIT = export_jsonl.SEMCONV_COMMIT

SCOPE_NAME = "blackbox"
VERSION = "0.1.0.dev0"

#: OTLP's `SpanKind`: 0 unspecified, 1 internal, 2 server, 3 client, 4 producer, 5 consumer.
INTERNAL, CLIENT = 1, 3

#: OTLP's `StatusCode`: 0 unset, 1 ok, 2 error.
UNSET, OK, ERROR = 0, 1, 2


def _payload(frame: dict[str, Any]) -> Any:
    return export_jsonl._payload_of(frame)


def _ids(capsule_id: str, run_id: str | None) -> tuple[str, str]:
    """The trace id (one per run) and the root span id (one per capsule)."""
    trace = hashlib.sha256(f"{run_id or capsule_id}".encode()).hexdigest()[:32]
    root = hashlib.sha256(f"{capsule_id}:root".encode()).hexdigest()[:16]
    return trace, root


def _span_id(capsule_id: str, seq: int) -> str:
    return hashlib.sha256(f"{capsule_id}:{seq}".encode()).hexdigest()[:16]


def _value(value: Any) -> dict[str, Any]:
    """One OTLP `AnyValue`. int64 is a string in proto3 JSON, and this file follows that rather than inventing a shape."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_value(item) for item in value]}}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": [{"key": str(key), "value": _value(item)}
                                           for key, item in sorted(value.items())]}}
    return {"stringValue": "" if value is None else str(value)}


def _attributes(pairs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": key, "value": _value(pairs[key])} for key in sorted(pairs)]


def _status(ok: bool | None, message: str | None = None) -> dict[str, Any]:
    code = UNSET if ok is None else (OK if ok else ERROR)
    return {"code": code} if message is None else {"code": code, "message": message}


class _Builder:
    """Collects spans in frame order, then emits them in a stable order."""

    def __init__(self, capsule_id: str, run_id: str | None) -> None:
        self.capsule_id = capsule_id
        self.run_id = run_id
        self.trace, self.root_span = _ids(capsule_id, run_id)
        self.spans: list[dict[str, Any]] = []
        #: span id -> the seq of the frame that opened it, so the emit order is chronological and repeatable: two
        #: frames can share a wall-clock tick (Windows' clock is coarse), and an order that fell back to a hash would
        #: differ across platforms for no reason.
        self.seq_of: dict[str, int] = {}

    def add(self, *, seq: int, name: str, kind: int, start: int, end: int, parent: str | None,
            attributes: dict[str, Any], status: dict[str, Any] | None = None) -> str:
        span_id = _span_id(self.capsule_id, seq)
        span: dict[str, Any] = {"traceId": self.trace, "spanId": span_id, "name": name, "kind": kind,
                                "startTimeUnixNano": str(start), "endTimeUnixNano": str(max(start, end)),
                                "attributes": _attributes(attributes), "status": status or _status(None)}
        if parent:
            span["parentSpanId"] = parent
        self.spans.append(span)
        self.seq_of[span_id] = seq
        return span_id


def _node_span_name(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A node's span name and its semconv attributes, or ours when the node is not a tool or an agent."""
    name = payload.get("name")
    kind = payload.get("kind")
    if kind == "tool" and isinstance(name, str) and name:
        return f"execute_tool {name}", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name}
    attributes: dict[str, Any] = {"gen_ai.operation.name": "invoke_agent"}
    if isinstance(name, str) and name:
        attributes["gen_ai.agent.name"] = name
        return f"invoke_agent {name}", attributes
    return "invoke_agent", attributes


def _model_span_name(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """`{operation} {model}`, with each half optional — and our own name when the operation is not known."""
    path = payload.get("path") if isinstance(payload.get("path"), str) else None
    model = payload.get("model") if isinstance(payload.get("model"), str) and payload.get("model") else None
    operation = export_jsonl.operation_of(path)
    attributes: dict[str, Any] = {}
    if operation:
        attributes["gen_ai.operation.name"] = operation
    system = export_jsonl.system_of(payload.get("provider") if isinstance(payload.get("provider"), str) else None)
    if system:
        attributes["gen_ai.provider.name"] = system
    if model:
        attributes["gen_ai.request.model"] = model
    if operation:
        return f"{operation} {model}".strip(), attributes
    return "bbx.model.call", attributes


def render(frames: list[dict[str, Any]], *, capsule_id: str, run_id: str | None = None) -> bytes:
    """The whole document: one `resourceSpans` entry, one scope, the spans of this capsule in frame order."""
    builder = _Builder(capsule_id, run_id)
    last_ns = max((int(frame.get("wall_ns") or 0) for frame in frames), default=0)
    run_start = next((frame for frame in frames if int(frame.get("kind") or 0) == C.KIND_RUN_START), None)
    run_end = next((frame for frame in reversed(frames) if int(frame.get("kind") or 0) == C.KIND_RUN_END), None)
    root = builder.root_span if run_start is not None else None
    if run_start is not None:
        start = int(run_start.get("wall_ns") or 0)
        payload = _payload(run_start)
        attributes: dict[str, Any] = {"bbx.span.source": "run"}
        if isinstance(payload, dict) and isinstance(payload.get("run_id"), str):
            attributes["bbx.run.id"] = payload["run_id"]
        builder.seq_of[builder.root_span] = int(run_start.get("seq") or 0)
        builder.spans.append({
            "traceId": builder.trace, "spanId": builder.root_span, "name": "bbx.run", "kind": INTERNAL,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(int(run_end.get("wall_ns") or last_ns) if run_end is not None else last_ns),
            "attributes": _attributes(attributes),
            "status": _status(None if run_end is not None else False,
                              None if run_end is not None else "the run never ended: no RUN_END frame"),
        })

    node_spans: dict[int, list[tuple[int, str]]] = {}        # slot -> [(open_seq, span_id)], newest last
    open_nodes: dict[int, list[dict[str, Any]]] = {}
    open_calls: dict[int, list[dict[str, Any]]] = {}
    for frame in frames:
        kind = int(frame.get("kind") or 0)
        slot = int(frame.get("node_slot") or 0)
        seq = int(frame.get("seq") or 0)
        wall = int(frame.get("wall_ns") or 0)
        payload = _payload(frame)
        if kind == C.KIND_NODE_OPEN and isinstance(payload, dict):
            open_nodes.setdefault(slot, []).append({"seq": seq, "wall": wall, "payload": payload})
        elif kind == C.KIND_NODE_CLOSE:
            stack = open_nodes.get(slot)
            if not stack:
                continue
            opened = stack.pop(0)
            name, attributes = _node_span_name(opened["payload"])
            body = payload if isinstance(payload, dict) else {}
            parent_slot = opened["payload"].get("parent")
            parent: str | None = None
            if isinstance(parent_slot, int):
                parents_of_slot = node_spans.get(parent_slot) or []
                parent = parents_of_slot[-1][1] if parents_of_slot else None
            span_id = builder.add(seq=opened["seq"], name=name, kind=INTERNAL, start=opened["wall"], end=wall,
                                  parent=parent or root, attributes={**attributes,
                                                                     "bbx.node.slot": slot,
                                                                     "bbx.node.kind": opened["payload"].get("kind")},
                                  status=_status(body.get("outcome") != "error",
                                                 None if body.get("outcome") is None
                                                 else f"outcome {body.get('outcome')}"))
            if isinstance(body.get("duration_us"), int):
                builder.spans[-1]["attributes"].extend(_attributes({"bbx.node.duration_us": body["duration_us"]}))
            node_spans.setdefault(slot, []).append((opened["seq"], span_id))
        elif kind == C.KIND_MODEL_REQUEST and isinstance(payload, dict):
            open_calls.setdefault(slot, []).append({"seq": seq, "wall": wall, "payload": payload})
        elif kind in (C.KIND_MODEL_RESPONSE, C.KIND_MODEL_ERROR):
            calls = open_calls.get(slot)
            if not calls:
                continue
            request = calls.pop(0)
            body = payload if isinstance(payload, dict) else {}
            name, attributes = _model_span_name(request["payload"])
            attributes = {**attributes, "bbx.span.source": "model"}
            for key, name_ in (("chunks_raw", "bbx.model.chunks_raw"), ("chunks_stored", "bbx.model.chunks_stored"),
                              ("bytes_received", "bbx.model.bytes_received"), ("bytes_stored", "bbx.model.bytes_stored")):
                if isinstance(body.get(key), int):
                    attributes[name_] = body[key]
            for key, name_ in (("call_id", "bbx.call.id"), ("request_fp", "bbx.request.fp"),
                              ("response_fp", "bbx.response.fp"), ("status", "bbx.response.status")):
                if body.get(key) is not None:
                    attributes[name_] = body[key]
            if body.get("partial"):
                attributes["bbx.span.partial"] = True
            if isinstance(body.get("error"), str):
                attributes["bbx.model.error"] = body["error"]
            parents = node_spans.get(slot)
            parent = parents[-1][1] if parents else root
            builder.add(seq=request["seq"], name=name, kind=CLIENT, start=request["wall"], end=wall, parent=parent,
                        attributes=attributes,
                        status=_status(kind != C.KIND_MODEL_ERROR and not body.get("error"),
                                       body.get("error") if isinstance(body.get("error"), str) else None))

    for slot, stack in sorted(open_nodes.items()):
        for opened in stack:                                  # no close: a span that says it was cut short
            name, attributes = _node_span_name(opened["payload"])
            builder.add(seq=opened["seq"], name=name, kind=INTERNAL, start=opened["wall"], end=last_ns,
                        parent=root, attributes={**attributes, "bbx.node.slot": slot,
                                                 "bbx.node.kind": opened["payload"].get("kind"),
                                                 "bbx.span.truncated": True},
                        status=_status(False, "the node never closed: no NODE_CLOSE frame"))
    for slot, calls in sorted(open_calls.items()):
        for request in calls:
            name, attributes = _model_span_name(request["payload"])
            builder.add(seq=request["seq"], name=name, kind=CLIENT, start=request["wall"], end=last_ns,
                        parent=root, attributes={**attributes, "bbx.span.source": "model",
                                                 "bbx.span.truncated": True},
                        status=_status(False, "the call never closed: no MODEL_RESPONSE frame"))

    spans = sorted(builder.spans, key=lambda span: (int(span["startTimeUnixNano"]),
                                                    builder.seq_of.get(span["spanId"], 0)))
    document = {
        "resourceSpans": [{
            "resource": {"attributes": _attributes({
                "service.name": "blackbox",
                "bbx.capsule.id": capsule_id,
                **({"bbx.run.id": run_id} if run_id else {}),
            })},
            "scopeSpans": [{"scope": {"name": SCOPE_NAME, "version": VERSION, "attributes": []}, "spans": spans}],
        }]
    }
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"


def first_difference(stored: bytes, recomputed: bytes) -> dict[str, Any] | None:
    """Where two renderings disagree, as a line-based report — the same shape `export_jsonl` uses."""
    return export_jsonl.first_difference(stored, recomputed)


__all__ = ["CLIENT", "INTERNAL", "SEMCONV", "SEMCONV_COMMIT", "first_difference", "render"]
