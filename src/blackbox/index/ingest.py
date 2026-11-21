"""Reading a capsule into rows, and writing those rows idempotently.

The source of this index is the sealed capsule — not the tape, not the run directory. A tape that has not been sealed is
invisible here on purpose: the index is what a reader may rely on, and a capsule is the artefact that has an integrity
root over exactly the bytes the rows came from. `blackbox verify` seals nothing either; recovery does.

Three things the reader is careful about:

* **Frames, not summaries.** Events and nodes come from the capsule's own segment bytes (`Capsule.frames_of`), decoded
  with the same frame decoder every other reader uses. The manifest's `counts` block is compared against what the frames
  actually hold, and a disagreement is returned, not silently preferred either way.
* **A damaged capsule is a row with a reason.** A capsule that cannot be read is skipped and counted; the build reports
  `unreadable` rather than failing the whole index because one file in a directory is broken — that is when someone runs
  the command.
* **Nothing host-specific is stored.** The capsule column holds the file name, so two data directories with the same
  capsules produce the same rows.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from typing import Any

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.index import schema
from blackbox.tape import frame as frame_mod


def _frames(blob: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = C.SEGMENT_HEADER_BYTES
    while offset < len(blob):
        try:
            fields, offset = frame_mod.decode(blob, offset)
        except Exception:                     # a capsule's segments were validated on write; stop quietly if not
            break
        out.append(fields)
    return out


def capsule_rows(path: str) -> dict[str, Any]:
    """Every row one capsule contributes: `run`, `node[]`, `event[]`, `artifact`, plus what could not be agreed.

    Raises whatever `cap.read_capsule` raises for an unreadable file — the caller decides whether that is a warning or
    a failure, because only the caller knows how many other capsules there are.
    """
    capsule = cap.read_capsule(path)
    manifest = capsule.manifest
    run_block = manifest.get("run") or {}
    counts = manifest.get("counts") or {}
    frames: list[dict[str, Any]] = []
    for name in sorted(capsule.entries):
        # `events/seg-000001.raw` — the segment entries, in segment order, which is the order their seqs ascend in.
        if name.startswith("events/seg-") and name.endswith(".raw"):
            frames.extend(_frames(capsule.entries[name]))
    run_id = str(run_block.get("run_id") or "")
    nodes: dict[int, dict[str, Any]] = {}
    for fields in frames:
        slot = int(fields.get("node_slot") or 0)
        if fields["kind"] == C.KIND_NODE_OPEN:
            nodes.setdefault(slot, {"kind": "node", "name": "", "status": None})
            body = _payload(fields)
            nodes[slot]["name"] = str(body.get("name") or body.get("kind") or f"node {slot}")
            nodes[slot]["kind"] = str(body.get("kind") or "node")
        elif fields["kind"] == C.KIND_NODE_CLOSE and slot in nodes:
            body = _payload(fields)
            status = body.get("status")
            nodes[slot]["status"] = None if status is None else str(status)
    events = [{"seq": int(f["seq"]), "kind": int(f["kind"]), "t_wall_ns": int(f["wall_ns"]),
               "bytes": int(f["len"])} for f in frames]
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    disagreements: list[str] = []
    if counts.get("events") is not None and int(counts["events"]) != len(events):
        disagreements.append(f"manifest counts.events={counts['events']} but the segments hold {len(events)}")
    if counts.get("nodes") is not None and int(counts["nodes"]) != len(nodes):
        disagreements.append(f"manifest counts.nodes={counts['nodes']} but the segments hold {len(nodes)}")
    return {
        "run": {"run_id": run_id, "capsule": os.path.basename(path),
                "root": str((manifest.get("integrity") or {}).get("root") or "") or None,
                "status": run_block.get("status"), "status_cause": run_block.get("status_cause"),
                "completeness": run_block.get("completeness"),
                "events": len(events), "nodes": len(nodes), "bytes": os.path.getsize(path)},
        "nodes": [{"run_id": run_id, "node_slot": slot, **row} for slot, row in sorted(nodes.items())],
        "events": [{"run_id": run_id, **row} for row in sorted(events, key=lambda r: r["seq"])],
        "artifact": {"artifact_id": str(manifest.get("capsule_id") or os.path.basename(path)),
                     "run_id": run_id, "kind": "capsule", "path": os.path.basename(path),
                     "bytes": os.path.getsize(path), "sha256": digest, "created_ns": None},
        "disagreements": disagreements,
    }


def _payload(fields: dict[str, Any]) -> dict[str, Any]:
    from blackbox.codec import cbor

    try:
        body = cbor.decode_exact(fields.get("payload") or b"")
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def insert_rows(con: sqlite3.Connection, rows: dict[str, Any]) -> dict[str, int]:
    """Insert one capsule's rows, idempotently. Returns how many rows were new (a re-ingest adds none).

    Two things here are load-bearing for a 500-capsule build on a slow filesystem, and both were measured (defect 114):

    * **The count comes from `total_changes`, not from two `COUNT(*)` sweeps.** The first version counted every table
      before and after each capsule — 1 928 `SELECT`s for a 120-capsule corpus, and eight sweeps of a 4 500-row table per
      capsule at 500. `size` now costs nothing.
    * **The transaction is the caller's if one is open.** `build()` wraps the whole loop in one `BEGIN IMMEDIATE`, so a
      500-capsule build issues one `COMMIT` instead of 500. A commit is a journal write plus a sync, which is the part a
      Windows runner with a scanner on the volume pays most for; the index is derived data, so a crash that loses a
      partial build costs a rebuild and nothing else.
    """
    before = con.total_changes
    owns = not con.in_transaction
    if owns:
        con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "INSERT INTO run (run_id, capsule, root, status, status_cause, completeness, events, nodes, bytes) "
            "VALUES (:run_id, :capsule, :root, :status, :status_cause, :completeness, :events, :nodes, :bytes) "
            "ON CONFLICT (run_id) DO NOTHING", rows["run"])
        for node in rows["nodes"]:
            con.execute(
                "INSERT INTO node (run_id, node_slot, kind, name, status) VALUES (:run_id, :node_slot, :kind, :name, "
                ":status) ON CONFLICT (run_id, node_slot) DO NOTHING", node)
        for event in rows["events"]:
            con.execute(
                "INSERT INTO event (run_id, seq, kind, t_wall_ns, bytes) VALUES (:run_id, :seq, :kind, :t_wall_ns, "
                ":bytes) ON CONFLICT (run_id, seq) DO NOTHING", event)
        artifact = rows["artifact"]
        con.execute(
            "INSERT INTO artifact (artifact_id, run_id, kind, path, bytes, sha256, created_ns) VALUES (:artifact_id, "
            ":run_id, :kind, :path, :bytes, :sha256, :created_ns) ON CONFLICT (artifact_id) DO NOTHING", artifact)
        if owns:
            con.execute("COMMIT")
    except BaseException:
        if owns and con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return {"added": con.total_changes - before}



