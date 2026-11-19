"""P4-12 — a reader written from the document, checked against the one written from the code.

`tools/readers/bbx-read.mjs` implements the capsule format in Node from `docs/format.md` (§4 frames, §5 segments, §8.1
capsule) and the `.bbx` refusal table (§13.4 of the architecture doc). It imports nothing from this project — only
`node:fs`, `node:zlib` and `node:crypto` — so it can answer a question the Python reader cannot answer about itself:
**is the document enough?**

Three things are checked here, and they are three different claims:

* the two readers **agree** on a real capsule: same entries, same integrity root, same frames, same kinds;
* they **refuse the same hostile capsules with the same codes** — same inputs, same `BBX-Exxx`, from two codebases that
  share no line;
* a tampered capsule is a **reported** failure, not a crash: exit 4 with the mismatch named, never a stack trace.

A disagreement is not automatically the Node reader's fault — writing it is what found the two documentation defects
recorded in `docs/defects.md` (85): the document did not state `manifest.format`/`format_version`, and §4's table put
the frame trailer at `48 + payload_len` when the writer puts it at `frame_len - 8`.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_capsule_attacks import _manifest, _mark_members_encrypted, _write
from test_capsule_determinism import _capsule_bytes, _record

from blackbox.cap import format as cap
from blackbox.errors import BlackBoxError

READER = ROOT / "tools" / "readers" / "bbx-read.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="no node on PATH")


def _read(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["node", str(READER), str(path), "--json"], capture_output=True, text=True, timeout=120)


def _real_capsule(tmp_path: Path) -> tuple[Path, str]:
    data_dir = tmp_path / "data"
    uuid_hex = _record(data_dir)
    _, root = _capsule_bytes(data_dir, uuid_hex)
    return next(data_dir.glob("capsules/*.bbx")), root


# ── the two readers agree ────────────────────────────────────────────────────
def test_the_independent_reader_verifies_a_real_capsule(tmp_path: Path) -> None:
    capsule_path, python_root = _real_capsule(tmp_path)
    python_side = cap.read_capsule(str(capsule_path))
    done = _read(capsule_path)
    assert done.returncode == 0, done.stderr or done.stdout
    node = json.loads(done.stdout)

    assert node["ok"] is True
    assert node["integrity"]["root"] == python_root, "the two readers disagree about the root"
    assert node["integrity"]["root_ok"] is True
    assert node["integrity"]["mismatched"] == []
    assert len(node["entries"]) == len(python_side.entries)
    assert {entry["name"] for entry in node["entries"]} == set(python_side.entries)

    # The frames: same count, same kinds, same seq order — decoded from the bytes by a second implementation.
    assert node["frames"]["count"] == python_side.manifest["counts"]["events"]
    assert node["frames"]["non_increasing_seq"] is False
    python_frames = cap.frame_count(python_side.entry("events/seg-000001.raw"))
    assert node["frames"]["count"] == python_frames
    assert sum(node["frames"]["kinds"].values()) == python_frames
    assert node["manifest"]["unknown_keys"] == [], "the document's key list is behind the writer"


def test_the_reader_reports_what_the_manifest_declares(tmp_path: Path) -> None:
    capsule_path, _ = _real_capsule(tmp_path)
    python_side = cap.read_capsule(str(capsule_path))
    node = json.loads(_read(capsule_path).stdout)
    assert node["manifest"]["capsule_id"] == python_side.manifest["capsule_id"]
    assert node["manifest"]["run_id"] == python_side.manifest["run"]["run_id"]
    assert node["manifest"]["run_status"] == python_side.manifest["run"]["status"]
    assert node["manifest"]["format_version"] == python_side.manifest["format_version"]


# ── the two readers refuse the same things ──────────────────────────────────
ATTACKS = [
    ("traversal", {"entries": [("../escape.bin", b"x")]}, "BBX-E403"),
    ("absolute", {"entries": [("/etc/shadow", b"x")]}, "BBX-E403"),
    ("backslash", {"entries": [("events\\..\\x", b"x")]}, "BBX-E403"),
    ("case-folded duplicate", {"entries": [("a.json", b"a"), ("A.json", b"b")]}, "BBX-E404"),
    ("bomb", {"entries": [("events/bomb.bin", b"\0" * (2 << 20))]}, "BBX-E405"),
    ("symlink member", {"entries": [("link", b"/etc/passwd")],
                        "decorate": {"link": {"external_attr": 0o120777 << 16}}}, "BBX-E403"),
    ("manifest is a list", {"manifest": b"[]", "entries": [("events/seg-000001.raw", b"x")]}, "BBX-E407"),
    ("manifest names an absent part",
     {"manifest": _manifest(derived_parts=[{"name": "graph.json", "sha256": "0" * 64}]),
      "entries": [("events/seg-000001.raw", b"x")]}, "BBX-E408"),
]


@pytest.mark.parametrize("label,kwargs,code", ATTACKS, ids=[a[0] for a in ATTACKS])
def test_the_two_readers_refuse_with_the_same_code(tmp_path: Path, label: str, kwargs: dict, code: str) -> None:
    entries = kwargs.pop("entries")
    stored = {cap.MANIFEST_NAME} if "manifest" in kwargs else None
    path = _write(tmp_path, entries, stored=stored, name=f"{label.replace(' ', '-')}.bbx", **kwargs)

    with pytest.raises(BlackBoxError) as caught:
        cap.read_capsule(str(path))
    assert caught.value.code == code, f"Python reader: {caught.value.code} {caught.value}"

    done = _read(path)
    assert done.returncode == 3, f"node exited {done.returncode}: {done.stdout[:400]}{done.stderr[:400]}"
    node = json.loads(done.stdout)
    assert node["refused"] == code, f"{label}: node said {node['refused']} ({node['reason']}), Python said {code}"


def test_an_encrypted_member_is_refused_by_both(tmp_path: Path) -> None:
    path = _mark_members_encrypted(_write(tmp_path, [("events/seg-000001.raw", b"x")], manifest=_manifest()))
    with pytest.raises(BlackBoxError) as caught:
        cap.read_capsule(str(path))
    assert caught.value.code == "BBX-E406"
    node = json.loads(_read(path).stdout)
    assert node["refused"] == "BBX-E406", node


def test_a_file_that_is_not_a_zip_is_refused_by_both(tmp_path: Path) -> None:
    path = tmp_path / "not-a-capsule.bbx"
    path.write_bytes(b"this is not a zip file, it is a sentence\n" * 4)
    with pytest.raises(BlackBoxError) as caught:
        cap.read_capsule(str(path))
    assert caught.value.code == "BBX-E402"
    node = json.loads(_read(path).stdout)
    assert node["refused"] == "BBX-E402", node


# ── tampering is reported, not fatal ────────────────────────────────────────
def _rewrite_with_manifest(path: Path, mutate) -> Path:
    """Rebuild a capsule with one manifest field changed, leaving every entry's bytes else untouched."""
    with zipfile.ZipFile(path) as zf:
        entries = {info.filename: zf.read(info.filename) for info in zf.infolist()}
    manifest = json.loads(entries[cap.MANIFEST_NAME])
    mutate(manifest)
    entries[cap.MANIFEST_NAME] = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    out = path.with_name("tampered-" + path.name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in [*sorted(n for n in entries if n != cap.MANIFEST_NAME), cap.MANIFEST_NAME]:
            info = zipfile.ZipInfo(name, date_time=cap.FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED if name.endswith(".json") else zipfile.ZIP_STORED
            info.external_attr = cap.FIXED_MODE
            zf.writestr(info, entries[name])
    out.write_bytes(buf.getvalue())
    return out


def test_a_root_that_does_not_match_is_exit_4_with_the_reason(tmp_path: Path) -> None:
    capsule_path, _ = _real_capsule(tmp_path)
    tampered = _rewrite_with_manifest(capsule_path, lambda m: m["integrity"].__setitem__("root", "0" * 64))
    done = _read(tampered)
    assert done.returncode == 4, done.stdout[:400]
    node = json.loads(done.stdout)
    assert node["ok"] is False
    assert node["integrity"]["root_ok"] is False
    assert node["integrity"]["root"] != node["integrity"]["declared_root"]
    # The Python side reports the same thing the same way it always has: a root that does not match is a *reported*
    # problem on a capsule that still opens (`root_ok` false), not a refusal — a user whose file drifted wants the
    # timeline with a red banner, not an exit code. The two readers agree on the fact and on the severity.
    python_side = cap.read_capsule(str(tampered))
    assert python_side.root_ok is False
    assert any("root" in problem for problem in python_side.problems), python_side.problems


# ── the reader is independent, and says so ─────────────────────────────────
def test_the_reader_imports_nothing_from_this_project() -> None:
    source = READER.read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith("import ")]
    assert imports, "the reader should have imports"
    for line in imports:
        assert "from 'node:" in line, f"a non-Node import: {line}"
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) or "require(" in stripped or "await import(" in stripped:
            assert "blackbox" not in stripped, f"the independent reader imports project code: {stripped}"
    assert "docs/format.md" in source, "the reader must name the document it was written from"


def test_the_reader_reproduces_the_published_crc32_vectors() -> None:
    """P4-12's own acceptance: the vectors are numbers the reader did not produce."""
    vectors = ROOT / "tests" / "conformance" / "vectors" / "crc32.json"
    done = subprocess.run(["node", str(READER), "--vectors", str(vectors), "--json"],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[:400] + done.stderr[:400]
    report = json.loads(done.stdout)
    assert report["vectors"] >= 7, report
    assert report["failures"] == [], report
    assert report["algorithm"] == "CRC-32/ISO-HDLC"


def test_the_reader_runs_on_a_machine_with_no_blackbox_installed(tmp_path: Path) -> None:
    """`node` with an empty environment: the reader cannot be leaning on PYTHONPATH or the installed package."""
    capsule_path, python_root = _real_capsule(tmp_path)
    done = subprocess.run(["node", str(READER), str(capsule_path), "--json"], capture_output=True, text=True,
                          env={"PATH": "/usr/local/bin:/usr/bin:/bin"}, timeout=120)
    assert done.returncode == 0, done.stderr[:600]
    assert json.loads(done.stdout)["integrity"]["root"] == python_root
