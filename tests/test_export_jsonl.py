"""P4-13 — `events/events.jsonl` is a derived part, and a derived part is only worth having if it can be recomputed.

Four claims, each with its own test and its own way of failing honestly:

* **one object per frame, and the file is a text file** — a trailing newline, LF, sorted keys, no generation timestamp,
  so rendering the same frames twice gives the same bytes (`capsule verify` compares bytes, so this is the load-bearing
  property, not a nicety);
* **the `gen_ai.*` names are the pinned ones, and only where the mapping has a name** — OpenTelemetry semantic
  conventions v1.39.0 (`6d05e92`), where the provider attribute is `gen_ai.provider.name`; a host and a path the mapping
  does not know get no
  `gen_ai.*` key at all, because a plausible-looking guess in a standards namespace is worse than an honest absence;
* **everything else is `bbx.`** — asserted over every attribute of every line, so a future export cannot quietly add an
  un-namespaced claim;
* **content is not copied** — a chunk frame contributes a byte count, never the bytes, so the export stays a timeline
  rather than a second tape.

And the capsule half: the part is inside the hashed container, its declared sha256 matches, an edited line is reported as
`BBX-E412` with the line number, and the capsule is left exactly as it was.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import derived, export_jsonl
from blackbox.cap import format as cap
from blackbox.cap.cli import main as capsule_main
from blackbox.codec import cbor
from blackbox.rec import recovery
from blackbox.tape import writer as writer_mod


def _lines(blob: bytes) -> list[dict]:
    assert blob.endswith(b"\n"), "a JSONL file ends with a newline"
    return [json.loads(line) for line in blob.decode("utf-8").splitlines()]


def _request(model: str = "gpt-4", provider: str = "api.openai.com", path: str = "/v1/chat/completions") -> bytes:
    return json.dumps({"provider": provider, "path": path, "model": model, "request_fp": "fp1",
                       "stream": True, "call_id": "c0ffee", "linked_run": "r_0123456789abcdef"}).encode()


def _capsule(tmp_path: Path) -> tuple[Path, dict]:
    """A real tape with a model call, a node and a chunk, recovered the way the launcher recovers it."""
    data = tmp_path / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1).open()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": "r_0123456789abcdef"}), node_slot=0)
    writer.emit(C.KIND_NODE_OPEN, cbor.encode({"name": "agent", "kind": "run"}), node_slot=1)
    writer.emit(C.KIND_MODEL_REQUEST, _request(), node_slot=1)
    writer.emit(C.KIND_MODEL_CHUNK, b"[bbx-hash sha256=" + b"0" * 64 + b" bytes=42]", node_slot=1)
    writer.emit(C.KIND_MODEL_RESPONSE, json.dumps({"provider": "api.openai.com", "path": "/v1/chat/completions",
                                                   "model": "gpt-4", "status": 200, "response_fp": "fp2",
                                                   "chunks_events": 1, "bytes_received": 42}).encode(), node_slot=1)
    writer.emit(C.KIND_NODE_CLOSE, cbor.encode({"name": "agent", "outcome": "ok"}), node_slot=1)
    writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": "r_0123456789abcdef"}), node_slot=0)
    writer.close()
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    assert result is not None and result.capsule_path
    return Path(result.capsule_path), cap.read_capsule(result.capsule_path).manifest


def test_one_object_per_frame_and_byte_identical_on_a_second_pass(tmp_path):
    path, _manifest = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    assert cap.EVENTS_NAME in capsule.entries
    blob = capsule.entry(cap.EVENTS_NAME)
    lines = _lines(blob)
    assert len(lines) == capsule.manifest["counts"]["events"], "one object per event, and the manifest agrees"
    assert [line["seq"] for line in lines] == sorted(line["seq"] for line in lines)
    assert blob == export_jsonl.render(derived.frames_of(capsule)), \
        "the export is a pure function of the frames: two renderings must be the same bytes"
    assert b"generated" not in blob and b'"capsule' not in blob, "nothing about the run of the exporter belongs in it"


def test_the_gen_ai_names_are_the_pinned_semconv_and_nothing_else(tmp_path):
    path, _manifest = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    request = next(line for line in _lines(capsule.entry(cap.EVENTS_NAME)) if line["kind"] == "MODEL_REQUEST")
    attributes = request["attributes"]
    assert attributes["gen_ai.operation.name"] == "chat"
    assert attributes["gen_ai.provider.name"] == "openai", "v1.39.0 calls it gen_ai.provider.name"
    assert attributes["gen_ai.request.model"] == "gpt-4"
    assert attributes["bbx.model.call_id"] == "c0ffee" and attributes["bbx.model.linked_run"] == "r_0123456789abcdef"
    assert export_jsonl.SEMCONV == "v1.39.0" and export_jsonl.SEMCONV_COMMIT.startswith("6d05e92")


@pytest.mark.parametrize("provider,path,system,operation", [
    ("127.0.0.1:8123", "/v1/chat/completions", None, "chat"),     # the recorder knows the socket, not the provider
    ("api.openai.com", "/v1/unknown", "openai", None),             # a provider we can name, an API we cannot
    ("api.anthropic.com", "/v1/messages", "anthropic", "chat"),
    ("generativelanguage.googleapis.com", "/v1beta/models/x", "gcp.gemini", None),
    ("not-openai.example", "/v1/chat/completions", None, "chat"),      # a suffix match would be a lie
])
def test_a_name_is_used_only_where_the_mapping_has_one(provider, path, system, operation):
    """Two independent mappings: who answers (`gen_ai.system`) and which API was called (`gen_ai.operation.name`).

    Neither is inferred from the other, and a fact we do hold — the recorded host — is still written down under `bbx.`,
    so an absent `gen_ai.*` key means "the mapping has no name for this", not "we did not look".
    """
    frame = {"seq": 1, "kind": C.KIND_MODEL_REQUEST, "wall_ns": 1, "mono_ns": 2, "node_slot": 0, "run_slot": 0,
             "payload": _request(provider=provider, path=path)}
    attributes = export_jsonl.line(frame)["attributes"]
    assert attributes.get("gen_ai.provider.name") == system
    assert attributes.get("gen_ai.operation.name") == operation
    assert attributes["bbx.provider.host"] == provider


def test_every_attribute_is_either_pinned_semconv_or_ours(tmp_path):
    path, _manifest = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    for line in _lines(capsule.entry(cap.EVENTS_NAME)):
        for name in line["attributes"]:
            assert name.startswith(("gen_ai.", "bbx.")), f"{name} is in no namespace a reader can check"


def test_chunk_content_is_not_copied_into_the_export(tmp_path):
    """The export is a timeline; the bytes stay in the segments where the content policy put them."""
    data = tmp_path / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1).open()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": "r_0123456789abcdef"}), node_slot=0)
    secret = b"sk-live-not-a-real-key"
    writer.emit(C.KIND_MODEL_REQUEST, _request(), node_slot=0)
    writer.emit(C.KIND_MODEL_CHUNK, secret, node_slot=0)
    writer.close()
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    capsule = cap.read_capsule(result.capsule_path)
    blob = capsule.entry(cap.EVENTS_NAME)
    assert secret not in blob
    chunk = next(line for line in _lines(blob) if line["kind"] == "MODEL_CHUNK")
    assert chunk["attributes"] == {"bbx.chunk.bytes": len(secret)}


def test_the_part_is_declared_inside_the_capsule_and_verifies(tmp_path):
    path, manifest = _capsule(tmp_path)
    declared = {part["name"]: part for part in manifest["derived_parts"]}
    capsule = cap.read_capsule(str(path))
    import hashlib
    assert declared[cap.EVENTS_NAME]["sha256"] == hashlib.sha256(capsule.entry(cap.EVENTS_NAME)).hexdigest()
    report = derived.verify(capsule)
    assert report["ok"] and report["drift"] == [], report
    assert {part["name"] for part in report["parts"]} == {cap.EVENTS_NAME, cap.GRAPH_NAME, cap.OTLP_NAME}


def test_an_edited_line_is_reported_with_its_number_and_the_capsule_is_untouched(tmp_path):
    path, manifest = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    lines = capsule.entry(cap.EVENTS_NAME).decode("utf-8").splitlines()
    # Find the line rather than counting to it: the frame order includes the chain's own frames, and a hard-coded
    # index is a test that passes on one platform and edits a NODE_OPEN on another (this test did, in CI).
    index = next(i for i, text in enumerate(lines) if json.loads(text)["kind"] == "MODEL_REQUEST")
    edited = json.loads(lines[index])
    edited["attributes"]["bbx.model.call_id"] = "not-the-id-that-was-recorded"
    lines[index] = json.dumps(edited, sort_keys=True, separators=(",", ":"))
    entries = {name: blob for name, blob in capsule.entries.items() if name not in
               (cap.MANIFEST_NAME, cap.INTEGRITY_NAME)}
    entries[cap.EVENTS_NAME] = ("\n".join(lines) + "\n").encode()
    tampered = tmp_path / "tampered.bbx"
    cap.write_capsule(str(tampered), entries, {**manifest, "capsule_id": "c_tampered"})
    check = cap.read_capsule(str(tampered))
    report = derived.verify(check)
    assert not report["ok"], report
    events_drift = [drift for drift in report["drift"] if drift["part"] == cap.EVENTS_NAME]
    assert events_drift, report["drift"]
    fields = {drift["field"] for drift in events_drift}
    assert "sha256" in fields and f"line {index + 1}" in fields, fields
    line_drift = next(drift for drift in events_drift if drift["field"] == f"line {index + 1}")
    assert "not-the-id-that-was-recorded" in line_drift["stored"]
    assert "c0ffee" in line_drift["recomputed"]
    assert cap.read_capsule(str(tampered)).entry(cap.EVENTS_NAME) == entries[cap.EVENTS_NAME], \
        "verification reports; it does not rewrite"


def test_the_cli_says_which_parts_it_checked_and_which_it_could_not(tmp_path, capsys):
    path, _manifest = _capsule(tmp_path)
    assert capsule_main(["verify", str(path)]) == 0
    out = capsys.readouterr().out
    assert cap.EVENTS_NAME in out and cap.GRAPH_NAME in out
    assert "derived parts verify" in out
