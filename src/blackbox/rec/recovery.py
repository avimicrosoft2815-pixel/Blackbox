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
import json
import os
import time
from dataclasses import dataclass, field

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.codec.ids import blake2b
from blackbox.errors import bbx
from blackbox.platform import current as _plat_current
from blackbox.rec import discover
from blackbox.tape import chain as chain_mod
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
        if hasattr(os, "fdatasync"):
            os.fdatasync(fd)
        else:
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):        # Windows has no directory fsync; its shim says so
        _plat.fsync_dir(directory)


def _read_json(path: str) -> dict | None:
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

    def to_dict(self) -> dict:
        out = {"node_slot": self.node_slot, "status": self.status, "detail": self.detail,
               "model_requests": self.model_requests, "tool_requests": self.tool_requests,
               "tool_results": self.tool_results}
        if self.status == INTERRUPTED_MID_STREAM:
            out.update({"chunks": self.chunks, "partial_stream_bytes": self.partial_stream_bytes,
                        "last_chunk_age_ms": 0})
        return out


def classify_nodes(frames: list[dict], *, now_wall_ns: int | None = None) -> list[NodeRecord]:
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
            continue
        if kind == C.KIND_NODE_OPEN:
            rec(slot)
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
            r.partial_stream_bytes += len(payload)
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

    out: list[NodeRecord] = []
    for slot, r in sorted(nodes.items()):
        if slot in closed:
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


def run_status(frames: list[dict], *, sealed: bool) -> tuple[str, str | None]:
    """`(status, status_cause)` for the run itself: `SEAL_INTERRUPTED` when the run ended but was never sealed."""
    has_end = any(f["kind"] == C.KIND_RUN_END for f in frames)
    if has_end and not sealed:
        return SEAL_INTERRUPTED, "SEAL_NEVER_FINISHED"
    return ("recovered", "PROCESS_DEATH")


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
    node_statuses: list[dict] = field(default_factory=list)
    run_status: str = "recovered"
    status_cause: str | None = None

    def to_dict(self) -> dict:
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
        # Stable identity: the tape says who it is, so a resumed recovery lands in the same run directory.
        self.run_id = "r_" + _hash(writer_uuid_hex, meta.get("created_wall_ns", 0))[:16]
        self.run_dir = os.path.join(data_dir, "runs", self.run_id)
        self.capsule_id = "c_" + _hash(self.run_id)[:16]
        self.capsule_path = os.path.join(data_dir, "capsules", f"{self.capsule_id}.bbx")
        self.result = RecoveryResult(run_id=self.run_id, run_dir=self.run_dir)
        self._scan: scanner.ScanResult | None = None

    # ── infrastructure ──────────────────────────────────────────────────────
    def _read_meta(self) -> dict:
        try:
            with open(os.path.join(self.tape_dir, "meta"), "rb") as fh:
                return meta_mod.decode(fh.read(C.META_BYTES))
        except (FileNotFoundError, Exception):        # a damaged meta is the DISCOVERED step's problem
            return {}

    def _marker_path(self, step: str) -> str:
        return os.path.join(self.run_dir, f"recovery.done.{step}")

    def _marker(self, step: str) -> dict | None:
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
            if hasattr(os, "fdatasync"):
                os.fdatasync(fd)
            else:
                os.fsync(fd)
        finally:
            os.close(fd)
        return True

    def _lock_is_live(self, held: dict) -> bool:
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
                 "frames": scan.frame_count, "gaps": scan.gaps, "nodes": [n.to_dict() for n in nodes],
                 "run_status": status, "status_cause": cause,
                 "open_nodes": sum(1 for n in nodes), "edges": []}
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
            with open(segments.seg_path(self.tape_dir, n), "rb") as fh:
                out[cap.seg_entry_name(n)] = fh.read()
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
            "capture": {"coverage": "PARTIAL" if scan.truncated_tail else "FULL", "providers": []},
            "redaction": {"profile_id": "none", "content_mode": "full", "masks": 0, "failures": 0},
            "gaps": scan.gaps,
            "counts": {"events": scan.frame_count, "nodes": len(self.result.node_statuses),
                       "segments": len(entries)},
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


def recover(data_dir: str, writer_uuid_hex: str | None = None, *, now_ns: int | None = None) -> RecoveryResult:
    """Convenience entry point: pick the newest unsealed tape and recover it."""
    if writer_uuid_hex is None:
        candidates = discover.discover(data_dir)
        if not candidates:
            raise bbx("BBX-E220", f"{data_dir} holds no tape to recover")
        newest = max(candidates, key=lambda e: (e.created_wall_ns, e.writer_uuid))
        writer_uuid_hex = newest.writer_uuid
    return Recovery(data_dir, writer_uuid_hex, now_ns=now_ns).run()
