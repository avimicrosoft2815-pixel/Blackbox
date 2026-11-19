"""Recovery is idempotent on purpose, twice over.

The row says: *10 runs ⇒ 1 capsule, identical sha256; concurrent recoverers ⇒ 1 winner*. Both halves matter to a user
rather than to a reviewer:

* a person who closes the window and reopens the folder must not accumulate capsules — each one is an incident report,
  and a duplicate is a second, subtly different claim about the same incident;
* two processes recovering the same tape at once is not hypothetical — the desktop app's startup sweep, a `blackbox run`
  that ends as the app opens, and a second window all touch the same data directory.

Idempotency here is not "it happens to agree the second time": `Recovery.run` notices the archived state and skips every
step, and the seal reproduces byte-for-byte (same tape, same bytes, same root — `tests/test_capsule_determinism.py`). This file
asserts the observable consequences: one file, one hash, one winner.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.rec import recovery
from blackbox.tape.writer import TapeWriter

NOW = 1_790_000_000_000_000_000


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def data_dir(tmp_path: Path) -> tuple[str, str]:
    """One unfinished tape: five frames and a run-start, no run-end, never sealed."""
    directory = tmp_path / "data"
    writer = TapeWriter(str(directory), segment_bytes=C.MIN_SEGMENT_BYTES).open()
    try:
        writer.append(C.KIND_RUN_START, cbor.encode({"run_id": "r_00000000000000fe"}), priority=0)
        for index in range(5):
            writer.append(C.KIND_MODEL_REQUEST, cbor.encode({"i": index}), priority=0, run_slot=1)
        writer.publish()
    finally:
        writer.close()                        # closed, not sealed: this tape is left for recovery
    return str(directory), writer.writer_uuid.hex()


def test_ten_recoveries_leave_one_capsule_with_one_hash(data_dir):
    directory, uuid = data_dir
    results = [recovery.recover(directory, uuid, now_ns=NOW) for _ in range(10)]
    paths = {result.capsule_path for result in results}
    assert len(paths) == 1, f"ten recoveries produced {len(paths)} capsules: {sorted(paths)}"
    only = Path(paths.pop())
    capsules = sorted((Path(directory) / "capsules").glob("*.bbx"))
    assert capsules == [only], f"the data directory holds {len(capsules)} capsule(s), not one"

    hashes = {_sha256(only) for _ in range(3)}
    assert len(hashes) == 1
    roots = {result.capsule_root for result in results}
    assert roots == {results[0].capsule_root} and None not in roots

    # The first recovery did the work; the other nine did none of it and said so.
    assert results[0].steps_run, "the first recovery ran no step?"
    for later in results[1:]:
        assert later.steps_run == [], f"a repeat recovery ran {later.steps_run}"
        assert later.state == "ARCHIVED"
        assert later.capsule_path == results[0].capsule_path

    # And the capsule still verifies: idempotency that produced something unreadable would be worse than a duplicate.
    from blackbox.cap import format as cap

    parsed = cap.read_capsule(str(only))
    assert parsed.root_ok, parsed.problems


def test_six_concurrent_recoverers_elect_one_winner(data_dir, tmp_path):
    """Six *processes*, because a thread test would be measuring the GIL rather than the claim."""
    directory, uuid = data_dir
    script = tmp_path / "recover_once.py"
    script.write_text(
        "import json, sys\n"
        "from blackbox.rec import recovery\n"
        f"result = recovery.recover({directory!r}, {uuid!r}, now_ns={NOW})\n"
        "print(json.dumps({'path': result.capsule_path, 'root': result.capsule_root, 'state': result.state,\n"
        "                  'run': result.steps_run}))\n",
        encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("BLACKBOX")}
    env["PYTHONPATH"] = str(ROOT / "src")

    def once(_: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=600)

    with ThreadPoolExecutor(max_workers=6) as pool:
        completed = list(pool.map(once, range(6)))

    capsules = sorted((Path(directory) / "capsules").glob("*.bbx"))
    assert len(capsules) == 1, f"concurrent recovery produced {len(capsules)} capsules"

    winners, worked = [], 0
    for result in completed:
        if result.returncode != 0:
            # A loser may be refused rather than silently second — but only with a declared code, never a traceback.
            assert "BBX-E" in result.stderr, f"a concurrent recoverer failed without a code: {result.stderr[-1200:]}"
            continue
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["path"] == str(capsules[0]), payload
        assert payload["root"], payload
        winners.append(payload)
        if payload["run"]:
            worked += 1
    assert winners, "every concurrent recoverer failed"
    assert worked <= 1, f"{worked} recoverers each claimed to have done the work"
    assert len({payload["root"] for payload in winners}) == 1
