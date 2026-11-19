"""P4-01 — the same inputs are the same capsule, byte for byte.

The claim a reproducibility story rests on is not "two seals of one run agree" — `test_recovery.py` already checks
that, because the second seal skips `SEALED` and copies the first one's root. The claim is stronger and it is the one
this file tests: **take the recorded tape away, put it somewhere else, seal it there, and get the same bytes.** That
is what makes a capsule something a second party can re-derive from the evidence rather than something a first party
produced once and vouched for.

Three things can break it, and each has its own test below:

* a clock, a pid or a path in the manifest or in the archive's own headers (`created_ns`, `ZipInfo.date_time`);
* an order that depends on `os.listdir`, a `dict` walked by insertion, or a set;
* a writer that uses up a resource the second run does not have — `<name>.partial` names carry the pid, so a crash in
  one process must not leave bytes in the next one's capsule.

The capsule under test is a **real** one: a tape written by `TapeWriter`, sealed by `recovery.recover`, not a hand-built
zip. `tests/test_capsule.py` covers the container itself; this file covers the thing users actually get.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.rec import recovery
from blackbox.tape.writer import TapeWriter


def _record(data_dir: Path, *, events: int = 20) -> str:
    """Write one run to `data_dir` and return the tape's writer uuid.

    The frames carry a fixed `wall_ns` so that a difference between two capsules is a difference the *writer* made, not
    one the recording did. `TapeWriter` gets zero batch latency for the same reason the other recovery tests do it:
    batching by time makes the segment boundaries depend on how fast the machine is.
    """
    w = TapeWriter(str(data_dir), segment_bytes=C.MIN_SEGMENT_BYTES)
    w.batch_bytes = 0
    w.batch_ms = 0.0
    assert w.open() is w
    uuid_hex = w.writer_uuid.hex()
    try:
        w.append(C.KIND_RUN_START, b'{"run":"determinism"}', priority=0)
        w.append(C.KIND_NODE_OPEN, b'{"name":"step"}', node_slot=1, priority=0, run_slot=1)
        for index in range(events):
            w.append(C.KIND_MODEL_REQUEST, b'{"model":"m","n":%d}' % index, node_slot=1, priority=0, run_slot=1,
                     wall_ns=1_700_000_000_000_000_000 + index * 1_000_000)
            w.publish()
        w.append(C.KIND_NODE_CLOSE, b'{"ok":true}', node_slot=1, priority=0, run_slot=1)
        w.append(C.KIND_RUN_END, b"{}", priority=0)
        w.publish()
    finally:
        w.close()
    return uuid_hex


def _capsule_bytes(data_dir: Path, uuid_hex: str) -> tuple[bytes, str]:
    result = recovery.recover(str(data_dir), uuid_hex)
    path = Path(result.capsule_path or "")
    assert path.exists(), result.to_dict()
    return path.read_bytes(), str(result.capsule_root)


#: One writer uuid for both processes in the pid test: the uuid *is* allowed inside a capsule (it names the tape), so it
#: has to be held constant for the comparison to be about the pid.
_FIXED_UUID = bytes(range(16))

def _record_in_a_process(data_dir: Path) -> tuple[int, str]:
    """Record the fixed frames in a **separate process**, so the pid differs by construction. Returns (pid, data dir)."""
    import subprocess
    sys.path.insert(0, str(ROOT / "tests"))
    script = ROOT / "tests" / "_record_fixed.py"
    assert script.exists(), f"the helper script is missing: {script}"
    finished = subprocess.run([sys.executable, str(script), str(data_dir)], capture_output=True, text=True, timeout=300,
                              env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert finished.returncode == 0, finished.stderr[-1500:]
    return int(finished.stdout.strip().splitlines()[-1]), str(data_dir)


# ── the claim ────────────────────────────────────────────────────────────────
def test_the_same_tape_seals_to_the_same_bytes_wherever_it_is_sealed(tmp_path: Path) -> None:
    """Move the evidence, seal it again, get the same file — the whole of P4-01 in one assertion."""
    first_dir = tmp_path / "machine-a" / "data"
    uuid_hex = _record(first_dir)
    # Copied, not re-recorded: a fresh recording would have a new writer uuid, and a capsule is allowed to name the
    # tape it came from. What must not move is everything derived from the *frames*.
    second_dir = tmp_path / "machine-b" / "elsewhere" / "data"
    shutil.copytree(first_dir, second_dir)

    one, root_one = _capsule_bytes(first_dir, uuid_hex)
    two, root_two = _capsule_bytes(second_dir, uuid_hex)

    assert one == two, (
        f"the same tape sealed to different capsules: {len(one)} B vs {len(two)} B, "
        f"sha256 {hashlib.sha256(one).hexdigest()[:16]} vs {hashlib.sha256(two).hexdigest()[:16]}")
    assert root_one == root_two


def test_a_different_tape_is_a_different_capsule(tmp_path: Path) -> None:
    """The control. A determinism test that passes on constants would pass on this one too."""
    one_dir = tmp_path / "one" / "data"
    two_dir = tmp_path / "two" / "data"
    uuid_one = _record(one_dir, events=20)
    uuid_two = _record(two_dir, events=21)          # one more model frame
    one, _ = _capsule_bytes(one_dir, uuid_one)
    two, _ = _capsule_bytes(two_dir, uuid_two)
    assert one != two


# ── what must not be in the bytes ───────────────────────────────────────────
def test_no_clock_no_pid_and_no_path_reaches_the_capsule(tmp_path: Path) -> None:
    """What a capsule may and may not be built from, asserted where each one can actually be proved.

    Two earlier versions of this test used short numbers as needles and both failed on CI — pid 1280 (`00 00 05 00`,
    present in the archive's own zero bytes) and pid 2596 (`b"2596"`, present in deflated data). A needle that short is a
    test of the archive format, not of a leak (defect 100). The honest version proves each property in the place it lives:

    * **clock** — the manifest's `created_ns` is `0`. That is the field the clock would have gone into, and it is what
      makes two seals of one tape identical (`test_the_same_tape_seals_to_the_same_bytes_wherever_it_is_sealed`).
    * **path** — the data directory contains a 40-character random token, and that token appears nowhere in the bytes; and
      no absolute-path marker appears either.
    * **pid** — there is no field for it. The manifest's keys are exactly the documented set (nothing named `pid`, no
      `host`, no `path`), and every entry name is exactly the documented set — a pid could only reach a capsule through a
      name, because the archive's names are `<name>.partial`-style temp files' destination, and those never travel.
    """
    import uuid as _uuid

    token = "bbxprobe" + _uuid.uuid4().hex          # distinctive: this cannot be a coincidence in any archive
    data_dir = tmp_path / token / "data"
    uuid_hex = _record(data_dir)
    blob, _ = _capsule_bytes(data_dir, uuid_hex)
    capsule = cap.read_capsule(str(Path(recovery.recover(str(data_dir), uuid_hex).capsule_path or "")))
    assert capsule.root_ok, capsule.problems

    assert capsule.manifest["created_ns"] == 0, capsule.manifest["created_ns"]
    assert token.encode() not in blob, "the data directory's path reached the capsule"
    for needle in (b"/tmp/", b"\\\\?\\\\", str(Path.home()).encode()):
        assert needle not in blob, f"an absolute path ({needle!r}) reached the capsule"

    # The manifest's key set is the documented one, and a key that could carry an identity would be a finding.
    keys = set(capsule.manifest)
    assert not {key for key in keys if "pid" in key.lower() or "host" in key.lower() or "username" in key.lower()}, \
        sorted(keys)
    assert keys == {
        "capsule_id", "capture", "capture_seam", "capture_vantage", "chain_root", "counts", "created_ns",
        "derived_parts", "format", "format_version", "gaps", "integrity", "links_summary", "min_reader", "redaction",
        "run", "signature", "tool",
    }, f"the manifest carries keys this test does not know about: {sorted(keys)}"
    assert sorted(capsule.entries) == [
        "INTEGRITY.sha256", "chain.json", "events/events.jsonl", "events/seg-000001.raw", "graph.json", "links.json",
        "manifest.json", "otlp/spans.json", "otlp/spans.pb",
    ], sorted(capsule.entries)
    pid = str(os.getpid())
    names = sorted(capsule.entries)
    assert all(pid not in name for name in names), f"an entry name carries the pid: {names}"
    assert all(not name.endswith(".partial") for name in names), names


def test_every_entry_carries_the_same_fixed_metadata(tmp_path: Path) -> None:
    """An archive header is a place a clock leaks in without anyone writing a clock into the manifest."""
    data_dir = tmp_path / "data"
    uuid_hex = _record(data_dir)
    path = Path(recovery.recover(str(data_dir), uuid_hex).capsule_path or "")
    infos = cap.zinfo_unpack(str(path))
    assert infos, "no entries"
    for info in infos:
        assert info["date_time"] == cap.FIXED_DATE, (info["name"], info["date_time"])
        assert info["external_attr"] == cap.FIXED_MODE, (info["name"], oct(info["external_attr"]))
    names = [i["name"] for i in infos]
    # Sorted with the manifest last, so a reader streaming the archive has the data before the description of it.
    assert names[-1] == cap.MANIFEST_NAME, names
    assert names[:-1] == sorted(names[:-1]), names


def test_resealing_leaves_the_file_and_its_timestamp_alone(tmp_path: Path) -> None:
    """Idempotent is not just "same root": the second seal must not rewrite the bytes under the user's open viewer."""
    data_dir = tmp_path / "data"
    uuid_hex = _record(data_dir)
    _, _ = _capsule_bytes(data_dir, uuid_hex)
    path = Path(recovery.recover(str(data_dir), uuid_hex).capsule_path or "")
    before = path.read_bytes()
    stat_before = path.stat()
    # A third recovery, straight after: SEALED is skipped, and nothing touches the file.
    recovery.recover(str(data_dir), uuid_hex)
    after = path.read_bytes()
    assert after == before
    assert path.stat().st_mtime_ns == stat_before.st_mtime_ns
