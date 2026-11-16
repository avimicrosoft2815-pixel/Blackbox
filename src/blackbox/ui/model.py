"""The viewer's data: a capsule (or a data directory of them) as one JSON document, with no interpolation of opinions.

The rule this module follows is the one `docs/format.md` follows: **the page renders what the capsule says and labels
what it does not know.** So every field here is either read out of the capsule or computed from bytes in it — no
"probably", no filled-in default that looks like a measurement. Where the capsule is silent (a node with no duration, a
run whose status is `SEAL_INTERRUPTED`), the document says so in the same shape the CLI says it, and the page has a
place to put it.

Two deliberate choices:

* **the frames are decoded once, here, not in JavaScript.** The browser gets a flat list of events with their kind
  names already resolved, because a viewer that re-implements the frame layout is a second reader of a frozen format
  and the first place a disagreement would hide.
* **nothing is trusted as markup.** Payloads are summarised into strings (`summary`), never shipped as HTML, and the
  page writes them with `textContent`. A capsule is untrusted input by construction — it arrived from somewhere.
"""
from __future__ import annotations

import os
from typing import Any

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.codec import cbor
from blackbox.errors import bbx
from blackbox.tape import frame as frame_mod

VIEWER_VERSION = 1
#: Kinds whose payload is a summary of something the user cares about, and how to say it in one line.
_KIND_LABEL = getattr(C, "KIND_NAMES", {})


def _text(value: Any, limit: int = 400) -> str:
    """A payload value as one short line of plain text. Never markup, never a nested structure."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = cbor_repr(value)
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def cbor_repr(value: Any) -> str:
    """A readable one-line rendering of a decoded payload — keys sorted, so the same payload always reads the same."""
    if isinstance(value, dict):
        return ", ".join(f"{k}={cbor_repr(v)}" for k, v in sorted(value.items(), key=lambda kv: str(kv[0])))
    if isinstance(value, list):
        return "[" + ", ".join(cbor_repr(v) for v in value) + "]"
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def decode_frames(blob: bytes) -> list[dict[str, Any]]:
    """Every decodable frame in a capsule's logical segment bytes; stops quietly at a torn tail.

    A capsule's segments were validated on write, so a failure here is not a normal state — but a viewer that raises
    on a damaged capsule cannot show the reader *that* it is damaged, which is the one thing they need to know.
    """
    out: list[dict[str, Any]] = []
    offset = C.SEGMENT_HEADER_BYTES
    while offset < len(blob):
        try:
            fields, offset = frame_mod.decode(blob, offset)
        except Exception:
            break
        out.append(fields)
    return out


def _payload(fields: dict[str, Any]) -> dict[str, Any]:
    try:
        body = cbor.decode_exact(fields.get("payload") or b"")
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _summary(fields: dict[str, Any]) -> str:
    kind = fields["kind"]
    body = _payload(fields)
    if not body:
        raw = fields.get("payload") or b""
        return f"{len(raw)} B" if raw else ""
    if kind == C.KIND_RUN_START:
        argv = body.get("argv")
        return _text({"run_id": body.get("run_id"), "source": body.get("source"),
                      "attached": body.get("attached"), "argv": argv})
    if kind in (C.KIND_NODE_OPEN, C.KIND_NODE_CLOSE):
        return _text({k: body[k] for k in ("name", "kind", "status") if k in body})
    if kind == C.KIND_MODEL_REQUEST:
        return _text({k: body[k] for k in ("provider", "model", "stream") if k in body})
    if kind == C.KIND_MODEL_CHUNK:
        return f"{len(fields.get('payload') or b'')} B chunk"
    if kind == C.KIND_TOOL_REQUEST:
        return _text({k: body[k] for k in ("name", "tool") if k in body})
    if kind == C.KIND_MARK:
        return _text({k: body[k] for k in ("kind", "name", "hash") if k in body})
    return _text(body)


def _rel_ms(wall_ns: int, start_ns: int) -> float:
    return round((wall_ns - start_ns) / 1_000_000, 3) if start_ns and wall_ns else 0.0


def _nodes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per node slot that carries a name: what it was, when it ran, and whether it closed."""
    nodes: dict[int, dict[str, Any]] = {}
    for event in events:
        slot = int(event.get("node_slot") or 0)
        if not slot:
            continue
        entry = nodes.setdefault(slot, {"slot": slot, "name": f"node {slot}", "kind": "unknown",
                                        "opened_rel_ms": event["rel_ms"], "closed_rel_ms": None,
                                        "events": [], "model_calls": 0, "chunks": 0, "tools": []})
        entry["events"].append(event["i"])
        if event["kind"] == C.KIND_NODE_OPEN:
            body = _payload({"payload": event["payload"]}) if event.get("payload") else {}
            entry["name"] = _text(body.get("name") or entry["name"], 120)
            entry["kind"] = _text(body.get("kind") or entry["kind"], 40)
        elif event["kind"] == C.KIND_NODE_CLOSE:
            entry["closed_rel_ms"] = event["rel_ms"]
        elif event["kind"] == C.KIND_MODEL_REQUEST:
            entry["model_calls"] += 1
        elif event["kind"] == C.KIND_MODEL_CHUNK:
            entry["chunks"] += 1
        elif event["kind"] == C.KIND_TOOL_REQUEST:
            body = _payload({"payload": event.get("payload") or b""})
            entry["tools"].append(_text(body.get("name") or body.get("tool") or "tool", 80))
    out = []
    for slot in sorted(nodes):
        entry = nodes[slot]
        closed = entry.pop("closed_rel_ms")
        entry["closed_rel_ms"] = closed
        entry["closed"] = closed is not None
        entry["duration_ms"] = round((closed - entry["opened_rel_ms"]), 3) if closed is not None else None
        entry["status"] = "closed" if closed is not None else "open-at-end"
        out.append(entry)
    return out


def document_for_capsule(path: str, *, role: str = "", writer: str = "") -> dict[str, Any]:
    """One capsule as the page's document. Raises `BBX-E403`-class errors only when the capsule cannot be read."""
    capsule = cap.read_capsule(path)
    manifest = capsule.manifest
    segment_names = [name for name in capsule.entries if name.startswith("events/")]
    events: list[dict[str, Any]] = []
    decoded: list[dict[str, Any]] = []
    for name in segment_names:
        for fields in decode_frames(capsule.entry(name)):
            decoded.append(fields)
            events.append({
                "i": len(events),
                "seq": fields.get("seq"),
                "kind": fields["kind"],
                "kind_name": fields.get("kind_name") or _KIND_LABEL.get(fields["kind"], ""),
                "node_slot": int(fields.get("node_slot") or 0),
                "wall_ns": int(fields.get("wall_ns") or 0),
                "rel_ms": 0.0,
                "summary": _summary(fields),
                "payload": fields.get("payload") or b"",
            })
    start_ns = next((e["wall_ns"] for e in events if e["kind"] == C.KIND_RUN_START and e["wall_ns"]), 0)
    for event in events:
        event["rel_ms"] = _rel_ms(event["wall_ns"], start_ns)
    links = {}
    try:
        links = __import__("json").loads(capsule.entry(cap.LINKS_NAME).decode("utf-8"))
    except Exception:
        links = {"links": [], "summary": {"declared": 0, "exact": 0, "inferred": 0}}
    run = manifest.get("run") or {}
    # The identity the page shows is the one the tape *names*, when it names one: a capsule's own `run.run_id` is
    # derived from the writer (`Recovery.run_id`), which is what makes two tapes of one run keep separate capsule files.
    # A reader who ran `blackbox run` was given the named id, so that is the id the page must show — with the derived
    # one kept beside it as `capsule_run_id`, because that is what the capsule file is called.
    start_body = next((_payload(f) for f in decoded if f["kind"] == C.KIND_RUN_START), {})
    named_run = start_body.get("run_id")
    # What kind of tape this is, said by the tape: the SDK marks its own boundary, the hook folder names its vantage,
    # and the launcher's boundary is the one that carries the argv.
    tape_role = role or ("sdk tape" if start_body.get("source") == "sdk"
                         else "hook tape" if start_body.get("vantage") == "hook"
                         else "launcher boundary" if start_body.get("argv") is not None or start_body.get("stall_ms")
                         else "tape")
    return {
        "viewer": {"version": VIEWER_VERSION, "capsule_id": manifest.get("capsule_id"),
                   "capsule_path": os.path.basename(path), "role": tape_role, "writer": writer,
                   "root": (manifest.get("integrity") or {}).get("root"),
                   "root_ok": bool(capsule.root_ok), "problems": list(capsule.problems)},
        "run": {"run_id": named_run or run.get("run_id"), "capsule_run_id": run.get("run_id"),
                "status": run.get("status"), "status_cause": run.get("status_cause"),
                "durability_class": run.get("durability_class"), "completeness": run.get("completeness"),
                "truncated_tail": bool(run.get("truncated_tail")), "lost_from_seq": run.get("lost_from_seq"),
                "started_ns": start_ns, "ended_ns": events[-1]["wall_ns"] if events else 0},
        "counts": dict(manifest.get("counts") or {}),
        "capture": dict(manifest.get("capture") or {}),
        "redaction": dict(manifest.get("redaction") or {}),
        "gaps": list(manifest.get("gaps") or []),
        "links": links,
        "events": [{k: v for k, v in e.items() if k != "payload"} for e in events],
        "nodes": _nodes(events),
        "cost": {"model_calls": sum(1 for e in events if e["kind"] == C.KIND_MODEL_REQUEST),
                 "tool_calls": sum(1 for e in events if e["kind"] == C.KIND_TOOL_REQUEST),
                 "chunks": sum(1 for e in events if e["kind"] == C.KIND_MODEL_CHUNK),
                 "stream_bytes": sum(len(e["payload"]) for e in events if e["kind"] == C.KIND_MODEL_CHUNK)},
    }


def document_for_path(path: str) -> dict[str, Any]:
    """A capsule → a one-tape document; a directory → one document per capsule, linked into one run list.

    The data directory case is what makes the linked tapes of `blackbox run` legible: three capsules, three entries in
    the rail, and the `exact` links between them shown rather than merged.
    """
    if os.path.isdir(path):
        capsule_dir = os.path.join(path, "capsules") if os.path.isdir(os.path.join(path, "capsules")) else path
        paths = sorted(os.path.join(capsule_dir, name) for name in os.listdir(capsule_dir) if name.endswith(".bbx"))
        if not paths:
            raise bbx("BBX-E220", f"{path} holds no capsule to view")
        docs = [document_for_capsule(p) for p in paths]
        # Which tape the timeline shows: the one with the most events, because that is where the work is. On
        # `blackbox run -- python app.py` that is the child's SDK tape, not the launcher's five-frame boundary —
        # a page that opened on the boundary would look like a run in which nothing happened.
        docs.sort(key=lambda doc: (-len(doc["events"]), doc["viewer"]["capsule_path"] or ""))
        primary = docs[0]
        for doc in docs:
            linked = [link for link in (doc["links"].get("links") or []) if link.get("class") == "exact"]
            if linked:
                doc["viewer"]["role"] = doc["viewer"]["role"] + " · linked"
        primary["tapes"] = [{k: doc["viewer"][k] for k in ("capsule_id", "capsule_path", "role", "writer")}
                            | {"run_id": doc["run"]["run_id"], "status": doc["run"]["status"],
                               "events": doc["counts"].get("events"), "nodes": doc["counts"].get("nodes")}
                            for doc in docs]
        return primary
    return document_for_capsule(path)
