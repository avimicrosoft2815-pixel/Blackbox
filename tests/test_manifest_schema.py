"""P4-02 — the manifest schema, executed on write and on read.

Two rules, and the tests are split by them. **Required means every capsule this format has produced carries it**, so a
missing `run.run_id` is refused on the way in *and* on the way out (`BBX-E407`, naming the dotted path). **Unknown keys
are tolerated**: a capsule from a newer writer must open here, and the keys this build does not know are carried on the
`Capsule` rather than dropped or treated as damage.

The older-capsule test is the one that keeps the pair honest: every field added after the first capsules were written —
`derived_parts`, `chain_root`, `capture_seam`, `capture_vantage`, `links_summary` — is optional, because refusing an
older capsule would make forward compatibility a claim instead of a property.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.cap import manifest as manifest_mod
from blackbox.codec import cbor
from blackbox.errors import BlackBoxError
from blackbox.rec import recovery
from blackbox.tape import writer as writer_mod

RUN_ID = "r_0123456789abcdef"


def _capsule(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1).open()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": RUN_ID}), node_slot=0)
    writer.emit(C.KIND_MODEL_REQUEST, json.dumps({"provider": "api.openai.com", "path": "/v1/chat/completions",
                                                  "model": "gpt-4", "capture_seam": "sdk_httpx"}).encode(),
                node_slot=0)
    writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": RUN_ID}), node_slot=0)
    writer.close()
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    assert result is not None and result.capsule_path
    return Path(result.capsule_path)


def _manifest(path: Path) -> dict:
    return json.loads(cap.read_capsule(str(path)).entries[cap.MANIFEST_NAME].decode("utf-8"))


def _write(path: Path, manifest: dict, *, entries_from: Path) -> Path:
    """Write a capsule whose manifest is exactly `manifest`, keeping the entries of a real one.

    Built by hand, without `cap.write_capsule`: that function now validates on write (which is the point of the ticket),
    so producing a capsule with an invalid manifest — what a hostile or broken writer would hand a reader — needs the
    container to be assembled the way a foreign writer would. The integrity root is recomputed so that the *only*
    defect is the manifest key under test.
    """
    import zipfile

    source = cap.read_capsule(str(entries_from))
    entries = {name: blob for name, blob in source.entries.items()
               if name not in (cap.MANIFEST_NAME, cap.INTEGRITY_NAME)}
    lines = cap.integrity_lines(entries)
    full = {**manifest, "integrity": {"alg": "sha256", "entries": len(entries) + 2,
                                     "root": cap.integrity_root(lines)}}
    manifest_bytes = json.dumps(full, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])
        zf.writestr(cap.INTEGRITY_NAME, lines)
        zf.writestr(cap.MANIFEST_NAME, manifest_bytes)
    return path


def test_a_capsule_this_build_writes_validates_cleanly(tmp_path):
    path = _capsule(tmp_path)
    manifest = _manifest(path)
    assert manifest_mod.validate(manifest) == [], "the writer produces a manifest its own reader accepts"
    assert cap.read_capsule(str(path)).unknown_manifest_keys == []


def test_every_required_key_is_one_the_writer_actually_writes(tmp_path):
    """A required key the writer never writes would make every capsule unreadable — so the schema is checked against a
    real manifest, not against itself."""
    manifest = _manifest(_capsule(tmp_path))

    def flat(schema: dict, prefix: str = "") -> set[str]:
        out: set[str] = set()
        for key, (spec, required) in schema.items():
            if required:
                out.add(prefix + key)
            if isinstance(spec, dict):
                out = out | flat(spec, prefix + key + ".")
        return out

    required = flat(manifest_mod.SCHEMA)
    missing = sorted(item for item in required if not _has(manifest, item))
    assert missing == [], f"SCHEMA requires keys the writer does not produce: {missing}"


def _has(manifest: dict, dotted: str) -> bool:
    node: object = manifest
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def test_a_missing_required_key_is_refused_on_read_and_names_its_path(tmp_path):
    path = _capsule(tmp_path)
    manifest = _manifest(path)
    del manifest["run"]["run_id"]
    broken = _write(tmp_path / "broken.bbx", manifest, entries_from=path)
    with pytest.raises(BlackBoxError) as exc:
        cap.read_capsule(str(broken))
    assert "BBX-E407" in str(exc.value)
    assert "run.run_id" in str(exc.value), "the message must name the dotted path, not just say 'invalid'"


def test_a_missing_required_key_is_refused_on_write_and_writes_nothing(tmp_path):
    path = _capsule(tmp_path)
    manifest = _manifest(path)
    del manifest["capsule_id"]
    source = cap.read_capsule(str(path))
    entries = {name: blob for name, blob in source.entries.items()
               if name not in (cap.MANIFEST_NAME, cap.INTEGRITY_NAME)}
    target = tmp_path / "never-written.bbx"
    with pytest.raises(BlackBoxError) as exc:
        cap.write_capsule(str(target), entries, manifest)
    assert "BBX-E407" in str(exc.value) and "capsule_id" in str(exc.value)
    assert not target.exists(), "a refused manifest must not leave a partial capsule behind"


def test_a_wrong_type_is_refused_with_the_dotted_path(tmp_path):
    path = _capsule(tmp_path)
    manifest = _manifest(path)
    manifest["counts"] = {**manifest["counts"], "events": "many"}
    broken = _write(tmp_path / "types.bbx", manifest, entries_from=path)
    with pytest.raises(BlackBoxError) as exc:
        cap.read_capsule(str(broken))
    assert "counts.events" in str(exc.value) and "BBX-E407" in str(exc.value)


def test_a_bool_is_not_an_integer(tmp_path):
    """`True` is an `int` in Python and not in JSON Schema; a format_version of `True` must not read as 1."""
    manifest = _manifest(_capsule(tmp_path))
    manifest["format_version"] = True
    with pytest.raises(BlackBoxError) as exc:
        manifest_mod.validate(manifest)
    assert "format_version" in str(exc.value)


def test_an_unknown_key_is_tolerated_and_carried(tmp_path):
    path = _capsule(tmp_path)
    manifest = _manifest(path)
    manifest["future_field"] = {"whatever": [1, 2, 3]}
    newer = _write(tmp_path / "newer.bbx", manifest, entries_from=path)
    capsule = cap.read_capsule(str(newer))
    assert capsule.unknown_manifest_keys == ["future_field"]
    assert manifest_mod.validate(capsule.manifest) == ["future_field"], "tolerated, and named rather than swallowed"
    assert capsule.manifest["future_field"] == {"whatever": [1, 2, 3]}, "an unknown key is preserved, not dropped"


def test_an_older_manifest_without_the_later_fields_still_reads(tmp_path):
    path = _capsule(tmp_path)
    manifest = _manifest(path)
    for key in ("derived_parts", "chain_root", "capture_seam", "capture_vantage", "links_summary", "min_reader",
                "signature"):
        manifest.pop(key, None)
    older = _write(tmp_path / "older.bbx", manifest, entries_from=path)
    capsule = cap.read_capsule(str(older))
    assert capsule.unknown_manifest_keys == []
    report = manifest_mod.verify(capsule)
    assert report["ok"] is True
    assert {"chain_root", "capture_seam", "links_summary"} <= set(report["uncheckable"]), \
        "absent later fields are uncheckable, not failures"


def test_a_plan_shaped_manifest_validates(tmp_path):
    """§13.2's own field names (`run.name`, `counts.model_calls`, `capture.sdk_matrix`, `redaction.key_included`) are
    accepted where this build writes different ones: the schema describes the vocabulary, not one writer's dialect."""
    manifest = _manifest(_capsule(tmp_path))
    manifest["run"] = {**manifest["run"], "name": "refund", "started_ns": 0, "ended_ns": 0}
    manifest["counts"] = {**manifest["counts"], "model_calls": 6, "tool_calls": 11}
    manifest["capture"] = {**manifest["capture"], "sdk_matrix": "2026.11", "unsupported_ops": 0}
    manifest["redaction"] = {**manifest["redaction"], "key_included": False}
    assert manifest_mod.validate(manifest) == []


def test_the_validator_rejects_a_spec_it_does_not_implement():
    """Same rule as the OTLP snapshot: a validator that ignored what it cannot check would report a pass it never ran.

    The guard is inside `_type_ok`, so a future entry in `SCHEMA` that spells a kind of value this file does not know
    fails loudly on the first capsule read rather than passing everything it is handed.
    """
    with pytest.raises(AssertionError, match="not one this validator implements"):
        manifest_mod._type_ok("quantum", 1)
