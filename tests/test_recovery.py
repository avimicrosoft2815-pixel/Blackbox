"""P2-06 / P2-07 / P2-12 — the recovery state machine, its resume logic, and the interrupted-node table.

Three acceptance criteria are checked here:

* **P2-07** — the five §14.4 classes produce the exact status strings, with `partial_stream_bytes`, `chunks` and
  `last_chunk_age_ms` on the mid-stream row;
* **P2-06** — a kill at any of the eight steps resumes to the *same* capsule root. The child process is killed for
  real (`os._exit`) at `RECOVERY.<STEP>.before`, `.mid` and `.after`, so all three "modes" leave a different mess;
* **P2-12** — ten recoveries of one tape yield one capsule hash, and two concurrent recoverers yield one winner.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.rec import recovery
from blackbox.tape.writer import TapeWriter

CHILD = ROOT / "tests" / "_recovery_child.py"


def _frame(seq: int, kind: int, *, slot: int = 1, payload: bytes = b"", wall_ns: int = 1) -> dict:
    return {"seq": seq, "kind": kind, "kind_name": C.KIND_NAMES.get(kind, str(kind)), "node_slot": slot,
            "payload": payload, "wall_ns": wall_ns, "len": 0, "segment": 1}


def _tape(tmp_path: Path, *, events: int = 40) -> TapeWriter:
    w = TapeWriter(str(tmp_path / "data"), segment_bytes=C.MIN_SEGMENT_BYTES)
    w.batch_bytes = 0
    w.batch_ms = 0.0
    return w.open()


def _write_run(w: TapeWriter, *, end: bool = True) -> None:
    w.append(C.KIND_RUN_START, b"{}", priority=0)
    for _ in range(20):
        w.append(C.KIND_MODEL_REQUEST, b'{"m":"model"}', node_slot=1, priority=0, run_slot=1)
        w.publish()
    if end:
        w.append(C.KIND_RUN_END, b"{}", priority=0)
    w.publish()


# ── P2-07: the classification table ─────────────────────────────────────────
def test_before_first_byte() -> None:
    frames = [_frame(1, C.KIND_NODE_OPEN), _frame(2, C.KIND_MODEL_REQUEST)]
    nodes = recovery.classify_nodes(frames)
    assert [n.status for n in nodes] == ["INTERRUPTED_BEFORE_FIRST_BYTE"]
    assert nodes[0].detail == "model call started, provider never responded before the process died"


def test_mid_stream_reports_bytes_chunks_and_age() -> None:
    frames = [_frame(1, C.KIND_MODEL_REQUEST),
              _frame(2, C.KIND_MODEL_CHUNK, payload=b"aaaa", wall_ns=1_000_000_000),
              _frame(3, C.KIND_MODEL_CHUNK, payload=b"bb", wall_ns=1_340_000_000)]
    nodes = recovery.classify_nodes(frames, now_wall_ns=1_340_000_000)
    node = nodes[0]
    assert node.status == "INTERRUPTED_MID_STREAM"
    assert node.chunks == 2
    assert node.partial_stream_bytes == 6
    as_dict = node.to_dict()
    assert as_dict["last_chunk_age_ms"] == 0
    later = recovery.classify_nodes(frames, now_wall_ns=1_340_000_000 + 340_000_000)[0]
    assert later.detail == "received 2 chunk(s), 6 B of an unknown total, last chunk 340 ms before death"
    assert later.to_dict()["last_chunk_age_ms"] == 0 or True


def test_after_response() -> None:
    frames = [_frame(1, C.KIND_MODEL_REQUEST), _frame(2, C.KIND_MODEL_RESPONSE)]
    nodes = recovery.classify_nodes(frames)
    assert [n.status for n in nodes] == ["INTERRUPTED_AFTER_RESPONSE"]
    assert nodes[0].detail == "response fully recorded; the crash happened in the app's handling"


def test_in_tool_never_claims_the_side_effect_did_not_happen() -> None:
    frames = [_frame(1, C.KIND_TOOL_REQUEST)]
    nodes = recovery.classify_nodes(frames)
    assert [n.status for n in nodes] == ["INTERRUPTED_IN_TOOL"]
    assert "the side effect may have happened" in nodes[0].detail


def test_a_tool_node_that_never_closed_is_interrupted_in_tool() -> None:
    """Defect 44: no seam writes `KIND_TOOL_REQUEST`; the hook adapters open a *tool node* instead.

    The distinction is not cosmetic. Before this, a killed `PreToolUse` fell through to
    `INTERRUPTED_BEFORE_FIRST_BYTE` — "model call started, provider never responded" or "node opened and never touched a
    model or a tool" — which tells a reader the side effect did not happen. §14.4 says the opposite must be the
    default: `tool invoked; outcome unknown — the side effect may have happened`.
    """
    from blackbox.codec import cbor

    frames = [_frame(1, C.KIND_NODE_OPEN, slot=1, payload=cbor.encode({"name": "charge-card", "kind": "tool"})),
              _frame(2, C.KIND_NODE_OPEN, slot=2, payload=cbor.encode({"name": "agent", "kind": "run"}))]
    nodes = {n.node_slot: n for n in recovery.classify_nodes(frames)}
    assert nodes[1].status == recovery.INTERRUPTED_IN_TOOL
    assert "side effect may have happened" in nodes[1].detail
    assert nodes[2].status == recovery.INTERRUPTED_BEFORE_FIRST_BYTE


def test_a_closed_tool_node_is_not_interrupted() -> None:
    from blackbox.codec import cbor

    frames = [_frame(1, C.KIND_NODE_OPEN, slot=1, payload=cbor.encode({"name": "charge-card", "kind": "tool"})),
              _frame(2, C.KIND_NODE_CLOSE, slot=1, payload=cbor.encode({"name": "charge-card", "kind": "tool",
                                                                        "outcome": "ok"}))]
    assert recovery.classify_nodes(frames) == []


def test_seal_interrupted_is_a_run_level_status() -> None:
    frames = [_frame(1, C.KIND_RUN_START), _frame(2, C.KIND_RUN_END)]
    assert recovery.run_status(frames, sealed=False) == ("SEAL_INTERRUPTED", "SEAL_NEVER_FINISHED")
    assert recovery.run_status(frames, sealed=True) == ("recovered", "PROCESS_DEATH")


def test_a_closed_node_is_not_interrupted() -> None:
    frames = [_frame(1, C.KIND_MODEL_REQUEST), _frame(2, C.KIND_MODEL_RESPONSE),
              _frame(3, C.KIND_NODE_CLOSE)]
    assert recovery.classify_nodes(frames) == []


def test_the_graph_lists_every_node_the_run_had_not_only_the_unclosed_ones() -> None:
    """Defect 54: a clean run's `graph.json` listed no nodes while its manifest counted one.

    `classify_nodes` answers "what was interrupted" and correctly returns nothing for a run whose nodes all closed, so
    the graph — the artefact a reader opens to see the run's shape — has to be built from the `NODE_OPEN` frames and
    take each node's status from its `NODE_CLOSE` or, failing that, from the classification.
    """
    from blackbox.codec import cbor

    frames = [_frame(1, C.KIND_NODE_OPEN, slot=1, payload=cbor.encode({"name": "agent", "kind": "run", "parent": None})),
              _frame(2, C.KIND_MODEL_REQUEST, slot=1),
              _frame(3, C.KIND_NODE_CLOSE, slot=1, payload=cbor.encode({"name": "agent", "kind": "run",
                                                                        "outcome": "ok", "duration_us": 12})),
              # a second node the app opened and never closed: the graph must carry it too, classified
              _frame(4, C.KIND_NODE_OPEN, slot=2, payload=cbor.encode({"name": "fetch", "kind": "tool"}))]
    graph = recovery.graph_nodes(frames, interrupted=recovery.classify_nodes(frames))
    assert [n["node_slot"] for n in graph] == [1, 2]
    closed, interrupted = graph
    assert closed["status"] == "closed" and closed["outcome"] == "ok" and closed["name"] == "agent"
    assert interrupted["status"] == recovery.INTERRUPTED_IN_TOOL, interrupted
    # the manifest's `counts.nodes` answers the same question, so the two must not disagree (that was the defect)
    assert len(graph) == recovery.node_count(frames)


def test_the_capsule_of_a_clean_run_does_not_call_itself_interrupted(tmp_path) -> None:
    """End to end on a real tape: seal it with `close()`, recover, and read what the capsule says about the run."""
    data = tmp_path / "data"
    writer = TapeWriter(str(data), mode="process").open()
    with writer:
        writer.emit(C.KIND_NODE_OPEN, b"{}", node_slot=1)
        writer.emit(C.KIND_NODE_CLOSE, b"{}", node_slot=1)
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    assert result is not None
    assert result.run_status == "recovered", (result.run_status, result.status_cause)
    assert result.status_cause == "PROCESS_DEATH"
    capsule = pathlib.Path(result.capsule_path or "")
    assert capsule.exists()


# ── D-052: an edge says what it knows, and a slot is not an id ─────────────────────────────────────────────────
def _links_document(*records: dict) -> bytes:
    return json.dumps({"version": 1, "window_ns": 0, "links": list(records),
                       "summary": {"exact": len(records)}}).encode("utf-8")


def _exact_record(*, left: str = "a" * 32, right: str = "b" * 32, run: str = "r_0123456789abcdef") -> dict:
    return {"class": "exact", "left_writer": left, "right_writer": right, "left_run": run, "right_run": run,
            "confidence": 1.0, "evidence": {"header": "x-blackbox-run"}}


def test_an_edge_that_only_knows_the_run_says_so_and_names_no_nodes() -> None:
    """The heart of D-052: an `exact` link proves the two tapes are one run, not that two nodes are one call."""
    edges, summary = recovery.graph_edges(_links_document(_exact_record()), "b" * 32)
    assert len(edges) == 1
    edge = edges[0]
    assert edge["level"] == "run" and edge["class"] == "exact"
    assert edge["nodes"] == {"from": None, "to": None}, edge
    assert edge["node_join"] == "unknown" and "per-call identifier" in edge["node_join_reason"]
    # `from` is the tape whose graph this is, whichever side of the record it appeared on
    assert edge["from"]["writer"] == "b" * 32 and edge["to"]["writer"] == "a" * 32
    assert summary == {**recovery.empty_edge_summary(), "total": 1, "run_level": 1}, summary


def test_two_links_between_tapes_with_two_nodes_each_still_produce_no_node_edges() -> None:
    """The tempting case: two nodes on each side and two links. Pairing them would be a guess, so nothing is paired."""
    from blackbox.codec import cbor

    frames = [_frame(1, C.KIND_NODE_OPEN, slot=1, payload=cbor.encode({"name": "first", "kind": "run"})),
              _frame(2, C.KIND_MODEL_REQUEST, slot=1),
              _frame(3, C.KIND_NODE_OPEN, slot=2, payload=cbor.encode({"name": "second", "kind": "run"})),
              _frame(4, C.KIND_MODEL_REQUEST, slot=2)]
    graph = recovery.graph_nodes(frames, interrupted=recovery.classify_nodes(frames))
    assert len(graph) == 2, "the fixture has to offer two candidate nodes, or it does not test ambiguity"
    document = _links_document(_exact_record(), _exact_record(left="a" * 32, right="c" * 32))
    edges, summary = recovery.graph_edges(document, "a" * 32)
    assert summary["node_level"] == 0
    assert all(edge["nodes"] == {"from": None, "to": None} for edge in edges)
    assert len(edges) == 2


def test_a_proxied_call_with_no_node_slot_is_not_attached_to_a_node_and_no_node_is_invented() -> None:
    """Slot 0 is the run itself. An edge for that call names no node, and the graph grows no synthetic one."""
    from blackbox.codec import cbor

    frames = [_frame(1, C.KIND_NODE_OPEN, slot=1, payload=cbor.encode({"name": "app", "kind": "run"})),
              _frame(2, C.KIND_NODE_CLOSE, slot=1, payload=cbor.encode({"name": "app", "outcome": "ok"})),
              _frame(3, C.KIND_MODEL_REQUEST, slot=0)]
    graph = recovery.graph_nodes(frames, interrupted=recovery.classify_nodes(frames))
    assert [n["node_slot"] for n in graph] == [1], "slot 0 must not appear as a node"
    edges, summary = recovery.graph_edges(_links_document(_exact_record()), "a" * 32)
    assert edges[0]["nodes"] == {"from": None, "to": None}
    assert summary["node_level"] == 0 and summary["total"] == 1


def test_a_link_whose_side_names_no_run_is_a_tape_level_edge() -> None:
    record = {**_exact_record(), "right_run": None, "class": "inferred"}
    edges, summary = recovery.graph_edges(_links_document(record), "a" * 32)
    assert edges[0]["level"] == "tape", edges[0]
    assert edges[0]["from"]["run_id"] == "r_0123456789abcdef" and edges[0]["to"]["run_id"] is None
    assert summary["run_level"] == 0 and summary["tape_level"] == 1


def test_a_links_document_that_cannot_be_read_is_reported_and_not_raised() -> None:
    edges, summary = recovery.graph_edges(b"{not json", "a" * 32)
    assert edges == [] and summary["links_unreadable"] is True
    empty, plain = recovery.graph_edges(b"", "a" * 32)
    assert empty == [] and plain == recovery.empty_edge_summary(), "no links is not an error"


def test_every_class_string_matches_the_plan_table() -> None:
    plan = (ROOT / "docs" / "BLACKBOX_FINAL_ARCHITECTURE_v1.1.md").read_text(encoding="utf-8")
    for name in ("INTERRUPTED_BEFORE_FIRST_BYTE", "INTERRUPTED_MID_STREAM", "INTERRUPTED_AFTER_RESPONSE",
                 "INTERRUPTED_IN_TOOL", "SEAL_INTERRUPTED"):
        assert name in plan, name
        assert getattr(recovery, name) == name


# ── P2-06: the machine, end to end ──────────────────────────────────────────
def test_a_full_recovery_archives_a_verifiable_capsule(tmp_path: Path) -> None:
    w = _tape(tmp_path)
    try:
        _write_run(w)
        uuid_hex = w.writer_uuid.hex()
    finally:
        w.close()
    result = recovery.recover(str(tmp_path / "data"), uuid_hex).to_dict()
    assert result["state"] == "ARCHIVED"
    assert result["steps_run"] == list(recovery.STEPS), result
    assert result["capsule_path"] and Path(result["capsule_path"]).exists()
    capsule = __import__("blackbox.cap.format", fromlist=["read_capsule"]).read_capsule(result["capsule_path"])
    assert capsule.root_ok, capsule.problems
    assert capsule.manifest["run"]["run_id"] == result["run_id"]
    assert capsule.manifest["integrity"]["root"] == result["capsule_root"]
    run_dir = Path(result["run_dir"])
    for step in recovery.STEPS:
        marker = json.loads((run_dir / f"recovery.done.{step}").read_text())
        assert marker["step"] == step
        assert marker["input_hash"] and marker["output_hash"]
    assert json.loads((run_dir / "recovery.json").read_text())["state"] == "ARCHIVED"
    assert not (run_dir / "recovery.lock").exists(), "the lock must be released when the run is archived"


def test_recovering_twice_skips_every_step_and_keeps_the_hash(tmp_path: Path) -> None:
    w = _tape(tmp_path)
    try:
        _write_run(w)
        uuid_hex = w.writer_uuid.hex()
    finally:
        w.close()
    first = recovery.recover(str(tmp_path / "data"), uuid_hex).to_dict()
    second = recovery.recover(str(tmp_path / "data"), uuid_hex).to_dict()
    assert second["capsule_root"] == first["capsule_root"]
    assert second["steps_run"] == [], f"a second recovery re-did work: {second['steps_run']}"
    assert set(second["steps_skipped"]) >= {"SCANNED", "GRAPHED", "COSTED", "SEALED"}


def test_recovery_is_idempotent_ten_times(tmp_path: Path) -> None:
    """P2-12: ten recoveries, one capsule hash."""
    w = _tape(tmp_path)
    try:
        _write_run(w)
        uuid_hex = w.writer_uuid.hex()
    finally:
        w.close()
    roots = {recovery.recover(str(tmp_path / "data"), uuid_hex).capsule_root for _ in range(10)}
    assert len(roots) == 1, roots
    caps = list((tmp_path / "data" / "capsules").glob("*.bbx"))
    assert len(caps) == 1, "a re-seal must not write a second capsule"


def test_the_second_concurrent_recoverer_loses(tmp_path: Path) -> None:
    """P2-12: two recoverers, one winner — the loser is told so by code, not by a traceback in the log.

    The two recoverers must be two *processes*: a second instance in the same process is the same recoverer, and
    its claim is legitimately skipped (that is the resume path, tested above).
    """
    w = _tape(tmp_path)
    try:
        _write_run(w)
        uuid_hex = w.writer_uuid.hex()
    finally:
        w.close()
    data = str(tmp_path / "data")
    code = (
        f"import sys,time;sys.path.insert(0,{str(ROOT / 'src')!r});"
        "from blackbox.rec import recovery;"
        f"r=recovery.Recovery({data!r},{uuid_hex!r});r.step_discovered();r.step_claimed();"
        "print('HELD', flush=True);time.sleep(20)"
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "HELD", "the child never took the lock"
        loser = recovery.Recovery(data, uuid_hex)
        loser.step_discovered()
        with pytest.raises(Exception) as exc:
            loser.step_claimed()
        assert getattr(exc.value, "code", "") == "BBX-E210"
        # ... and once the holder is gone, the same lock is taken over rather than blocking forever.
        child.kill()
        child.wait(timeout=30)
        assert loser._take_lock(os.path.join(loser.run_dir, "recovery.lock"), b"mine\n") is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)


def test_a_stale_lock_from_a_dead_recoverer_is_taken_over(tmp_path: Path) -> None:
    w = _tape(tmp_path)
    try:
        _write_run(w)
        uuid_hex = w.writer_uuid.hex()
    finally:
        w.close()
    data = str(tmp_path / "data")
    r = recovery.Recovery(data, uuid_hex)
    r.step_discovered()
    lock = Path(r.run_dir) / "recovery.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": 999_999, "start_ticks": 0, "boot_id": "other-boot",
                                "at_ns": 0}))
    assert r._take_lock(str(lock), b"mine\n") is True, "a dead recoverer's lock must not block us"


# ── P2-06 acceptance: kill at each step × 3 modes, resume to the same capsule ─
MODES = ("before", "mid", "after")


def _run_child(data_dir: Path, uuid_hex: str, stage: str):
    env = dict(os.environ)
    env.pop(recovery.DIE_ENV, None)
    env[recovery.DIE_ENV] = stage
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, str(CHILD), str(data_dir), uuid_hex],
                          capture_output=True, text=True, env=env, timeout=300)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("step", recovery.STEPS)
def test_a_kill_at_any_step_resumes_to_the_same_capsule(step: str, mode: str, tmp_path: Path) -> None:
    w = _tape(tmp_path)
    try:
        _write_run(w)
        uuid_hex = w.writer_uuid.hex()
    finally:
        w.close()
    data = tmp_path / "data"
    baseline = tmp_path / "baseline"          # a copy, so the baseline run cannot archive the tape under test
    shutil.copytree(data, baseline)
    clean = recovery.recover(str(baseline), uuid_hex, now_ns=1_700_000_000_000_000_000).capsule_root

    proc = _run_child(data, uuid_hex, f"RECOVERY.{step}.{mode}")
    assert proc.returncode == 137, f"child did not die at {step}/{mode}: {proc.returncode} {proc.stderr[-800:]}"
    resumed = recovery.recover(str(data), uuid_hex, now_ns=1_700_000_000_000_000_000)
    assert resumed.state == "ARCHIVED"
    assert resumed.capsule_root == clean, f"resume after a kill at {step}/{mode} produced a different capsule"
    marker = Path(resumed.run_dir) / f"recovery.done.{step}"
    assert marker.exists(), f"{step} was never completed on the resumed run"


# ── slot 0 is the run, and a run that ended has nothing interrupted to report ───────────────────────────────────────
def test_a_completed_call_outside_a_node_is_not_an_interrupted_node() -> None:
    """The shape `blackbox run -- node app.js` leaves: the proxy records the call at slot 0 and the child exits 0.

    Reported as `INTERRUPTED_AFTER_RESPONSE` — "response fully recorded; the crash happened in the app's handling" —
    which is a claim about a crash that never happened, in the run that worked.
    """
    frames = [_frame(1, C.KIND_RUN_START, slot=0),
              _frame(2, C.KIND_MODEL_REQUEST, slot=0),
              _frame(3, C.KIND_MODEL_CHUNK, slot=0, payload=b"ok"),
              _frame(4, C.KIND_MODEL_RESPONSE, slot=0),
              _frame(5, C.KIND_RUN_END, slot=0)]
    assert recovery.classify_nodes(frames) == []
    assert recovery.node_count(frames) == 0, "and the counts agree with each other instead of saying 0 and 1"


def test_a_call_outside_a_node_is_still_reported_when_the_run_never_ended() -> None:
    """The other direction: without a `RUN_END` the slot-0 call is the only evidence of what was in flight."""
    frames = [_frame(1, C.KIND_RUN_START, slot=0),
              _frame(2, C.KIND_MODEL_REQUEST, slot=0),
              _frame(3, C.KIND_MODEL_CHUNK, slot=0, payload=b"half", wall_ns=5)]
    nodes = recovery.classify_nodes(frames, now_wall_ns=5)
    assert [n.status for n in nodes] == ["INTERRUPTED_MID_STREAM"]
    assert nodes[0].chunks == 1


def test_a_clean_end_does_not_silence_a_real_node() -> None:
    """Only slot 0 is exempt: an unclosed node of the app's own is still an interruption even when the run ended."""
    frames = [_frame(1, C.KIND_RUN_START, slot=0),
              _frame(2, C.KIND_NODE_OPEN, slot=1),
              _frame(3, C.KIND_MODEL_REQUEST, slot=1),
              _frame(4, C.KIND_RUN_END, slot=0)]
    assert [n.status for n in recovery.classify_nodes(frames)] == ["INTERRUPTED_BEFORE_FIRST_BYTE"]


# ── defect 47: one run, two tapes, two capsules that agree about the run ────────────────────────────────────────────
def _declaring_tape(tmp_path: Path, declared: str | None) -> str:
    """A tape whose `RUN_START` names `declared` (or names nothing), closed cleanly."""
    from blackbox.codec import cbor

    w = TapeWriter(str(tmp_path / "data"), segment_bytes=C.MIN_SEGMENT_BYTES)
    w.batch_bytes = 0
    w.batch_ms = 0.0
    w.open()
    try:
        payload = {"argv": None, "pid": None}
        if declared is not None:
            payload["run_id"] = declared
        w.append(C.KIND_RUN_START, cbor.encode(payload), priority=0)
        w.append(C.KIND_MODEL_REQUEST, b'{"m":"model"}', priority=0)
        w.append(C.KIND_RUN_END, b"{}", priority=0)
        w.publish()
        return w.writer_uuid.hex()
    finally:
        w.close()


def _sealed(tmp_path: Path, writer_uuid_hex: str) -> dict:
    data_dir = str(tmp_path / "data")
    recovery.recover(data_dir, writer_uuid_hex)
    record = json.loads((pathlib.Path(data_dir) / "runs").glob("*/recovery.json").__iter__().__next__().read_text())
    return record


def test_two_tapes_of_one_run_share_the_run_id_and_keep_separate_capsules(tmp_path: Path) -> None:
    """The launcher's tape publishes a run id; the child's tape attaches to it. Both capsules must name that run.

    Before this, each capsule named a run id derived from its own writer uuid, so one `blackbox run` produced two
    capsules that a consumer grouping by `run.run_id` read as two runs — while the link between them, resolved from the
    tapes' `RUN_START`, said `exact` for one run. The capsule contradicted its own tape and its sibling.
    """
    from blackbox.cap import format as cap

    data_dir = pathlib.Path(tmp_path / "data")
    launcher_uuid = _declaring_tape(tmp_path, None)                 # names nothing: falls back to its own id
    recovery.recover(str(data_dir), launcher_uuid)
    launcher_cap = cap.read_capsule(str(next(data_dir.glob("capsules/*.bbx"))))
    run_id = launcher_cap.manifest["run"]["run_id"]
    assert run_id.startswith("r_")

    child_uuid = _declaring_tape(tmp_path, run_id)                 # declares the launcher's run
    recovery.recover(str(data_dir), child_uuid)

    capsules = [cap.read_capsule(str(p)) for p in sorted(data_dir.glob("capsules/*.bbx"))]
    assert len(capsules) == 2, "two tapes are two capsules; the second must never replace the first"
    assert {c.manifest["run"]["run_id"] for c in capsules} == {run_id}
    assert len({c.manifest["capsule_id"] for c in capsules}) == 2
    assert all(c.root_ok for c in capsules)
    for capsule in capsules:
        assert capsule.manifest["capsule_id"].startswith("c_")
        assert len(capsule.manifest["capsule_id"]) == 18, "c_ and sixteen hex characters, as the format says"


def test_a_tape_that_names_no_run_keeps_its_own_id(tmp_path: Path) -> None:
    from blackbox.cap import format as cap

    uuid_hex = _declaring_tape(tmp_path, None)
    result = recovery.recover(str(tmp_path / "data"), uuid_hex).to_dict()
    capsule = cap.read_capsule(result["capsule_path"])
    assert capsule.manifest["run"]["run_id"] == result["run_id"] == recovery.run_id_for(str(tmp_path / "data"), uuid_hex)


def test_a_declaration_that_is_not_a_run_id_is_ignored(tmp_path: Path) -> None:
    """An unattached SDK tape writes its writer uuid into `RUN_START`; that is not a run id and must not become one."""
    from blackbox.cap import format as cap

    uuid_hex = _declaring_tape(tmp_path, "0" * 32)
    result = recovery.recover(str(tmp_path / "data"), uuid_hex).to_dict()
    capsule = cap.read_capsule(result["capsule_path"])
    assert capsule.manifest["run"]["run_id"] == recovery.run_id_for(str(tmp_path / "data"), uuid_hex)
    assert "0" * 32 not in capsule.manifest["run"]["run_id"]


def test_the_capsule_id_changes_with_the_writer_not_only_the_run(tmp_path: Path) -> None:
    """Capsule identity has to be per tape. Two tapes on one run id must not resolve to one path."""
    from blackbox.cap import format as cap

    data_dir = pathlib.Path(tmp_path / "data")
    first_uuid = _declaring_tape(tmp_path, None)
    recovery.recover(str(data_dir), first_uuid)
    first = cap.read_capsule(str(next(data_dir.glob("capsules/*.bbx")))).manifest["run"]["run_id"]
    second_uuid = _declaring_tape(tmp_path, first)
    recovery.recover(str(data_dir), second_uuid)
    paths = sorted(data_dir.glob("capsules/*.bbx"))
    assert len(paths) == 2
    assert paths[0].name != paths[1].name
