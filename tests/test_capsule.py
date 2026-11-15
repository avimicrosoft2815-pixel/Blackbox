"""P2-10 — capsule v0: raw frames + manifest, atomic `.partial` → `replace`, reproducible bytes.

The plan's acceptance is three sentences, and each is a test here:

* **the capsule opens** — a container written from a real tape reads back with its segments byte-identical;
* **the sha256 root verifies** — and stops verifying when one byte of one entry changes;
* **re-sealing identical inputs yields identical bytes** — compared as bytes, not as "equivalent" content.

The hostile-input checks that guard `read_capsule` are borrowed from §13.4 by ordering: the reader validates names
and sizes *before* it trusts a single entry, so a capsule that would escape its root never gets read at all.
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
from blackbox.errors import BlackBoxError
from blackbox.tape import segments
from blackbox.tape.writer import TapeWriter


def _tape(tmp_path: Path) -> tuple[TapeWriter, dict[str, bytes]]:
    """A small real tape, returned with the raw segment bodies a v0 capsule stores."""
    w = TapeWriter(str(tmp_path / "data"), segment_bytes=C.MIN_SEGMENT_BYTES)
    w.batch_bytes = 0
    w.batch_ms = 0.0
    w.open()
    for _ in range(30):
        w.append(C.KIND_RUN_START, b"a" * 2000)
        w.publish()
    w.publish()
    bodies = {}
    for n in segments.list_segments(w.tape_dir):
        bodies[cap.seg_entry_name(n)] = Path(segments.seg_path(w.tape_dir, n)).read_bytes()
    return w, bodies


def _manifest(w: TapeWriter, bodies: dict[str, bytes]) -> dict:
    return {
        "capsule_id": "c_test_0001",
        "created_ns": 0,
        "tool": {"name": "blackbox", "version": "0.1.0.dev0", "git": "test"},
        "run": {"run_id": "r_test", "status": "recovered", "completeness": "full"},
        "capture": {"coverage": "FULL", "providers": []},
        "redaction": {"profile_id": "none", "content_mode": "full"},
        "gaps": [],
        "counts": {"events": w.head.head_seq, "segments": len(bodies)},
    }


def test_a_capsule_opens_and_its_root_verifies(tmp_path: Path) -> None:
    w, bodies = _tape(tmp_path)
    try:
        dest = str(tmp_path / "capsules" / "c_test_0001.bbx")
        out = cap.write_capsule(dest, bodies, _manifest(w, bodies))
        assert Path(dest).exists()
        assert out["unchanged"] is False
    finally:
        w.close()
    capsule = cap.read_capsule(dest)
    assert capsule.root_ok is True, capsule.problems
    assert capsule.manifest["format"] == "bbx.capsule"
    assert capsule.manifest["integrity"]["root"] == out["root"]
    assert capsule.manifest["counts"]["segments"] == len(bodies)
    for name, blob in bodies.items():
        assert capsule.entries[name] == blob, "raw frames must be byte-identical to the tape"
        assert cap.frame_count(blob) > 0


def test_resealing_the_same_inputs_produces_identical_bytes(tmp_path: Path) -> None:
    w, bodies = _tape(tmp_path)
    try:
        manifest = _manifest(w, bodies)
        a = str(tmp_path / "a.bbx")
        b = str(tmp_path / "b.bbx")
        cap.write_capsule(a, bodies, manifest)
        cap.write_capsule(b, bodies, manifest)
    finally:
        w.close()
    assert Path(a).read_bytes() == Path(b).read_bytes(), "a capsule must be reproducible byte for byte"
    again = cap.write_capsule(a, bodies, manifest)
    assert again["unchanged"] is True
    assert Path(a).read_bytes() == Path(b).read_bytes()


def test_entry_metadata_is_fixed(tmp_path: Path) -> None:
    w, bodies = _tape(tmp_path)
    try:
        dest = str(tmp_path / "c.bbx")
        cap.write_capsule(dest, bodies, _manifest(w, bodies))
    finally:
        w.close()
    infos = cap.zinfo_unpack(dest)
    assert all(i["date_time"] == cap.FIXED_DATE for i in infos), "timestamps must not leak the wall clock"
    assert all(i["external_attr"] == cap.FIXED_MODE for i in infos)
    names = [i["name"] for i in infos]
    assert names[-1] == cap.MANIFEST_NAME, "the manifest is written last"
    assert names == [*sorted(names[:-1]), cap.MANIFEST_NAME]
    assert {i["compress_type"] for i in infos if i["name"].endswith(".raw")} == {zipfile.ZIP_STORED}


def test_one_changed_byte_breaks_the_root(tmp_path: Path) -> None:
    """Tampering means changing an entry *without* updating INTEGRITY — that is the case that must fail."""
    w, bodies = _tape(tmp_path)
    try:
        dest = str(tmp_path / "c.bbx")
        cap.write_capsule(dest, bodies, _manifest(w, bodies))
    finally:
        w.close()
    good = cap.read_capsule(dest)
    first = sorted(bodies)[0]
    tampered = bytearray(good.entries[first])
    tampered[len(tampered) // 2] ^= 0xFF
    forged = tmp_path / "forged.bbx"
    with zipfile.ZipFile(forged, "w") as zf:
        for name in sorted(good.entries):
            blob = bytes(tampered) if name == first else good.entries[name]
            info = zipfile.ZipInfo(name, date_time=cap.FIXED_DATE)
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, blob)
    capsule = cap.read_capsule(str(forged))
    assert capsule.root_ok is False
    assert any(first in p and "does not match" in p for p in capsule.problems), capsule.problems


def test_a_visible_capsule_is_never_partial(tmp_path: Path) -> None:
    """The `.partial` name is the only thing that exists until the rename, and no `.partial` is left behind."""
    w, bodies = _tape(tmp_path)
    directory = tmp_path / "capsules"
    try:
        dest = str(directory / "c.bbx")
        cap.write_capsule(dest, bodies, _manifest(w, bodies))
    finally:
        w.close()
    leftovers = [p.name for p in directory.iterdir() if p.name.endswith(".partial")]
    assert leftovers == []
    assert cap.read_capsule(dest).root_ok


def test_reading_a_capsule_rejects_an_entry_that_escapes_the_root(tmp_path: Path) -> None:
    dest = tmp_path / "evil.bbx"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("../../etc/passwd", b"root:x:0:0")
        zf.writestr(cap.MANIFEST_NAME, b"{}")
    with pytest.raises(BlackBoxError) as exc:
        cap.read_capsule(str(dest))
    assert exc.value.code == "BBX-E403"


def test_duplicate_entries_are_rejected_case_folded(tmp_path: Path) -> None:
    dest = tmp_path / "dup.bbx"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("events/seg-000001.raw", b"a")
        zf.writestr("events/SEG-000001.RAW", b"b")
        zf.writestr(cap.MANIFEST_NAME, b"{}")
    with pytest.raises(BlackBoxError) as exc:
        cap.read_capsule(str(dest))
    assert exc.value.code == "BBX-E404"


def test_a_missing_or_newer_manifest_is_a_named_error(tmp_path: Path) -> None:
    no_manifest = tmp_path / "nomanifest.bbx"
    with zipfile.ZipFile(no_manifest, "w") as zf:
        zf.writestr("events/seg-000001.raw", b"a")
    with pytest.raises(BlackBoxError) as exc:
        cap.read_capsule(str(no_manifest))
    assert exc.value.code == "BBX-E407"

    newer = tmp_path / "newer.bbx"
    blob, _manifest = cap.capsule_bytes({"events/seg-000001.raw": b"a"}, {"capsule_id": "c"})
    newer.write_bytes(blob)
    assert cap.read_capsule(str(newer)).manifest["format_version"] == 0
    good = cap.read_capsule(str(newer))
    bumped = dict(good.manifest)
    bumped["format_version"] = cap.CAPSULE_FORMAT_VERSION + 1
    with zipfile.ZipFile(newer, "w") as zf:
        for name in sorted(good.entries):
            body = json.dumps(bumped, sort_keys=True).encode() + b"\n" if name == cap.MANIFEST_NAME \
                else good.entries[name]
            info = zipfile.ZipInfo(name, date_time=cap.FIXED_DATE)
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, body)
    with pytest.raises(BlackBoxError) as exc:
        cap.read_capsule(str(newer))
    assert exc.value.code == "BBX-E407"


def test_integrity_root_covers_every_data_entry(tmp_path: Path) -> None:
    """The root is a hash of the sorted `<sha256>  <name>` lines, so both directions can be recomputed."""
    entries = {"events/seg-000001.raw": b"one", "chain.json": b"{}"}
    blob, manifest = cap.capsule_bytes(entries, {"capsule_id": "c"})
    with zipfile.ZipFile(__import__("io").BytesIO(blob)) as zf:
        lines = zf.read(cap.INTEGRITY_NAME)
    expected = ("\n".join([f"{hashlib.sha256(b'{}').hexdigest()}  chain.json",
                           f"{hashlib.sha256(b'one').hexdigest()}  events/seg-000001.raw"]) + "\n")
    assert lines.decode() == expected
    assert manifest["integrity"]["root"] == hashlib.sha256(lines).hexdigest()
    assert cap.integrity_lines({"chain.json": b"{}", "events/seg-000001.raw": b"one"}) == lines
