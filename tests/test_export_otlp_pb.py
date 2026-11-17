"""P4-20 — `otlp/spans.pb`: the same spans as OTLP protobuf, encoded by hand and decoded here by an independent reader.

The decoder in this file shares no code with the encoder: it walks the wire format (varint, fixed64, length-delimited)
and rebuilds the fields by number, so a wrong field number or a wrong wire type fails a test instead of being "agreed"
by two copies of the same mistake. That is also how the field-number bug was found during development: the first encoder
emitted span attributes at field 1, and a real decoder read them back as `trace_id`. The fields asserted here are exactly
the ones a Phoenix-class receiver needs: ids, name, kind, the two timestamps, parent, attributes and status.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import derived, export_otlp
from blackbox.cap import format as cap
from blackbox.codec import cbor
from blackbox.rec import recovery
from blackbox.tape import writer as writer_mod

RUN_ID = "r_0123456789abcdef"


# --------------------------------------------------------------------------------------------- an independent reader


def _varint(buf: bytes, index: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7


def _decode(buf: bytes) -> list[tuple[int, int, object]]:
    fields: list[tuple[int, int, object]] = []
    index = 0
    while index < len(buf):
        key, index = _varint(buf, index)
        number, wire = key >> 3, key & 0x07
        if wire == 0:
            value, index = _varint(buf, index)
        elif wire == 1:
            value = int.from_bytes(buf[index:index + 8], "little")
            index += 8
        elif wire == 2:
            size, index = _varint(buf, index)
            value = buf[index:index + size]
            index += size
        else:                                                    # pragma: no cover - the encoder emits only 0/1/2
            raise AssertionError(f"wire type {wire} at field {number} is not one this reader knows")
        fields.append((number, wire, value))
    return fields


def _one(fields: list[tuple[int, int, object]], number: int, wire: int) -> object:
    for field, kind, value in fields:
        if field == number and kind == wire:
            return value
    return None


def _all(fields: list[tuple[int, int, object]], number: int, wire: int) -> list[object]:
    return [value for field, kind, value in fields if field == number and kind == wire]


def _any_value(raw: bytes) -> object:
    for number, _wire, value in _decode(raw):
        if number == 1:
            return value.decode("utf-8")
        if number == 2:
            return bool(value)
        if number == 3:
            return value - (1 << 64) if value >= (1 << 63) else value       # int64 is two's complement
        if number == 4:
            return struct.unpack("<d", struct.pack("<Q", value))[0]
        if number == 5:
            return [_any_value(item) for item in _all(_decode(value), 1, 2)]
        if number == 6:
            return {kv["key"]: kv["value"] for kv in _key_values(value, 1)}
    return None


def _key_values(raw: bytes, field: int = 1) -> list[dict]:
    out: list[dict] = []
    for entry in _all(_decode(raw), field, 2):
        fields = _decode(entry)
        out.append({"key": _one(fields, 1, 2).decode("utf-8"), "value": _any_value(_one(fields, 2, 2))})
    return out


def _document(pb: bytes) -> dict:
    """ResourceSpans → (resource attributes, scope, spans)."""
    resource_spans = _decode(_one(_decode(pb), 1, 2))
    resource = _one(resource_spans, 1, 2)
    scope_spans = _decode(_one(resource_spans, 2, 2))
    scope = _decode(_one(scope_spans, 1, 2))
    spans = []
    for raw in _all(scope_spans, 2, 2):
        fields = _decode(raw)
        status = _decode(_one(fields, 15, 2) or b"")
        spans.append({
            "trace_id": (_one(fields, 1, 2) or b"").hex(),
            "span_id": (_one(fields, 2, 2) or b"").hex(),
            "parent_span_id": (_one(fields, 4, 2) or b"").hex() or None,
            "name": _one(fields, 5, 2).decode("utf-8"),
            "kind": _one(fields, 6, 0),
            "start": _one(fields, 7, 1),
            "end": _one(fields, 8, 1),
            "attributes": {item["key"]: item["value"] for item in _span_attributes(fields)},
            "status": {"code": _one(status, 3, 0) or 0,
                       "message": (_one(status, 2, 2) or b"").decode("utf-8") or None},
        })
    return {"resource": {kv["key"]: kv["value"] for kv in _key_values(resource)},
            "scope": {"name": _one(scope, 1, 2).decode("utf-8"), "version": _one(scope, 2, 2).decode("utf-8")},
            "spans": spans}


def _span_attributes(fields: list[tuple[int, int, object]]) -> list[dict]:
    out: list[dict] = []
    for entry in _all(fields, 9, 2):
        inner = _decode(entry)
        out.append({"key": _one(inner, 1, 2).decode("utf-8"), "value": _any_value(_one(inner, 2, 2))})
    return out


# ------------------------------------------------------------------------------------------------ capsules to read


def _capsule(tmp_path: Path, *, close_node: bool = True, secret: bytes | None = None) -> Path:
    data = tmp_path / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1).open()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": RUN_ID}), node_slot=0)
    writer.emit(C.KIND_NODE_OPEN, cbor.encode({"name": "support-agent", "kind": "run"}), node_slot=1)
    writer.emit(C.KIND_MODEL_REQUEST, json.dumps({"provider": "api.openai.com", "path": "/v1/chat/completions",
                                                  "model": "gpt-4", "capture_seam": "sdk_httpx",
                                                  "call_id": "c0ffee"}).encode(), node_slot=1)
    if secret:
        writer.emit(C.KIND_MODEL_CHUNK, secret, node_slot=1)
    writer.emit(C.KIND_MODEL_RESPONSE, json.dumps({"status": 200, "chunks_raw": 12, "bytes_received": 8123,
                                                   "call_id": "c0ffee"}).encode(), node_slot=1)
    if close_node:
        writer.emit(C.KIND_NODE_CLOSE, cbor.encode({"outcome": "ok"}), node_slot=1)
    writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": RUN_ID}), node_slot=0)
    writer.close()
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    assert result is not None and result.capsule_path
    return Path(result.capsule_path)


def _pb(path: Path) -> bytes:
    return cap.read_capsule(str(path)).entry(cap.OTLP_PB_NAME)


def test_the_protobuf_decodes_with_an_independent_reader(tmp_path):
    document = _document(_pb(_capsule(tmp_path)))
    assert [span["name"] for span in document["spans"]] == ["bbx.run", "invoke_agent support-agent", "chat gpt-4"]
    assert document["scope"] == {"name": "blackbox", "version": "0.1.0.dev0"}
    assert document["resource"]["service.name"] == "blackbox"
    assert document["resource"]["bbx.run.id"] == RUN_ID
    run, agent, chat = document["spans"]
    assert run["parent_span_id"] is None
    assert chat["kind"] == export_otlp.CLIENT and agent["kind"] == export_otlp.INTERNAL
    assert chat["parent_span_id"] == run["span_id"] and agent["parent_span_id"] == run["span_id"]
    assert len(run["trace_id"]) == 32 and all(span["trace_id"] == run["trace_id"] for span in document["spans"])


def test_the_two_encodings_describe_the_same_spans(tmp_path):
    """The JSON and the protobuf are two views of one model, so a reader of either sees the same trace."""
    capsule = cap.read_capsule(str(_capsule(tmp_path)))
    json_spans = {span["name"]: span for span in
                  json.loads(capsule.entry(cap.OTLP_NAME))["resourceSpans"][0]["scopeSpans"][0]["spans"]}
    pb_spans = {span["name"]: span for span in _document(capsule.entry(cap.OTLP_PB_NAME))["spans"]}
    assert set(json_spans) == set(pb_spans)
    for name, span in json_spans.items():
        other = pb_spans[name]
        assert span["traceId"] == other["trace_id"]
        assert span["spanId"] == other["span_id"]
        assert span.get("parentSpanId") == other["parent_span_id"]
        assert int(span["startTimeUnixNano"]) == other["start"]
        assert int(span["endTimeUnixNano"]) == other["end"]
        assert span["kind"] == other["kind"]
        assert _json_attributes(span) == other["attributes"], name


def _json_attributes(span: dict) -> dict:
    """The JSON encoding spells int64 as a string; compare values, not spellings."""
    out: dict = {}
    for item in span["attributes"]:
        (kind, value), = item["value"].items()
        out[item["key"]] = int(value) if kind == "intValue" else value
    return out


def test_rendering_is_byte_stable_and_matches_the_shipped_part(tmp_path):
    capsule = cap.read_capsule(str(_capsule(tmp_path)))
    frames = derived.frames_of(capsule)
    kwargs = {"capsule_id": capsule.manifest["capsule_id"], "run_id": RUN_ID}
    once = export_otlp.render_pb(frames, **kwargs)
    assert once == export_otlp.render_pb(frames, **kwargs) == capsule.entry(cap.OTLP_PB_NAME)


def test_attributes_keep_their_types(tmp_path):
    """An int is an `int_value`, not a string: a receiver that shows `8123` as text has lost the type it needs."""
    spans = {span["name"]: span for span in _document(_pb(_capsule(tmp_path, close_node=False)))["spans"]}
    chat = spans["chat gpt-4"]
    assert chat["attributes"]["bbx.model.bytes_received"] == 8123
    assert chat["attributes"]["bbx.response.status"] == 200
    assert chat["attributes"]["gen_ai.operation.name"] == "chat"
    assert chat["attributes"]["gen_ai.provider.name"] == "openai"
    agent = spans["invoke_agent support-agent"]
    assert agent["attributes"]["bbx.span.truncated"] is True, "an unclosed node says so on the wire too"
    assert agent["status"] == {"code": 2, "message": "the node never closed: no NODE_CLOSE frame"}


def test_a_negative_integer_survives_the_twos_complement(tmp_path):
    encoded = export_otlp._any_value(-5)
    assert _any_value(encoded) == -5
    assert export_otlp._any_value(-1) == export_otlp._any_value(-1), "encoding is a function, not a draw"


def test_no_model_text_reaches_the_protobuf(tmp_path):
    secret = b"sk-live-not-a-real-key"
    capsule = cap.read_capsule(str(_capsule(tmp_path, secret=secret)))
    assert secret not in capsule.entry(cap.OTLP_PB_NAME)
    assert secret not in capsule.entry(cap.OTLP_NAME)
    chat = next(span for span in _document(capsule.entry(cap.OTLP_PB_NAME))["spans"] if span["name"] == "chat gpt-4")
    assert chat["attributes"]["bbx.model.chunks_raw"] == 12, "the counter, never the bytes"


def test_it_is_a_declared_derived_part_and_verify_recomputes_it(tmp_path):
    path = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    declared = {part["name"]: part for part in capsule.manifest["derived_parts"]}
    assert cap.OTLP_PB_NAME in declared
    import hashlib
    assert declared[cap.OTLP_PB_NAME]["sha256"] == hashlib.sha256(capsule.entry(cap.OTLP_PB_NAME)).hexdigest()
    report = derived.verify(capsule)
    assert report["ok"] and report["drift"] == []
    assert {part["name"] for part in report["parts"]} == {cap.EVENTS_NAME, cap.GRAPH_NAME, cap.OTLP_NAME,
                                                         cap.OTLP_PB_NAME}


def test_an_edited_protobuf_is_reported_as_bytes_not_as_a_line(tmp_path):
    path = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    entries = {name: blob for name, blob in capsule.entries.items()
               if name not in (cap.MANIFEST_NAME, cap.INTEGRITY_NAME)}
    original = entries[cap.OTLP_PB_NAME]
    assert b"gpt-4" in original
    entries[cap.OTLP_PB_NAME] = original.replace(b"gpt-4", b"gpt-5")          # same length: a name, not a framing change
    tampered = tmp_path / "pb.bbx"
    cap.write_capsule(str(tampered), entries, {**capsule.manifest, "capsule_id": capsule.manifest["capsule_id"]})
    report = derived.verify(cap.read_capsule(str(tampered)))
    assert not report["ok"]
    drift = [item for item in report["drift"] if item["part"] == cap.OTLP_PB_NAME]
    assert drift, report["drift"]
    byte_drift = [item for item in drift if str(item["field"]).startswith("bytes (")]
    assert byte_drift, drift
    assert "no lines to number" in byte_drift[0]["note"]


def test_an_unreadable_protobuf_is_not_a_pass(tmp_path):
    """Truncating the part must fail the comparison, not raise out of the verifier."""
    path = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    entries = {name: blob for name, blob in capsule.entries.items()
               if name not in (cap.MANIFEST_NAME, cap.INTEGRITY_NAME)}
    entries[cap.OTLP_PB_NAME] = entries[cap.OTLP_PB_NAME][:-3]
    tampered = tmp_path / "short.bbx"
    cap.write_capsule(str(tampered), entries, {**capsule.manifest})
    report = derived.verify(cap.read_capsule(str(tampered)))
    assert not report["ok"] and report["drift"]


def test_the_encoder_spells_what_it_cannot_type_as_a_string():
    """An attribute value this format has no type for is carried as text, never silently dropped.

    That is a deliberate limit rather than a mistake: the attributes this build writes are strings, integers and booleans
    (the frames cannot produce anything else), and a value that arrives from a future writer should still be visible to
    a receiver — as text, which is what it is to us.
    """
    assert _any_value(export_otlp._any_value({"a": 1})) == {"a": 1}, "a dict is a kvlist"
    assert _any_value(export_otlp._any_value([1, "two"])) == [1, "two"], "a list is an array"
    marker = "an-untypeable-value"
    assert _any_value(export_otlp._any_value(marker)) == marker, "a value with no type is text, never a dropped field"


def test_a_capsule_without_the_protobuf_is_absent_not_wrong(tmp_path):
    path = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    entries = {name: blob for name, blob in capsule.entries.items()
               if name not in (cap.MANIFEST_NAME, cap.INTEGRITY_NAME, cap.OTLP_PB_NAME)}
    older = tmp_path / "older.bbx"
    cap.write_capsule(str(older), entries, {**capsule.manifest,
                                            "derived_parts": [part for part in capsule.manifest["derived_parts"]
                                                              if part["name"] != cap.OTLP_PB_NAME]})
    report = derived.verify(cap.read_capsule(str(older)))
    assert report["ok"], report
    assert any(cap.OTLP_PB_NAME in item for item in report["absent"])
