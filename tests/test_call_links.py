"""D-053: one call, one id, on both sides — and what the graph may then claim.

The unit cases here are the three ways this can go wrong: two concurrent calls (must stay two), one id on both sides
twice (ambiguous, so nothing is named), and an id on one side only (an absence, not a link). The real-run case lives in
`tests/test_launcher_proxy.py`, where an actual child, an actual proxy and an actual origin are involved.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.rec import correlate, links, recovery
from blackbox.tape import writer as writer_mod


def _call(writer_uuid: str, *, call_id: str | None, node: int | None = 0, run: str | None = "r_0123456789abcdef") -> links.CallView:
    return links.CallView(writer_uuid=writer_uuid, vantage="sdk", wall_ns=1, fingerprint="fp", run_id=run,
                          node_slot=node, call_id=call_id)


def _view(writer_uuid: str, calls: list[links.CallView], *, run: str | None = "r_0123456789abcdef") -> links.RunView:
    return links.RunView.of(writer_uuid, "sdk", run, calls)


# ── resolve_calls: the id is the only thing matched ───────────────────────────────────────────────────────────────
def test_one_id_written_by_both_sides_is_one_call_link_naming_its_nodes():
    """Two nodes, one call: the SDK's node and the proxy's record of the same request — a `call` edge, not a guess."""
    sdk = _view("a", [_call("a", call_id="c1", node=3)])
    proxy = _view("b", [_call("b", call_id="c1", node=0)])
    records = links.resolve_calls(sdk, proxy)
    assert len(records) == 1
    record = records[0]
    assert record.call_id == "c1" and not record.ambiguous
    assert (record.left_node, record.right_node) == (3, None), "slot 0 is the run, not a node"
    assert record.level == "call", "the proxy recorded no node, so this is not a node-to-node edge"


def test_two_concurrent_calls_stay_two_links_each_with_its_own_node():
    """The concurrency case: two ids, two nodes, and no crossing over. This is what a time window cannot do."""
    sdk = _view("a", [_call("a", call_id="c1", node=1), _call("a", call_id="c2", node=2)])
    proxy = _view("b", [_call("b", call_id="c1"), _call("b", call_id="c2")])
    records = sorted(links.resolve_calls(sdk, proxy), key=lambda record: record.call_id)
    assert [(r.call_id, r.left_node) for r in records] == [("c1", 1), ("c2", 2)]


def test_one_id_recorded_twice_on_a_side_is_ambiguous_and_names_no_node():
    """A repeated id is not something to resolve by picking the first pair: the count is written down instead."""
    sdk = _view("a", [_call("a", call_id="c1", node=1), _call("a", call_id="c1", node=2)])
    proxy = _view("b", [_call("b", call_id="c1")])
    records = links.resolve_calls(sdk, proxy)
    assert len(records) == 1
    record = records[0]
    assert record.ambiguous and record.candidates == 2
    assert (record.left_node, record.right_node) == (None, None)
    assert record.level == "call"


def test_an_id_on_one_side_only_is_not_a_link():
    """No proxy in front means no second record; that is an absence, and absences are not edges."""
    sdk = _view("a", [_call("a", call_id="c1", node=1)])
    other = _view("b", [_call("b", call_id=None)])
    assert links.resolve_calls(sdk, other) == []


def test_two_calls_with_no_ids_are_not_matched_by_fingerprint_or_time():
    sdk = _view("a", [_call("a", call_id=None)])
    other = _view("b", [_call("b", call_id=None)])
    assert links.resolve_calls(sdk, other) == []


# ── the resolver has to be able to read the frames it is resolving ────────────────────────────────────────────────
def test_the_resolver_reads_json_model_request_payloads(tmp_path):
    """Defect 57: `MODEL_REQUEST` is JSON, and the resolver decoded CBOR — so `call_id` was never visible at all."""
    data = str(tmp_path)
    writer = writer_mod.TapeWriter(data, mode="process", vantage=1).open()
    payload = json.dumps({"provider": "openai", "path": "/v1/chat/completions", "request_fp": "abc123",
                          "linked_run": "r_0123456789abcdef", "call_id": "c0ffee"}).encode()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": "r_0123456789abcdef"}), node_slot=0)
    writer.emit(C.KIND_MODEL_REQUEST, payload, node_slot=7)
    writer.close()
    view = correlate.views(data)[0]
    assert len(view.calls) == 1
    call = view.calls[0]
    assert call.call_id == "c0ffee" and call.fingerprint == "abc123"
    assert call.run_id == "r_0123456789abcdef" and call.node_slot == 7


def test_the_links_document_keeps_the_tape_keys_and_adds_the_call_keys():
    """Format compatibility: a reader that knows `links`/`summary` sees exactly the same shapes it did before."""
    tape = links.Link(klass=links.EXACT, left_writer="a", right_writer="b", left_run="r1", right_run="r1",
                      confidence=1.0, evidence={"rule": "same run id"})
    call = links.CallLink(left_writer="a", right_writer="b", call_id="c1", left_run="r1", right_run="r1",
                          left_node=2, right_node=None)
    body = json.loads(correlate.document([tape], call_links=[call]))
    assert body["version"] == 1
    assert body["links"][0]["class"] == links.EXACT and body["summary"] == links.summary([tape])
    assert body["call_links"][0]["call_id"] == "c1" and body["call_summary"] == {"total": 1, "node_level": 0,
                                                                                 "call_level": 1, "ambiguous": 0}
    # and a document with no call links still says so rather than omitting the keys
    empty = json.loads(correlate.document([tape]))
    assert empty["call_links"] == [] and empty["call_summary"]["total"] == 0


# ── the graph: what those records become ────────────────────────────────────────────────────────────────────────
def _call_graph(*records: dict, writer: str = "a" * 32) -> tuple[list[dict], dict]:
    document = json.dumps({"version": 1, "links": [], "summary": {}, "call_links": list(records),
                           "call_summary": {}}).encode()
    return recovery.graph_edges(document, writer)


def test_a_call_edge_names_the_call_and_the_node_that_held_it():
    record = links.CallLink(left_writer="a" * 32, right_writer="b" * 32, call_id="c1", left_run="r1", right_run="r1",
                            left_node=4, right_node=None).as_dict()
    edges, summary = _call_graph(record)
    assert len(edges) == 1
    edge = edges[0]
    assert edge["level"] == "call" and edge["from"]["call_id"] == "c1"
    assert edge["nodes"] == {"from": 4, "to": None}, "the tape's own slot, and an honest None on the other side"
    assert edge["node_join"] == "exact"
    assert summary["call_level"] == 1 and summary["node_level"] == 0
    assert summary["node_join"] == "partial", "a node on one end is a partial node join, and says so"


def test_a_node_to_node_edge_appears_only_when_both_sides_have_a_node():
    """The level this build cannot produce today — asserted so that the day it can, it is not a surprise."""
    record = links.CallLink(left_writer="a" * 32, right_writer="b" * 32, call_id="c1", left_run="r1", right_run="r1",
                            left_node=4, right_node=9).as_dict()
    edges, summary = _call_graph(record)
    # `from` is the tape whose graph this is (`a`), whichever side of the record it appeared on
    assert edges[0]["level"] == "node", edges[0]
    assert edges[0]["nodes"] == {"from": 4, "to": 9}, edges[0]["nodes"]
    assert summary["node_level"] == 1 and summary["node_join"] == "exact"


def test_an_ambiguous_call_edge_names_no_node_and_carries_the_candidate_count():
    record = links.CallLink(left_writer="a" * 32, right_writer="b" * 32, call_id="c1", left_run="r1", right_run="r1",
                            left_node=None, right_node=None, ambiguous=True, candidates=2).as_dict()
    edges, summary = _call_graph(record)
    assert edges[0]["node_join"] == "ambiguous" and edges[0]["candidates"] == 2
    assert edges[0]["nodes"] == {"from": None, "to": None}
    assert summary["ambiguous"] == 1 and summary["node_level"] == 0


def test_an_old_document_without_call_keys_still_becomes_the_edges_it_used_to():
    """Byte compatibility in the other direction: a `links.json` from before this change reads as it did."""
    old = json.dumps({"version": 1, "links": [{"class": "exact", "left_writer": "a" * 32, "right_writer": "b" * 32,
                                               "left_run": "r1", "right_run": "r1", "confidence": 1.0,
                                               "evidence": {"rule": "same run id"}}], "summary": {"exact": 1}}).encode()
    edges, summary = recovery.graph_edges(old, "a" * 32)
    assert len(edges) == 1 and edges[0]["level"] == "run"
    assert summary["call_level"] == 0 and summary["node_join"] == "unknown"


@pytest.mark.parametrize("document", [b"", b"{not json"])
def test_a_missing_or_broken_document_never_raises(document):
    edges, summary = recovery.graph_edges(document, "a" * 32)
    assert edges == [] and summary["total"] == 0
