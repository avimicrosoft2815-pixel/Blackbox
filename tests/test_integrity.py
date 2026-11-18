"""P4-03 — `cap/integrity.py`: per-entry sha256, a sorted index, and a report that names the entry.

The ticket's acceptance line is "tamper 1 byte ⇒ verify fails naming the entry", so the tests are written the way a user
would meet the failure: change one byte in a sealed capsule, then ask the product what is wrong and check that the answer
contains the entry's name. The rest of the file covers the four findings the index can produce — a changed entry, a
listed-but-absent entry, an entry nobody listed, and a damaged line — plus the root over the index, because a tamperer
who edits a line *and* the entry would otherwise pass.
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.cap import integrity
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
    writer.emit(C.KIND_MODEL_REQUEST, json.dumps({"provider": "api.openai.com", "path": "/v1/chat/completions",
                                                  "model": "gpt-4"}).encode(), node_slot=0)
    writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": RUN_ID}), node_slot=0)
    writer.close()
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    assert result is not None and result.capsule_path
    return Path(result.capsule_path)


def _rewrite(path: Path, source: Path, *, mutate) -> Path:
    """Rebuild a capsule with one deliberate change — the index and manifest as they were, so the change is the only one."""
    capsule = cap.read_capsule(str(source))
    entries = {name: blob for name, blob in capsule.entries.items() if name != cap.MANIFEST_NAME}
    mutate(entries)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:      # STORED: a tiny tape trips the ratio guard
        for name in sorted(entries):
            zf.writestr(name, entries[name])
        zf.writestr(cap.MANIFEST_NAME, capsule.entries[cap.MANIFEST_NAME])
    return path


def test_the_index_is_sorted_hash_name_lines_and_the_root_is_its_hash():
    entries = {"events/seg-000001.raw": b"one", "chain.json": b"{}", "graph.json": b"{}"}
    lines = integrity.lines(entries)
    names = [line.split("  ", 1)[1] for line in lines.decode().splitlines()]
    assert names == sorted(names) == ["chain.json", "events/seg-000001.raw", "graph.json"]
    assert lines.decode().startswith(f"{hashlib.sha256(b'{}') .hexdigest()}  chain.json")
    assert lines.endswith(b"\n")
    assert integrity.root(lines) == hashlib.sha256(lines).hexdigest()
    assert integrity.lines({}) == b"", "an empty capsule has an empty index, not a blank line"
    assert integrity.root(b"") == hashlib.sha256(b"").hexdigest()


def test_the_index_excludes_itself_and_the_manifest():
    entries = {cap.INTEGRITY_NAME: b"whatever", cap.MANIFEST_NAME: b"{}", "chain.json": b"{}"}
    assert [line.split("  ", 1)[1] for line in integrity.lines(entries).decode().splitlines()] == ["chain.json"]


def test_one_changed_byte_is_reported_by_entry_name(tmp_path):
    source = _capsule(tmp_path)
    target = cap.seg_entry_name(1)

    def flip(entries: dict[str, bytes]) -> None:
        blob = bytearray(entries[target])
        blob[10] ^= 0x01
        entries[target] = bytes(blob)

    tampered = _rewrite(tmp_path / "flip.bbx", source, mutate=flip)
    report = integrity.verify(*_parts(tampered))
    assert not report["ok"]
    assert [item["name"] for item in report["mismatched"]] == [target], report
    assert report["mismatched"][0]["actual"] != report["mismatched"][0]["declared"]
    assert any(problem.startswith(target) for problem in report["problems"]), report["problems"]
    assert report["root"]["ok"] is True, "an edited entry does not move the root over the index"


def _parts(path: Path) -> tuple[dict[str, bytes], bytes, str | None]:
    capsule = cap.read_capsule(str(path)) if _readable(path) else None
    if capsule is not None:
        return capsule.entries, capsule.entries.get(cap.INTEGRITY_NAME, b""), capsule.manifest["integrity"]["root"]
    # An unreadable capsule still has entries; read them without the reader's verdict so the module can be tested alone.
    with zipfile.ZipFile(path) as zf:
        entries = {info.filename: zf.read(info) for info in zf.infolist()}
    manifest = json.loads(entries[cap.MANIFEST_NAME].decode())
    return entries, entries.get(cap.INTEGRITY_NAME, b""), manifest["integrity"]["root"]


def _readable(path: Path) -> bool:
    try:
        cap.read_capsule(str(path))
        return True
    except Exception:
        return False


def test_the_cli_names_the_entry_and_exits_9(tmp_path, capsys):
    source = _capsule(tmp_path)
    target = cap.seg_entry_name(1)

    def flip(entries: dict[str, bytes]) -> None:
        entries[target] = entries[target][:-1] + bytes([entries[target][-1] ^ 0x20])

    tampered = _rewrite(tmp_path / "cli.bbx", source, mutate=flip)
    assert verify_main([str(tampered)]) == 9
    out = capsys.readouterr().out
    assert "FAIL integrity" in out
    assert target in out, out
    assert "does not match the recorded" in out


def test_a_listed_entry_that_is_absent_is_missing_not_ignored(tmp_path):
    source = _capsule(tmp_path)
    target = cap.OTLP_PB_NAME

    def drop(entries: dict[str, bytes]) -> None:
        del entries[target]

    hole = _rewrite(tmp_path / "hole.bbx", source, mutate=drop)
    entries, declared, wanted_root = _parts(hole)
    report = integrity.verify(entries, declared, wanted_root)
    assert report["missing"] == [target] and not report["ok"]
    assert any("absent from the capsule" in problem for problem in report["problems"])


def test_an_entry_nobody_listed_is_unlisted(tmp_path):
    source = _capsule(tmp_path)

    def add(entries: dict[str, bytes]) -> None:
        entries["notes.txt"] = b"an addition the index does not cover"

    extra = _rewrite(tmp_path / "extra.bbx", source, mutate=add)
    entries, declared, wanted_root = _parts(extra)
    report = integrity.verify(entries, declared, wanted_root)
    assert report["unlisted"] == ["notes.txt"] and not report["ok"]


def tampers_with_the_index(entries: dict[str, bytes]) -> None:
    """Edit an entry *and* its line: the per-entry check now agrees, so only the root over the index can catch it."""
    target = cap.OTLP_PB_NAME
    entries[target] = entries[target].replace(b"gpt-4", b"gpt-5")
    rows = entries[cap.INTEGRITY_NAME].decode().splitlines()
    rows = [f"{hashlib.sha256(entries[target]).hexdigest()}  {target}" if row.endswith(f"  {target}") else row
            for row in rows]
    entries[cap.INTEGRITY_NAME] = ("\n".join(rows) + "\n").encode()


def test_an_edited_index_is_caught_by_the_root(tmp_path):
    source = _capsule(tmp_path)
    edited = _rewrite(tmp_path / "index.bbx", source, mutate=tampers_with_the_index)
    entries, declared, wanted_root = _parts(edited)
    report = integrity.verify(entries, declared, wanted_root)
    assert not report["root"]["ok"], report
    assert report["root"]["declared"] != report["root"]["recomputed"]
    assert any("integrity root" in problem for problem in report["problems"])


def test_a_damaged_line_is_reported_not_skipped():
    entries = {"chain.json": b"{}"}
    lines = b"not-a-hash  chain.json\n"
    report = integrity.verify(entries, lines, None)
    assert report["malformed"] == ["not-a-hash  chain.json"]
    assert not report["ok"], "a line the verifier cannot read must not read as agreement"


def test_a_missing_index_is_reported_by_the_reader(tmp_path):
    source = _capsule(tmp_path)
    capsule = cap.read_capsule(str(source))
    entries = {name: blob for name, blob in capsule.entries.items()
               if name not in (cap.INTEGRITY_NAME, cap.MANIFEST_NAME)}
    bare = tmp_path / "bare.bbx"
    with zipfile.ZipFile(bare, "w", zipfile.ZIP_STORED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])
        zf.writestr(cap.MANIFEST_NAME, capsule.entries[cap.MANIFEST_NAME])
    check = cap.read_capsule(str(bare))
    assert check.root_ok is False
    assert any("INTEGRITY.sha256 is missing" in problem for problem in check.problems)


def test_the_module_is_the_one_the_reader_and_verify_use(tmp_path):
    """One implementation, three callers: `read_capsule`, `manifest.verify`, and `blackbox verify`."""
    capsule = cap.read_capsule(str(_capsule(tmp_path)))
    report = integrity.verify(capsule.entries, capsule.entries[cap.INTEGRITY_NAME], capsule.manifest["integrity"]["root"])
    assert report["ok"] and capsule.root_ok
    checks = {check["check"]: check for check in manifest_mod.verify(capsule)["checks"]}
    assert checks["integrity"]["ok"] is True
    assert checks["integrity"]["recomputed"]["matched"] == report["matched"] > 0
    assert checks["integrity"]["recomputed"]["root_ok"] is True


@pytest.mark.parametrize("name", ["events/seg-000001.raw", "chain.json", "graph.json", "otlp/spans.pb"])
def test_every_entry_the_capsule_ships_is_covered(tmp_path, name):
    capsule = cap.read_capsule(str(_capsule(tmp_path)))
    digests, _malformed = integrity.parse(capsule.entries[cap.INTEGRITY_NAME])
    assert name in digests, sorted(digests)
    assert digests[name] == hashlib.sha256(capsule.entries[name]).hexdigest()
