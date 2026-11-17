"""D-053: the graph ships inside the capsule, and the capsule can prove it was not edited.

`graph.json` is a *derived* part: nodes come from the frames, edges from `links.json`, both of which the capsule already
carries. These tests are about the two halves of that claim — that a real recovery puts it in, and that a tampered one is
reported as `BBX-E412` while the capsule is left alone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import derived
from blackbox.cap import format as cap
from blackbox.cap.cli import main as capsule_main
from blackbox.codec import cbor
from blackbox.rec import recovery
from blackbox.tape import writer as writer_mod


def _capsule(tmp_path: Path) -> tuple[Path, dict]:
    """A real tape, recovered the way the launcher recovers it: two nodes, one call, sealed."""
    data = tmp_path / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1).open()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": "r_0123456789abcdef"}), node_slot=0)
    writer.emit(C.KIND_NODE_OPEN, cbor.encode({"name": "agent", "kind": "run"}), node_slot=1)
    writer.emit(C.KIND_MODEL_REQUEST, json.dumps({"provider": "openai", "path": "/v1/chat/completions",
                                                  "request_fp": "abc", "call_id": "c0ffee"}).encode(), node_slot=1)
    writer.emit(C.KIND_NODE_CLOSE, cbor.encode({"name": "agent", "outcome": "ok"}), node_slot=1)
    writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": "r_0123456789abcdef"}), node_slot=0)
    writer.close()
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    assert result is not None and result.capsule_path
    path = Path(result.capsule_path)
    return path, cap.read_capsule(str(path)).manifest


def test_the_capsule_carries_its_graph_as_a_declared_derived_part(tmp_path):
    path, manifest = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    assert cap.GRAPH_NAME in capsule.entries
    graph = json.loads(capsule.entry(cap.GRAPH_NAME))
    assert [node["name"] for node in graph["nodes"]] == ["agent"]
    assert graph["nodes"][0]["status"] == "closed"
    parts = manifest["derived_parts"]
    # Three derived parts since P4-14, declared in name order so the manifest is a function of its inputs only.
    assert [part["name"] for part in parts] == sorted([cap.EVENTS_NAME, cap.GRAPH_NAME, cap.OTLP_NAME])
    declared = {part["name"]: part for part in parts}
    for name in (cap.EVENTS_NAME, cap.GRAPH_NAME, cap.OTLP_NAME):
        assert declared[name]["sha256"] == __import__("hashlib").sha256(capsule.entry(name)).hexdigest(), name
        assert declared[name]["generator"].startswith("blackbox "), name
    report = derived.verify(capsule)
    assert report["ok"] and report["drift"] == [], report
    assert report["parts"][0]["declared"] is True


def test_a_graph_edited_after_the_capsule_was_written_is_reported_as_e412(tmp_path):
    """The point of a derived part: a graph nobody can recompute is a graph nobody can trust."""
    path, manifest = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    graph = json.loads(capsule.entry(cap.GRAPH_NAME))
    graph["nodes"].append({"node_slot": 99, "name": "invented", "kind": "run", "status": "closed"})
    graph["edges"].append({"class": "exact", "level": "node", "nodes": {"from": 1, "to": 99}})
    tampered = tmp_path / "tampered.bbx"
    entries = {name: blob for name, blob in capsule.entries.items() if name not in
               (cap.MANIFEST_NAME, cap.INTEGRITY_NAME)}
    entries[cap.GRAPH_NAME] = json.dumps(graph, sort_keys=True).encode()
    # the manifest is kept as it was, so `derived_parts` still names the hash of the *original* graph
    cap.write_capsule(str(tampered), entries, {**manifest, "capsule_id": "c_tampered"})
    check = cap.read_capsule(str(tampered))
    report = derived.verify(check)
    assert not report["ok"], report
    fields = {drift["field"] for drift in report["drift"]}
    assert {"sha256", "nodes", "edges"} <= fields, fields
    assert cap.read_capsule(str(tampered)).entry(cap.GRAPH_NAME) == entries[cap.GRAPH_NAME], \
        "verification reports; it does not rewrite"


def test_a_capsule_written_before_the_graph_existed_still_verifies(tmp_path):
    """Format compatibility in the direction that matters most: old capsules keep opening and keep passing."""
    path, manifest = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    entries = {name: blob for name, blob in capsule.entries.items()
               if name not in (cap.MANIFEST_NAME, cap.INTEGRITY_NAME, cap.GRAPH_NAME, cap.EVENTS_NAME,
                               cap.OTLP_NAME)}
    old_manifest = {key: value for key, value in manifest.items() if key != "derived_parts"}
    older = tmp_path / "older.bbx"
    cap.write_capsule(str(older), entries, old_manifest)
    check = cap.read_capsule(str(older))
    report = derived.verify(check)
    assert report["ok"] and report["drift"] == []
    # Nothing derived, nothing declared: an old capsule is reported absent and passes, which is the compatibility
    # rule in the direction that matters — a new build must not call an old capsule corrupt.
    assert report["absent"] and cap.GRAPH_NAME in report["absent"][0]
    assert capsule_main(["verify", str(path)]) == 0


def test_the_cli_exit_codes_split_drift_from_an_unreadable_capsule(tmp_path, capsys):
    path, manifest = _capsule(tmp_path)
    assert capsule_main(["verify", str(path)]) == 0
    assert "derived parts verify" in capsys.readouterr().out
    capsule = cap.read_capsule(str(path))
    graph = json.loads(capsule.entry(cap.GRAPH_NAME))
    graph["open_nodes"] = 7                                   # a number no scan of these frames can produce
    entries = {name: blob for name, blob in capsule.entries.items() if name not in
               (cap.MANIFEST_NAME, cap.INTEGRITY_NAME)}
    entries[cap.GRAPH_NAME] = json.dumps(graph, sort_keys=True).encode()
    drifted = tmp_path / "drifted.bbx"
    cap.write_capsule(str(drifted), entries, {**manifest, "capsule_id": "c_drifted"})
    assert capsule_main(["verify", str(drifted)]) == 1
    assert "BBX-E412" in capsys.readouterr().out
    assert capsule_main(["verify", str(tmp_path / "nope.bbx")]) == 2
    capsys.readouterr()


def test_json_output_carries_the_report(tmp_path, capsys):
    path, _manifest = _capsule(tmp_path)
    assert capsule_main(["verify", str(path), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    names = [part["name"] for part in body["parts"]]
    assert body["ok"] is True and names == sorted([cap.EVENTS_NAME, cap.GRAPH_NAME, cap.OTLP_NAME]), names


@pytest.mark.parametrize("field", ["nodes", "edges", "edge_summary", "open_nodes", "frames"])
def test_every_compared_field_is_one_a_recomputation_can_produce(field):
    """A guard on the comparison list: each name has to exist in what `recompute` returns, or it checks nothing."""
    assert field in derived.COMPARED
    source = Path(derived.__file__).read_text(encoding="utf-8")
    assert f'"{field}"' in source.split("def recompute")[1].split("def _has_seg_end")[0]
