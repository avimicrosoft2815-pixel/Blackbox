"""The library's fast path: `read_manifest` must refuse exactly what `read_capsule` refuses.

The library lists a hundred runs by reading a hundred manifests, so it cannot decompress and hash every capsule to draw a
table. That optimisation is only safe if it cannot become a *second, weaker reader*: a hostile archive refused by
`read_capsule` but accepted by `read_manifest` would be a file the app lists, opens and trusts.

This file holds the two proofs that make it safe, and nothing that depends on a display:

1. **same refusals** — the whole hostile corpus from `tests/test_capsule_attacks.py`, run through both readers, with the
   same error code from each;
2. **less work** — a capsule with a large segment costs `read_capsule` a decompression and a hash, and `read_manifest`
   almost nothing. The measurement is a ratio with a wide margin, because the point is "it does not read the bodies",
   not a benchmark.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_capsule_attacks import GOOD_MANIFEST, _manifest, _mark_members_encrypted, _write
from test_capsule_determinism import _capsule_bytes, _record

from blackbox.cap import format as cap
from blackbox.errors import BlackBoxError


def _code(reader, path: Path, **kwargs) -> str | None:
    """The refusal code from `reader`, or None when it accepted the file."""
    try:
        reader(str(path), **kwargs)
    except BlackBoxError as exc:
        return exc.code
    return None


#: `(label, entries, manifest body, stored members)` — the failure the corpus is aiming at, and the archive that causes it.
CORPUS = [
    ("no manifest", [("events/seg-0001.bin", b"x")], None, set()),
    ("manifest is a list", [("events/seg-0001.bin", b"x")], b"[]", set()),
    ("manifest is deep nesting", [("events/seg-0001.bin", b"x")], b"[" * 20_000 + b"]" * 20_000,
     {cap.MANIFEST_NAME}),
    ("manifest is not utf-8", [("events/seg-0001.bin", b"x")], b"\xff\xfe\x00garbage", set()),
    ("format is not the capsule format",
     [("events/seg-0001.bin", b"x")], json.dumps({**GOOD_MANIFEST, "format": "something-else"}).encode(), set()),
    ("format version is newer", [("events/seg-0001.bin", b"x")],
     _manifest(format_version=cap.CAPSULE_FORMAT_VERSION + 1), set()),
    ("entry escapes the root", [("events/../escape.bin", b"x")], _manifest(), set()),
    ("entry is absolute", [("/etc/passwd", b"x")], _manifest(), set()),
    ("case-folded duplicate", [("events/seg-0001.bin", b"a"), ("events/SEG-0001.BIN", b"b")], _manifest(), set()),
    ("segment expands a thousandfold", [("events/seg-0001.bin", b"\0" * (2 << 20))], _manifest(), set()),
    ("manifest lists a part the archive lacks",
     [("events/seg-0001.bin", b"x")],
     _manifest(derived_parts=[{"name": "graph.json", "sha256": "0" * 64}]), set()),
]


@pytest.mark.parametrize(("label", "entries", "manifest_body", "stored"), CORPUS,
                         ids=[case[0] for case in CORPUS])
def test_both_readers_refuse_with_the_same_code(tmp_path: Path, label: str, entries: list[tuple[str, bytes]],
                                               manifest_body: bytes | None, stored: set[str]) -> None:
    path = _write(tmp_path, entries, manifest=manifest_body, stored=stored, name="hostile.bbx")
    fast = _code(cap.read_manifest, path)
    full = _code(cap.read_capsule, path)
    assert fast is not None, f"{label}: the library reader accepted what the capsule reader must refuse"
    assert fast == full, f"{label}: read_manifest said {fast}, read_capsule said {full}"


def test_bytes_that_are_not_a_zip_are_refused_the_same_way(tmp_path: Path) -> None:
    path = tmp_path / "not-a-zip.bbx"
    path.write_bytes(b"these are not zip bytes")
    assert _code(cap.read_manifest, path) == _code(cap.read_capsule, path) == "BBX-E402"


def test_a_symlink_member_is_refused_by_the_library_reader_too(tmp_path: Path) -> None:
    path = _write(tmp_path, [("events/seg-0001.bin", b"x")], manifest=_manifest(),
                  decorate={"events/seg-0001.bin": {"external_attr": 0o120777 << 16}})
    fast, full = _code(cap.read_manifest, path), _code(cap.read_capsule, path)
    assert fast is not None and fast == full, (fast, full)


def test_an_encrypted_member_is_refused_by_the_library_reader_too(tmp_path: Path) -> None:
    """A capsule the library would list as readable must not be one the full reader refuses as a vault."""
    path = _write(tmp_path, [("events/seg-0001.bin", b"x")], manifest=_manifest())
    _mark_members_encrypted(path)
    assert _code(cap.read_manifest, path) == _code(cap.read_capsule, path) == "BBX-E406"


def test_a_real_capsule_reads_the_same_manifest_both_ways(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    uuid_hex = _record(data_dir)
    _capsule_bytes(data_dir, uuid_hex)
    path = next((data_dir / "capsules").glob("*.bbx"))
    assert cap.read_manifest(str(path)) == cap.read_capsule(str(path)).manifest


def test_the_library_reader_does_not_decompress_the_segments(tmp_path: Path) -> None:
    """The whole point of the fast path, measured: a big segment costs the full reader and not the library reader."""
    payload = os.urandom(2 << 20)                      # incompressible, so the full reader really inflates and hashes it
    path = _write(tmp_path, [("events/seg-0001.raw", payload)], manifest=_manifest(), name="big.bbx",
                  stored={cap.MANIFEST_NAME})
    cap.read_manifest(str(path))                       # warm the filesystem cache for both
    started = time.perf_counter()
    cap.read_manifest(str(path))
    fast = time.perf_counter() - started
    started = time.perf_counter()
    cap.read_capsule(str(path))
    full = time.perf_counter() - started
    assert fast * 4 < full, f"read_manifest {fast * 1000:.2f} ms vs read_capsule {full * 1000:.2f} ms"


def test_the_byte_limit_applies_to_the_library_reader(tmp_path: Path) -> None:
    path = _write(tmp_path, [("events/seg-0001.raw", b"x" * 4096)], manifest=_manifest())
    with pytest.raises(BlackBoxError) as caught:
        cap.read_manifest(str(path), max_bytes=64)
    assert caught.value.code == "BBX-E401"


def test_a_capsule_that_is_not_a_file_is_refused_rather_than_crashing(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        cap.read_manifest(str(tmp_path / "missing.bbx"))
