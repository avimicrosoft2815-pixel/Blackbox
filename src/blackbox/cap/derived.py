"""Derived capsule parts, recomputed and compared (D-053, P4-13, P4-14, §13.1a) — the graph and the two exports.

The raw frames are the only authoritative part of a capsule; `graph.json` is a *derived* one: nodes come from the
`NODE_*`/`MODEL_*` frames and edges come from `links.json`, both of which the capsule already carries. That is what makes
verification possible without trusting the derived file: recompute it from the segments, compare, and report drift as
`BBX-E412` rather than opening a graph nobody can check.

`events/events.jsonl` (P4-13) is the same contract applied to a second artefact: it is a pure function of the frames
(`export_jsonl.render`), so it can be recomputed byte for byte, and a line edited after sealing is found by comparing the
bytes rather than by trusting the file.

Two things this deliberately does not do. It does not *repair*: a drifted part is reported and the capsule is left
exactly as it is, because a verifier that rewrites the artefact has destroyed the evidence of the drift. And it does not
invent a part it cannot recompute — an absent `graph.json` (every capsule written before this change) is reported as
absent, not as a failure, so old capsules keep verifying.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from blackbox import constants as C
from blackbox.cap import export_jsonl, export_otlp
from blackbox.cap import format as cap
from blackbox.errors import bbx
from blackbox.rec import recovery
from blackbox.tape import frame as frame_mod


def frames_of(capsule: cap.Capsule) -> list[dict[str, Any]]:
    """The capsule's frames, in the shape `scanner.scan` produces — the input the graph is derived from."""
    out: list[dict[str, Any]] = []
    segment_numbers = sorted(int(name.rsplit("-", 1)[-1].split(".")[0]) for name in capsule.entries
                             if name.startswith("events/seg-"))
    for number in segment_numbers:
        raw = capsule.frames_of(number)
        off = C.SEGMENT_HEADER_BYTES
        while off < len(raw):
            try:
                frame, off = frame_mod.decode(raw, off)
            except Exception:                     # the segment is padded; padding is not a frame
                break
            out.append({**frame, "segment": number})
    return out


def recompute(capsule: cap.Capsule) -> dict[str, Any] | None:
    """The graph this capsule's frames and its own `links.json` imply, or `None` when there is nothing to derive from."""
    if cap.GRAPH_NAME not in capsule.entries:
        return None
    links_bytes = capsule.entries.get(cap.LINKS_NAME, b"")
    try:
        stored = json.loads(capsule.entry(cap.GRAPH_NAME).decode("utf-8"))
    except Exception as exc:
        raise bbx("BBX-E412", f"{cap.GRAPH_NAME} is not JSON: {type(exc).__name__}: {exc}") from None
    if not isinstance(stored, dict):
        raise bbx("BBX-E412", f"{cap.GRAPH_NAME} is not an object")
    writer = stored.get("writer_uuid")
    if not isinstance(writer, str) or not writer:
        raise bbx("BBX-E412", f"{cap.GRAPH_NAME} names no writer")
    frames = frames_of(capsule)
    run_status, cause = recovery.run_status(frames, sealed=bool(frames) and _has_seg_end(frames))
    interrupted = recovery.classify_nodes(frames)
    edges, edge_summary = recovery.graph_edges(links_bytes, writer)
    return {
        "writer_uuid": writer,
        "run_id": stored.get("run_id"),
        "frames": len(frames),
        "gaps": stored.get("gaps"),
        "nodes": recovery.graph_nodes(frames, interrupted=interrupted),
        "open_nodes": len(interrupted),
        "run_status": run_status,
        "status_cause": cause,
        "edges": edges,
        "edge_summary": edge_summary,
    }


def _has_seg_end(frames: list[dict[str, Any]]) -> bool:
    return any(int(frame.get("kind") or 0) == C.KIND_SEG_END for frame in frames)


#: The keys a recomputation can check. Everything a capsule's graph says about itself is derived from frames and links
#: except these, so these are what drift is measured on; a key the stored graph does not carry is not compared, which is
#: how a graph written by an older build (no `edge_summary`) keeps verifying.
COMPARED = ("nodes", "open_nodes", "edges", "edge_summary", "frames", "run_status")


def recompute_events(capsule: cap.Capsule) -> bytes | None:
    """The `events.jsonl` this capsule's own frames imply, or `None` when the capsule does not carry the part."""
    if cap.EVENTS_NAME not in capsule.entries:
        return None
    return export_jsonl.render(frames_of(capsule))


def recompute_otlp(capsule: cap.Capsule) -> bytes | None:
    """The `otlp/spans.json` this capsule's own frames imply, or `None` when the capsule does not carry the part."""
    if cap.OTLP_NAME not in capsule.entries:
        return None
    return export_otlp.render(frames_of(capsule), capsule_id=str(capsule.manifest.get("capsule_id") or ""),
                              run_id=export_jsonl_run_id(capsule))


def recompute_otlp_pb(capsule: cap.Capsule) -> bytes | None:
    """The `otlp/spans.pb` this capsule's own frames imply (P4-20), or `None` when the capsule does not carry it."""
    if cap.OTLP_PB_NAME not in capsule.entries:
        return None
    return export_otlp.render_pb(frames_of(capsule), capsule_id=str(capsule.manifest.get("capsule_id") or ""),
                                 run_id=export_jsonl_run_id(capsule))


def export_jsonl_run_id(capsule: cap.Capsule) -> str | None:
    """The run id the capsule's own manifest declares — the one thing the exports need that the frames do not repeat."""
    run = capsule.manifest.get("run")
    run_id = run.get("run_id") if isinstance(run, dict) else None
    return run_id if isinstance(run_id, str) and run_id else None


#: The parts this build knows how to recompute, in the order a report lists them. A part in a capsule that is not here
#: is not verified — and is reported as such, because "we cannot check this" must never look like "it checked out".
PARTS = (cap.EVENTS_NAME, cap.GRAPH_NAME, cap.OTLP_NAME, cap.OTLP_PB_NAME)


def verify(capsule: cap.Capsule) -> dict[str, Any]:
    """`{"ok", "parts", "drift", "absent"}` for one capsule, recomputing every derived part it carries."""
    report: dict[str, Any] = {"ok": True, "parts": [], "drift": [], "absent": [], "unknown": []}
    declared = capsule.manifest.get("derived_parts")
    declared_parts = [part for part in declared if isinstance(part, dict)] if isinstance(declared, list) else []
    declared_names = {part.get("name"): part for part in declared_parts}
    for name in declared_names:
        if name not in PARTS:
            report["unknown"].append(name)
    present = [name for name in PARTS if name in capsule.entries]
    if not present:
        report["absent"] = [f"{cap.GRAPH_NAME} (no derived parts in this capsule)"] if not declared_parts else \
                           [f"{part} (declared, not in this capsule)" for part in PARTS]
        report["ok"] = True                    # nothing carried, nothing wrong: absence is reported, not failed
        return report
    for name in PARTS:
        if name not in capsule.entries:
            report["absent"].append(f"{cap.GRAPH_NAME if name == cap.GRAPH_NAME else name} (not in this capsule)")
            continue
        stored_bytes = capsule.entry(name)
        actual_sha = hashlib.sha256(stored_bytes).hexdigest()
        declared_sha = (declared_names.get(name) or {}).get("sha256")
        if declared_sha is not None and declared_sha != actual_sha:
            report["drift"].append({"part": name, "field": "sha256", "declared": declared_sha, "actual": actual_sha})
        if name == cap.GRAPH_NAME:
            _compare_graph(capsule, stored_bytes, report)
        elif name == cap.OTLP_PB_NAME:
            # A protobuf is bytes, not lines: reporting "line 3" of a binary would name a byte offset as if it were
            # text. The sha256 comparison above already catches any change; when the bytes differ, the report says
            # which encoding disagreed, not which "line".
            expected_pb = recompute_otlp_pb(capsule) or b""
            if stored_bytes != expected_pb:
                report["drift"].append({"part": name, "field": f"bytes ({len(stored_bytes)} stored, "
                                                               f"{len(expected_pb)} recomputed)",
                                        "stored": hashlib.sha256(stored_bytes).hexdigest()[:32],
                                        "recomputed": hashlib.sha256(expected_pb).hexdigest()[:32],
                                        "note": "protobuf compares as bytes; there are no lines to number"})
        else:
            expected_bytes = (recompute_events(capsule) if name == cap.EVENTS_NAME else recompute_otlp(capsule)) or b""
            difference = export_jsonl.first_difference(stored_bytes, expected_bytes)
            if difference is not None:
                report["drift"].append({"part": name, "field": f"line {difference['line']}",
                                        "stored": difference.get("stored"),
                                        "recomputed": difference.get("recomputed"),
                                        **({"note": difference["note"]} if "note" in difference else {})})
        report["parts"].append({"name": name, "sha256": actual_sha,
                                "declared": declared_sha is not None,
                                "generator": (declared_names.get(name) or {}).get("generator")})
    # Drift fails; a part this build cannot recompute is reported (`unknown`) and does not, because an older tool
    # reading a newer capsule must not call it corrupt.
    report["ok"] = not report["drift"]
    return report


def _compare_graph(capsule: cap.Capsule, stored_bytes: bytes, report: dict[str, Any]) -> None:
    """The graph's own comparison: JSON keys, because a graph is a document and not a stream of lines."""
    try:
        stored = json.loads(stored_bytes.decode("utf-8"))
    except Exception as exc:
        report["drift"].append({"part": cap.GRAPH_NAME, "field": "json",
                                "stored": f"{type(exc).__name__}: {exc}", "recomputed": None})
        return
    expected = recompute(capsule)
    for key in COMPARED:
        if expected is None or key not in stored:
            continue
        if _canonical(stored[key]) != _canonical(expected[key]):
            report["drift"].append({"part": cap.GRAPH_NAME, "field": key,
                                    "stored": _first_difference(stored[key], expected[key]),
                                    "recomputed": _first_difference(expected[key], stored[key])})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _first_difference(left: Any, right: Any) -> Any:
    """The first item that differs, so a report names one thing instead of dumping two documents."""
    if isinstance(left, list) and isinstance(right, list):
        for index, item in enumerate(left):
            other = right[index] if index < len(right) else None
            if _canonical(item) != _canonical(other):
                return item
        return None if len(left) == len(right) else f"length {len(left)}"
    return left
