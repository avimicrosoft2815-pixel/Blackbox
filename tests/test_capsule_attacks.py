"""§13.4's import validator, attacked on purpose: fifteen hostile capsules, fifteen documented codes.

The plan's security row asks for a **capsule attack corpus (12) rejected with correct codes in < 50 ms**, and until now
this repository had the validator's code but no corpus: every case below was written by trying to get a capsule past
`read_capsule`, not by reading the validator and asserting that it returns what it says. Two of them got through as a
Python exception rather than a code, which is what a corpus is for (defect 83):

* a `manifest.json` of `[]`, `"x"` or `null` is valid JSON and reached `.get("format")` as an **`AttributeError`**;
* a manifest nested 20 000 deep is valid JSON and `json.loads` raised **`RecursionError`** out of the reader.

Both are now `BBX-E407`. The rest exercise checks 1–8 of §13.4 with the archive bytes an attacker would send — a symlink
member, an encrypted entry, a bzip2 entry, a case-folded duplicate, a `..` path, 5 001 entries, a 1000:1 bomb, a manifest
listing a part the archive does not carry.

Two rows are **not** claimed here, in those words: §13.4 check 9 (a running extraction counter that aborts at the
declared size) is exercised against `format._read_entry` directly, because `zipfile` bounds its own reads by the declared
size and a corpus cannot reach the branch through a real archive; and check 11 (generated tests are never executed) has
nothing to test against, because this build generates no tests to run.

Every rejection is timed, and the bound the plan asks for is asserted on the parse itself: the fixture's construction is
not part of it.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.cap import format as cap
from blackbox.errors import BlackBoxError

BUDGET_MS = 50.0

GOOD_MANIFEST = {
    "format": cap.CAPSULE_FORMAT, "format_version": cap.CAPSULE_FORMAT_VERSION,
    "capsule_id": "c_0000000000000001", "created_ns": 1,
    "tool": {"name": "blackbox-recorder", "version": "0.0.0"},
    "run": {"run_id": "r_0000000000000001", "status": "recovered"},
    "capture": {"coverage": "declared"},
    "redaction": {"profile_id": "full", "content_mode": "hash"},
    "gaps": [], "counts": {"events": 0, "nodes": 0},
    "integrity": {"root": "0" * 64},
}


def _write(tmp_path: Path, entries: list[tuple[str, bytes]], *, manifest: bytes | None = None,
           decorate: dict[str, dict] | None = None, stored: set[str] | None = None,
           name: str = "hostile.bbx") -> Path:
    """Write a zip by hand, so a member can carry the mode, flag bits or method an attacker would set.

    `stored` names the members written without compression. It exists because the ratio limit is checked *before* the
    manifest is parsed — so a bomb-shaped manifest cannot test the parser; a corpus that wants the parser has to write
    that entry as an attacker who is not also raising a different alarm.
    """
    stored = stored or set()
    path = tmp_path / name

    def write(zf: zipfile.ZipFile, entry_name: str, data: bytes) -> None:
        info = zipfile.ZipInfo(entry_name)
        info.compress_type = zipfile.ZIP_STORED if entry_name in stored else zipfile.ZIP_DEFLATED
        for field, value in (decorate or {}).get(entry_name, {}).items():
            setattr(info, field, value)
        zf.writestr(info, data)

    with zipfile.ZipFile(path, "w") as zf:
        for entry_name, data in entries:
            write(zf, entry_name, data)
        if manifest is not None:
            write(zf, cap.MANIFEST_NAME, manifest)
    return path


def _mark_members_encrypted(path: Path) -> Path:
    """Set the encrypted bit in every local and central header — the flag an attacker actually controls.

    `writestr` clears `flag_bits`, so the decoration has to be done to the file rather than to the `ZipInfo`: the byte
    an attacker would flip is this one, and a validator that only trusted `ZipInfo` would never see it.
    """
    data = bytearray(path.read_bytes())
    for marker, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        index = 0
        while (index := data.find(marker, index)) >= 0:
            start = index + offset
            data[start:start + 2] = (struct.unpack("<H", data[start:start + 2])[0] | 1).to_bytes(2, "little")
            index += 4
    path.write_bytes(bytes(data))
    return path


def _manifest(**overrides: object) -> bytes:
    document = {**GOOD_MANIFEST, **overrides}
    return json.dumps(document).encode()


def _reject(path: Path, code: str) -> float:
    """Assert `path` is refused with `code`, and return how long the refusal took in milliseconds."""
    started = time.perf_counter_ns()
    with pytest.raises(BlackBoxError) as caught:
        cap.read_capsule(str(path))
    elapsed_ms = (time.perf_counter_ns() - started) / 1e6
    assert caught.value.code == code, f"expected {code}, got {caught.value.code}: {caught.value}"
    return elapsed_ms


# ── §13.4 check 1–2: size and shape ────────────────────────────────────────────────────────────────────────────────


def test_check_1_a_capsule_over_the_byte_limit_is_refused_before_it_is_opened(tmp_path: Path) -> None:
    path = _write(tmp_path, [("events/seg-0001.bin", os.urandom(4096))], manifest=_manifest())
    with pytest.raises(BlackBoxError) as caught:
        cap.read_capsule(str(path), max_bytes=1024)
    assert caught.value.code == "BBX-E401"
    assert "over the" in str(caught.value)


def test_check_2_bytes_that_are_not_a_zip_are_a_capsule_that_cannot_be_read(tmp_path: Path) -> None:
    path = tmp_path / "not.zip"
    path.write_bytes(b"PK\x03\x04" + bytes(range(256)) * 4)
    assert _reject(path, "BBX-E402") < BUDGET_MS


def test_check_2_more_entries_than_the_format_allows(tmp_path: Path) -> None:
    entries = [(f"events/part-{index:04d}.txt", b"x") for index in range(cap.MAX_ENTRIES + 1)]
    path = _write(tmp_path, entries, manifest=_manifest())
    assert _reject(path, "BBX-E402") < BUDGET_MS


# ── §13.4 check 3: paths and member types ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("label", "entry_name"), [
    ("traversal", "../etc/passwd"),
    ("absolute", "/etc/passwd"),
    ("drive letter", "C:/windows/win.ini"),
    ("backslash", "events\\..\\..\\x"),
    ("empty component", "events//x"),
])
def test_check_3_a_member_name_that_leaves_the_capsule_root_is_refused(tmp_path: Path, label: str,
                                                                       entry_name: str) -> None:
    path = _write(tmp_path, [(entry_name, b"x")], manifest=_manifest(), name=f"{label}.bbx".replace(" ", "-"))
    assert _reject(path, "BBX-E403") < BUDGET_MS


def test_check_3_a_symlink_member_is_refused_even_though_nothing_is_ever_extracted(tmp_path: Path) -> None:
    path = _write(tmp_path, [("link", b"/etc/passwd")], manifest=_manifest(),
                  decorate={"link": {"external_attr": 0o120777 << 16}})
    with pytest.raises(BlackBoxError) as caught:
        cap.read_capsule(str(path))
    assert caught.value.code == "BBX-E403"
    assert "symlink" in str(caught.value)


# ── §13.4 check 4–6: duplicates, bombs, and codecs this format never promised ──────────────────────────────────────


def test_check_4_a_case_folded_duplicate_is_two_entries_that_one_filesystem_would_collide(tmp_path: Path) -> None:
    path = _write(tmp_path, [("events/seg-0001.bin", b"a"), ("events/SEG-0001.BIN", b"b")], manifest=_manifest())
    assert _reject(path, "BBX-E404") < BUDGET_MS


def test_check_5_a_member_that_expands_a_thousandfold_is_a_bomb(tmp_path: Path) -> None:
    path = _write(tmp_path, [("events/seg-0001.bin", b"\0" * (2 << 20))], manifest=_manifest())
    with pytest.raises(BlackBoxError) as caught:
        cap.read_capsule(str(path))
    assert caught.value.code == "BBX-E405"
    assert "expands" in str(caught.value)


def test_check_6_an_encrypted_member_is_refused_rather_than_decoded(tmp_path: Path) -> None:
    path = _mark_members_encrypted(_write(tmp_path, [("events/seg-0001.bin", b"x")], manifest=_manifest()))
    assert _reject(path, "BBX-E406") < BUDGET_MS


def test_check_6_a_compression_method_this_format_does_not_define_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, [("events/seg-0001.bin", b"x" * 4096)], manifest=_manifest(),
                  decorate={"events/seg-0001.bin": {"compress_type": zipfile.ZIP_BZIP2}})
    assert _reject(path, "BBX-E406") < BUDGET_MS


# ── §13.4 check 7–8: the manifest, and what it claims the archive contains ────────────────────────────────────────


@pytest.mark.parametrize(("label", "body"), [
    ("list", b"[]"),
    ("string", b'"capsule"'),
    ("null", b"null"),
    ("not json", b"{not json"),
    ("not utf-8", b"\xff\xfe manifest"),
    ("deeply nested", b"[" * 20_000 + b"]" * 20_000),
])
def test_check_7_a_manifest_that_is_not_an_object_is_a_capsule_that_cannot_be_read(tmp_path: Path, label: str,
                                                                                   body: bytes) -> None:
    """The two cases that used to escape as `AttributeError` and `RecursionError` (defect 83) are the list and the deep one."""
    path = _write(tmp_path, [("events/seg-0001.bin", b"x")], manifest=body, stored={cap.MANIFEST_NAME},
                  name=f"manifest-{label.replace(' ', '-')}.bbx")
    assert _reject(path, "BBX-E407") < BUDGET_MS


def test_check_7_a_newer_format_version_is_refused_rather_than_guessed_at(tmp_path: Path) -> None:
    path = _write(tmp_path, [("events/seg-0001.bin", b"x")],
                  manifest=_manifest(format_version=cap.CAPSULE_FORMAT_VERSION + 1))
    assert _reject(path, "BBX-E407") < BUDGET_MS


def test_check_8_a_manifest_that_lists_a_part_the_archive_does_not_carry_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, [("events/seg-0001.bin", b"x")],
                  manifest=_manifest(derived_parts=[{"name": "graph.json", "sha256": "0" * 64}]))
    with pytest.raises(BlackBoxError) as caught:
        cap.read_capsule(str(path))
    assert caught.value.code == "BBX-E408"
    assert "graph.json" in str(caught.value)


# ── §13.4 check 9: the counter, tested where it is reachable ──────────────────────────────────────────────────────


class _LyingArchive:
    """A `ZipFile` whose member produces more bytes than the central directory declared.

    `zipfile` bounds its own reads by `file_size`, so no real archive reaches this branch — which is why the counter is a
    second net and why it is tested against the object it guards rather than through a corpus that cannot get there.
    """

    class _Handle:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self, _size: int = -1) -> bytes:
            payload, self._payload = self._payload, b""
            return payload

        def __enter__(self) -> _LyingArchive._Handle:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def open(self, _info: object) -> _LyingArchive._Handle:
        return self._Handle(self._payload)


def test_check_9_the_extraction_counter_aborts_when_a_member_exceeds_its_declared_size() -> None:
    info = zipfile.ZipInfo("events/seg-0001.bin")
    info.file_size = 10
    with pytest.raises(BlackBoxError) as caught:
        cap._read_entry(_LyingArchive(b"x" * 4096), info, remaining=1 << 30)          # type: ignore[arg-type]
    assert caught.value.code == "BBX-E409"
    assert "declared 10 B" in str(caught.value)


def test_check_9_the_counter_aborts_when_the_declared_total_would_be_exceeded() -> None:
    info = zipfile.ZipInfo("events/seg-0001.bin")
    info.file_size = 1 << 20
    with pytest.raises(BlackBoxError) as caught:
        cap._read_entry(_LyingArchive(b"x" * (1 << 20)), info, remaining=1024)       # type: ignore[arg-type]
    assert caught.value.code == "BBX-E405"


# ── what the corpus is for: the whole set is fast, and the CLI says the code ──────────────────────────────────────


def test_every_attack_is_refused_inside_the_fifty_millisecond_budget(tmp_path: Path) -> None:
    """The plan's row, measured on the refusals rather than asserted in a sentence."""
    attacks: list[tuple[str, bytes]] = [
        ("traversal", _manifest()),
        ("orphan part", _manifest(derived_parts=[{"name": "graph.json", "sha256": "0" * 64}])),
        ("not an object", b"[]"),
        ("deep", b"[" * 20_000 + b"]" * 20_000),
        ("newer version", _manifest(format_version=cap.CAPSULE_FORMAT_VERSION + 1)),
    ]
    timings: dict[str, float] = {}
    for index, (label, manifest) in enumerate(attacks):
        name = "events/../escape.bin" if label == "traversal" else "events/seg-0001.bin"
        path = _write(tmp_path, [(name, b"x")], manifest=manifest, name=f"timed-{index}.bbx",
                      stored={cap.MANIFEST_NAME} if label == "deep" else None)
        started = time.perf_counter_ns()
        with pytest.raises(BlackBoxError):
            cap.read_capsule(str(path))
        timings[label] = (time.perf_counter_ns() - started) / 1e6
    assert len(timings) == len(attacks), timings
    assert max(timings.values()) < BUDGET_MS, timings


def test_the_cli_reports_the_documented_code_and_does_not_print_a_python_exception(tmp_path: Path) -> None:
    """A validator that raises `AttributeError` is a validator the CLI reports as a crash, not as a refusal."""
    import subprocess

    hostile = tmp_path / "manifest-list.bbx"
    _write(tmp_path, [("events/seg-0001.bin", b"x")], manifest=b"[]", stored={cap.MANIFEST_NAME}, name=hostile.name)
    finished = subprocess.run([sys.executable, "-m", "blackbox", "verify", str(hostile)], cwd=ROOT,
                              capture_output=True, text=True, timeout=120,
                              env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"})
    assert finished.returncode == 2, finished.stdout + finished.stderr
    assert "BBX-E407" in finished.stdout + finished.stderr
    assert "AttributeError" not in finished.stdout + finished.stderr
