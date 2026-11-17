"""P4-16 — `blackbox verify`: five checks, three ways to name a target, and one check that refuses to be a pass.

The tests worth reading are the tamper ones at the bottom: each edits exactly one part of a real capsule and asserts the
command notices, exits 9, and says which check failed — a raw entry through the integrity root, a derived part through
`BBX-E412`, and a manifest field through its recomputation. `capture_vantage` gets the opposite treatment: a test asserts
it is reported as **declared and uncheckable**, because "the capsule cannot answer this" is the fact the report has to
carry, and a green line there would be a check that never ran.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import __main__ as front_door
from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.cap import manifest as manifest_mod
from blackbox.cli.cmd_verify import main as verify_main
from blackbox.codec import cbor
from blackbox.rec import recovery
from blackbox.tape import writer as writer_mod

RUN_ID = "r_0123456789abcdef"


def _capsule(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1).open()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": RUN_ID}), node_slot=0)
    writer.emit(C.KIND_NODE_OPEN, cbor.encode({"name": "agent", "kind": "run"}), node_slot=1)
    writer.emit(C.KIND_MODEL_REQUEST, json.dumps({"provider": "api.openai.com", "path": "/v1/chat/completions",
                                                  "model": "gpt-4", "capture_seam": "sdk_httpx",
                                                  "call_id": "c0ffee"}).encode(), node_slot=1)
    writer.emit(C.KIND_MODEL_RESPONSE, json.dumps({"status": 200, "chunks_raw": 2, "call_id": "c0ffee"}).encode(),
                node_slot=1)
    writer.emit(C.KIND_NODE_CLOSE, cbor.encode({"outcome": "ok"}), node_slot=1)
    writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": RUN_ID}), node_slot=0)
    writer.close()
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    assert result is not None and result.capsule_path
    tmp_path.joinpath("writer").write_text(writer.writer_uuid.hex())
    return Path(result.capsule_path)


def _report(path: Path) -> dict:
    return manifest_mod.verify(cap.read_capsule(str(path)))


def _rewrite(source: Path, dest: Path, *, entries_edit=None, manifest_edit=None) -> Path:
    """Rewrite a capsule with a fresh, self-consistent integrity root — so a test can say "the manifest lies" without
    also breaking the archive's framing. Editing the entries *without* this is the raw-tamper test."""
    capsule = cap.read_capsule(str(source))
    entries = {name: blob for name, blob in capsule.entries.items()
               if name not in (cap.MANIFEST_NAME, cap.INTEGRITY_NAME)}
    manifest = dict(capsule.manifest)       # the id is kept: the OTLP export derives span ids from it, so changing it
                                            # would make every test here also a derived-part drift test.
    if entries_edit:
        entries_edit(entries)
    if manifest_edit:
        manifest_edit(manifest)
    cap.write_capsule(str(dest), entries, manifest)
    return dest


def test_a_fresh_capsule_passes_every_check_it_can_answer(tmp_path, capsys):
    path = _capsule(tmp_path)
    assert verify_main([str(path)]) == 0
    out = capsys.readouterr().out
    for name in ("integrity", "chain_root", "derived_parts", "capture_seam", "links_summary"):
        assert f"ok   {name}" in out, name
    assert "declared, not checkable" in out


def test_the_manifest_carries_the_four_fields_p4_16_asks_for(tmp_path):
    capsule = cap.read_capsule(str(_capsule(tmp_path)))
    for key in ("chain_root", "derived_parts", "capture_vantage", "capture_seam", "links_summary"):
        assert key in capsule.manifest, key
    assert capsule.manifest["capture_vantage"] == 1
    assert capsule.manifest["capture_seam"] == ["sdk_httpx"]
    assert capsule.manifest["chain_root"] and len(capsule.manifest["chain_root"]) == 64
    # every declared part is re-rendered by the derived-parts check, so the manifest cannot list a part it does not ship
    assert {part["name"] for part in capsule.manifest["derived_parts"]} == {cap.GRAPH_NAME, cap.EVENTS_NAME,
                                                                          cap.OTLP_NAME}


def test_the_vantage_is_declared_and_reported_as_uncheckable(tmp_path, capsys):
    path = _capsule(tmp_path)
    report = _report(path)
    check = {item["check"]: item for item in report["checks"]}["capture_vantage"]
    assert check["ok"] is None and check["checkable"] is False, "a check that never ran is not a pass"
    assert "no meta page" in check["note"]
    assert report["ok"] is True, "uncheckable is not a failure either"
    assert "capture_vantage" not in report["failed"]
    assert verify_main([str(path), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    shipped = {item["check"]: item for item in body["reports"][0]["checks"]}["capture_vantage"]
    assert shipped["ok"] is None and shipped["checkable"] is False, "--json must carry the same distinction"


def test_verify_names_the_target_three_ways(tmp_path, capsys):
    path = _capsule(tmp_path)
    writer = (tmp_path / "writer").read_text().strip()
    data_dir = str(path.parent.parent)
    assert verify_main(["--run", RUN_ID, "--data", data_dir]) == 0
    capsys.readouterr()
    assert verify_main(["--writer", writer, "--data", data_dir]) == 0
    captured = capsys.readouterr().out
    assert str(path) in captured, "the writer form resolves to the same capsule"


def test_an_unknown_run_or_writer_is_bad_input_not_a_verdict(tmp_path, capsys):
    path = _capsule(tmp_path)
    data_dir = str(path.parent.parent)
    assert verify_main(["--run", "r_ffffffffffffffff", "--data", data_dir]) == 2
    assert "names run" in capsys.readouterr().out
    assert verify_main(["--writer", "0" * 32, "--data", data_dir]) == 2
    assert "for writer" in capsys.readouterr().out, "a writer that derives no capsule is named in the message"


def test_a_writer_that_does_not_derive_the_capsule_id_is_reported(tmp_path, capsys):
    """A capsule claiming a tape it did not come from is a finding, not a "not found"."""
    path = _capsule(tmp_path)
    data_dir = str(path.parent.parent)
    other = "1" * 32
    assert verify_main(["--writer", other, "--data", data_dir]) == 2
    out = capsys.readouterr().out
    assert "declares r_0123456789abcdef but its id is not" in out, out


def test_a_raw_entry_edit_fails_the_integrity_check_with_exit_9(tmp_path, capsys):
    path = _capsule(tmp_path)
    capsule = cap.read_capsule(str(path))
    entries = {name: blob for name, blob in capsule.entries.items()
               if name not in (cap.MANIFEST_NAME, cap.INTEGRITY_NAME)}
    target = cap.seg_entry_name(1)
    entries[target] = entries[target].replace(b"gpt-4", b"gpt-5", 1)
    tampered = tmp_path / "raw.bbx"
    # No fresh integrity root: this is the tamper the INTEGRITY lines exist to catch.
    import zipfile
    # ZIP_STORED, not DEFLATED: the reader refuses a capsule whose entries expand over its ratio limit (BBX-E405), and a
    # half-empty test tape compresses absurdly well -- that refusal is a feature, so the tamper is built without it.
    with zipfile.ZipFile(tampered, "w", zipfile.ZIP_STORED) as zf:
        for name, blob in {**entries, cap.MANIFEST_NAME: capsule.entries[cap.MANIFEST_NAME]}.items():
            zf.writestr(name, blob)
    assert verify_main([str(tampered)]) == 9
    out = capsys.readouterr().out
    assert "FAIL integrity" in out
    assert target in out or "INTEGRITY" in out


def test_an_edited_derived_part_is_bbx_e412_and_exit_9(tmp_path, capsys):
    path = _capsule(tmp_path)
    document = json.loads(cap.read_capsule(str(path)).entry(cap.OTLP_NAME))
    document["resourceSpans"][0]["scopeSpans"][0]["spans"][-1]["name"] = "chat gpt-5"
    tampered = _rewrite(path, tmp_path / "derived.bbx",
                        entries_edit=lambda entries: entries.__setitem__(
                            cap.OTLP_NAME, json.dumps(document, indent=2, sort_keys=True).encode() + b"\n"))
    assert verify_main([str(tampered)]) == 9
    out = capsys.readouterr().out
    assert "FAIL derived_parts" in out
    assert "BBX-E412" in out


def test_a_manifest_chain_root_that_is_not_the_capsules_own_is_a_finding(tmp_path, capsys):
    path = _capsule(tmp_path)
    tampered = _rewrite(path, tmp_path / "chain.bbx",
                        manifest_edit=lambda manifest: manifest.__setitem__("chain_root", "0" * 64))
    assert verify_main([str(tampered)]) == 9
    out = capsys.readouterr().out
    assert "FAIL chain_root" in out and "0000000000000000" in out


def test_a_links_summary_that_does_not_match_links_json_is_a_finding(tmp_path, capsys):
    path = _capsule(tmp_path)
    tampered = _rewrite(path, tmp_path / "links.bbx",
                        manifest_edit=lambda manifest: manifest["links_summary"].__setitem__("links", 99))
    assert verify_main([str(tampered)]) == 9
    assert "FAIL links_summary" in capsys.readouterr().out


def test_a_seam_that_was_never_recorded_is_a_finding(tmp_path, capsys):
    path = _capsule(tmp_path)
    tampered = _rewrite(path, tmp_path / "seam.bbx",
                        manifest_edit=lambda manifest: manifest.__setitem__("capture_seam", ["proxy_module_patch"]))
    assert verify_main([str(tampered)]) == 9
    assert "FAIL capture_seam" in capsys.readouterr().out


def test_the_command_is_reachable_from_the_front_door(tmp_path, capsys):
    path = _capsule(tmp_path)
    assert front_door.main(["verify", str(path)]) == 0
    assert "verified" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:           # argparse's usage error: no target named
        front_door.main(["verify"])
    assert exc.value.code == 2


def test_an_old_capsule_without_the_new_fields_is_absent_not_wrong(tmp_path, capsys):
    """The compatibility rule points both ways: a new build must not call an older capsule corrupt."""
    path = _capsule(tmp_path)
    older = _rewrite(path, tmp_path / "older.bbx",
                     manifest_edit=lambda manifest: [manifest.pop(key, None) for key in
                                                     ("chain_root", "capture_seam", "links_summary")])
    assert verify_main([str(older)]) == 0
    out = capsys.readouterr().out
    assert "capture_seam" in out and "----" in out


def test_nothing_is_written_by_a_failed_verification(tmp_path):
    path = _capsule(tmp_path)
    before = path.read_bytes()
    tampered = _rewrite(path, tmp_path / "links2.bbx",
                        manifest_edit=lambda manifest: manifest["links_summary"].__setitem__("links", 99))
    tampered_before = tampered.read_bytes()
    assert verify_main([str(tampered)]) == 9
    assert tampered.read_bytes() == tampered_before, "a verifier that rewrote the capsule would erase its evidence"
    assert path.read_bytes() == before


@pytest.mark.parametrize("run_id", ["", "r_0000000000000000"])
def test_capsule_id_derivation_is_the_one_recovery_uses(run_id):
    assert manifest_mod.capsule_id_for(run_id, "0" * 32) == \
        "c_" + __import__("hashlib").blake2b(f"{run_id}|{'0' * 32}".encode()).hexdigest()[:16]
