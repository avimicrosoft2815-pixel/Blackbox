"""P4-14 — `otlp/spans.json`: the same frames as OTLP/JSON, and the same discipline as the other exports.

The ticket asks for three things and this file tests each of them where it can be tested:

* **spans validate against a JSON-schema snapshot** — `tests/schemas/otlp-spans.schema.json`, checked by the small
  validator in `tests/_schema.py` (it raises on a keyword it does not implement, so the snapshot cannot quietly grow a
  check nobody runs);
* **the mapping is the pinned semantic conventions** — v1.39.0's `invoke_agent`/`execute_tool`/`chat` names and
  `gen_ai.provider.name`, with our own names wherever the vocabulary has none;
* **it is a derived part** — declared with its sha256, recomputable byte for byte, and an edit is `BBX-E412`.

The importer half of the ticket ("a Phoenix/Langfuse-class importer accepts the file") is a *manual* check by
construction: no importer runs in this sandbox and claiming one did would be the kind of over-claim the rest of this
project exists to avoid. `docs/format.md` records what was verified and what was not.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _schema

from blackbox import constants as C
from blackbox.cap import derived, export_otlp
from blackbox.cap import format as cap
from blackbox.cap.cli import main as capsule_main
from blackbox.codec import cbor
from blackbox.rec import recovery
from blackbox.tape import writer as writer_mod

SCHEMA = json.loads((ROOT / "tests" / "schemas" / "otlp-spans.schema.json").read_text(encoding="utf-8"))


def _request(path: str = "/v1/chat/completions", provider: str = "api.openai.com",
             model: str | None = "gpt-4", call_id: str | None = "c0ffee") -> bytes:
    body: dict[str, object] = {"provider": provider, "path": path}
    if model is not None:
        body["model"] = model
    if call_id is not None:
        body["call_id"] = call_id
    return json.dumps(body).encode()


def _tape(tmp_path: Path, kind: str = "run", *, close_node: bool = True, model: str | None = "gpt-4",
          path: str = "/v1/chat/completions", provider: str = "api.openai.com") -> Path:
    data = tmp_path / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1).open()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": "r_0123456789abcdef"}), node_slot=0)
    writer.emit(C.KIND_NODE_OPEN, cbor.encode({"name": "agent", "kind": kind}), node_slot=1)
    writer.emit(C.KIND_MODEL_REQUEST, _request(path=path, provider=provider, model=model), node_slot=1)
    writer.emit(C.KIND_MODEL_RESPONSE, json.dumps({"provider": provider, "path": path, "model": model, "status": 200,
                                                   "call_id": "c0ffee", "chunks_raw": 3,
                                                   "bytes_received": 42}).encode(), node_slot=1)
    if close_node:
        writer.emit(C.KIND_NODE_CLOSE, cbor.encode({"name": "agent", "outcome": "ok"}), node_slot=1)
    writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": "r_0123456789abcdef"}), node_slot=0)
    writer.close()
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    assert result is not None and result.capsule_path
    return Path(result.capsule_path)


def _document(capsule_path: Path) -> dict:
    capsule = cap.read_capsule(str(capsule_path))
    return json.loads(capsule.entry(cap.OTLP_NAME))


def _spans(document: dict) -> list[dict]:
    return document["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _attributes(span: dict) -> dict:
    """`AnyValue` flattened for assertions; `intValue` is a string on the wire (proto3 JSON), an int here."""
    out: dict = {}
    for item in span["attributes"]:
        (kind, value), = item["value"].items()
        out[item["key"]] = int(value) if kind == "intValue" else value
    return out


def test_the_document_validates_against_the_schema_snapshot(tmp_path):
    path = _tape(tmp_path)
    document = _document(path)
    _schema.validate(document, SCHEMA, root=SCHEMA)
    spans = _spans(document)
    assert [span["name"] for span in spans] == ["bbx.run", "invoke_agent agent", "chat gpt-4"], spans
    assert len({span["spanId"] for span in spans}) == len(spans), "a span id is used once"
    assert spans[0].get("parentSpanId") is None, "the run is the root"
    assert all(span["traceId"] == spans[0]["traceId"] for span in spans)


def test_the_span_names_are_the_pinned_semconv_names(tmp_path):
    """`invoke_agent {gen_ai.agent.name}` and `{gen_ai.operation.name} {gen_ai.request.model}`, v1.39.0."""
    spans = {span["name"]: span for span in _spans(_document(_tape(tmp_path)))}
    agent = _attributes(spans["invoke_agent agent"])
    assert agent["gen_ai.operation.name"] == "invoke_agent" and agent["gen_ai.agent.name"] == "agent"
    chat = _attributes(spans["chat gpt-4"])
    assert chat["gen_ai.operation.name"] == "chat"
    assert chat["gen_ai.provider.name"] == "openai"
    assert chat["gen_ai.request.model"] == "gpt-4"
    assert spans["chat gpt-4"]["kind"] == export_otlp.CLIENT
    assert spans["invoke_agent agent"]["kind"] == export_otlp.INTERNAL
    assert export_otlp.SEMCONV == "v1.39.0" and export_otlp.SEMCONV_COMMIT.startswith("6d05e92")


def test_a_tool_node_becomes_an_execute_tool_span(tmp_path):
    spans = {span["name"]: span for span in _spans(_document(_tape(tmp_path, kind="tool")))}
    tool = _attributes(spans["execute_tool agent"])
    assert tool["gen_ai.operation.name"] == "execute_tool" and tool["gen_ai.tool.name"] == "agent"


def test_a_call_the_mapping_does_not_know_is_not_given_a_semconv_name(tmp_path):
    """The same rule as D-054, one level up: no operation name, so no `{operation} {model}` claim."""
    spans = {span["name"]: span for span in _spans(_document(
        _tape(tmp_path, path="/v1/unknown", provider="127.0.0.1:8123")))}
    assert "bbx.model.call" in spans, list(spans)
    attributes = _attributes(spans["bbx.model.call"])
    assert "gen_ai.operation.name" not in attributes
    assert "gen_ai.provider.name" not in attributes
    assert attributes["gen_ai.request.model"] == "gpt-4", "a model we did record is still recorded"
    assert spans["bbx.model.call"]["kind"] == export_otlp.CLIENT


def test_two_capsules_of_one_run_share_a_trace_and_never_share_a_span_id(tmp_path):
    """The cross-vantage property P3B-03 wants at trace level, and it is not a guess: it comes from the run id."""
    first = _spans(_document(_tape(tmp_path / "one")))
    (tmp_path / "one").mkdir(exist_ok=True)
    second = _spans(_document(_tape(tmp_path / "two")))
    assert first[0]["traceId"] == second[0]["traceId"], "one run, one trace"
    assert {span["spanId"] for span in first}.isdisjoint({span["spanId"] for span in second}), \
        "two capsules of one run must not produce the same span id"


def test_rendering_is_a_function_of_the_capsule(tmp_path):
    path = _tape(tmp_path)
    capsule = cap.read_capsule(str(path))
    frames = derived.frames_of(capsule)
    once = export_otlp.render(frames, capsule_id=str(capsule.manifest["capsule_id"]),
                              run_id=capsule.manifest["run"]["run_id"])
    twice = export_otlp.render(frames, capsule_id=str(capsule.manifest["capsule_id"]),
                               run_id=capsule.manifest["run"]["run_id"])
    assert once == twice == capsule.entry(cap.OTLP_NAME)


def test_no_model_text_reaches_a_span(tmp_path):
    data = tmp_path / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1).open()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": "r_0123456789abcdef"}), node_slot=0)
    secret = b"sk-live-not-a-real-key"
    writer.emit(C.KIND_MODEL_REQUEST, _request(), node_slot=0)
    writer.emit(C.KIND_MODEL_CHUNK, secret, node_slot=0)
    writer.emit(C.KIND_MODEL_RESPONSE, json.dumps({"provider": "api.openai.com", "path": "/v1/chat/completions",
                                                   "model": "gpt-4", "status": 200, "bytes_received": len(secret),
                                                   "chunks_raw": 1}).encode(), node_slot=0)
    writer.close()
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    capsule = cap.read_capsule(result.capsule_path)
    assert secret not in capsule.entry(cap.OTLP_NAME)
    span = _spans(json.loads(capsule.entry(cap.OTLP_NAME)))[-1]
    assert _attributes(span)["bbx.model.bytes_received"] == len(secret), "the counter, not the bytes"


def test_an_unclosed_node_gets_a_span_that_says_it_was_cut_short(tmp_path):
    spans = {span["name"]: span for span in _spans(_document(_tape(tmp_path, close_node=False)))}
    agent = spans["invoke_agent agent"]
    attributes = _attributes(agent)
    assert attributes["bbx.span.truncated"] is True
    assert agent["status"]["code"] == export_otlp.ERROR
    assert agent["status"]["message"] == "the node never closed: no NODE_CLOSE frame"
    assert int(agent["endTimeUnixNano"]) >= int(agent["startTimeUnixNano"])


def test_the_part_is_declared_verified_and_an_edit_is_reported(tmp_path, capsys):
    path = _tape(tmp_path)
    capsule = cap.read_capsule(str(path))
    declared = {part["name"]: part for part in capsule.manifest["derived_parts"]}
    assert cap.OTLP_NAME in declared
    assert capsule_main(["verify", str(path)]) == 0
    out = capsys.readouterr().out
    assert cap.EVENTS_NAME in out and cap.GRAPH_NAME in out and cap.OTLP_NAME in out

    document = json.loads(capsule.entry(cap.OTLP_NAME))
    document["resourceSpans"][0]["scopeSpans"][0]["spans"][1]["name"] = "invoke_agent someone-else"
    entries = {name: blob for name, blob in capsule.entries.items()
               if name not in (cap.MANIFEST_NAME, cap.INTEGRITY_NAME)}
    entries[cap.OTLP_NAME] = json.dumps(document, indent=2, sort_keys=True).encode() + b"\n"
    tampered = tmp_path / "tampered.bbx"
    cap.write_capsule(str(tampered), entries, {**capsule.manifest, "capsule_id": "c_tampered"})
    check = cap.read_capsule(str(tampered))
    report = derived.verify(check)
    assert not report["ok"], report
    drift = [item for item in report["drift"] if item["part"] == cap.OTLP_NAME]
    assert drift and any(item["field"] == "sha256" for item in drift), drift
    assert cap.read_capsule(str(tampered)).entry(cap.OTLP_NAME) == entries[cap.OTLP_NAME], \
        "verification reports; it does not rewrite"


def test_the_schema_validator_refuses_a_document_it_cannot_check():
    """A validator that ignores an unknown keyword would report a pass it never performed."""
    schema = {"type": "object", "oneOf": [{"type": "object"}]}
    with pytest.raises(_schema.SchemaError, match="does not implement"):
        _schema.validate({}, schema)
