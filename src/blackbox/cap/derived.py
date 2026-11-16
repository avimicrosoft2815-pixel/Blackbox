"""Derived capsule parts, recomputed and compared (D-053, §13.1a) — `graph.json` today.

The raw frames are the only authoritative part of a capsule; `graph.json` is a *derived* one: nodes come from the
`NODE_*`/`MODEL_*` frames and edges come from `links.json`, both of which the capsule already carries. That is what makes
verification possible without trusting the derived file: recompute it from the segments, compare, and report drift as
`BBX-E412` rather than opening a graph nobody can check.

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


def verify(capsule: cap.Capsule) -> dict[str, Any]:
    """`{"ok", "parts": [...], "drift": [...]}` for one capsule, recomputing every derived part it declares."""
    report: dict[str, Any] = {"ok": True, "parts": [], "drift": [], "absent": []}
    declared = capsule.manifest.get("derived_parts")
    declared_parts = [part for part in declared if isinstance(part, dict)] if isinstance(declared, list) else []
    if cap.GRAPH_NAME not in capsule.entries:
        report["absent"].append(cap.GRAPH_NAME if not declared_parts else f"{cap.GRAPH_NAME} (not in this capsule)")
        return report
    stored_bytes = capsule.entry(cap.GRAPH_NAME)
    stored = json.loads(stored_bytes.decode("utf-8"))
    declared_names = {part.get("name"): part for part in declared_parts}
    declared_sha = (declared_names.get(cap.GRAPH_NAME) or {}).get("sha256")
    actual_sha = hashlib.sha256(stored_bytes).hexdigest()
    if declared_sha is not None and declared_sha != actual_sha:
        report["drift"].append({"part": cap.GRAPH_NAME, "field": "sha256", "declared": declared_sha, "actual": actual_sha})
    expected = recompute(capsule)
    for key in COMPARED:
        if expected is None or key not in stored:
            continue
        if _canonical(stored[key]) != _canonical(expected[key]):
            report["drift"].append({"part": cap.GRAPH_NAME, "field": key,
                                    "stored": _first_difference(stored[key], expected[key]),
                                    "recomputed": _first_difference(expected[key], stored[key])})
    report["parts"].append({"name": cap.GRAPH_NAME, "sha256": actual_sha,
                            "declared": declared_sha is not None, "generator": (declared_names.get(cap.GRAPH_NAME)
                                                                                or {}).get("generator")})
    report["ok"] = not report["drift"]
    return report


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
