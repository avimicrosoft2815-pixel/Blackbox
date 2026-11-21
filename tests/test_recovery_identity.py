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
import errno
import json
import os
import subprocess
import time
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
            # The message is the *exception line*, not the last 1200 characters of a traceback: a CI annotation is cut at
            # 600 characters, and the end of `stderr` is the part that gets dropped (defect 105 needed two CI runs to
            # name a `PermissionError` because this used to end in a caret line).
            lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
            assert "BBX-E" in result.stderr, (
                f"a concurrent recoverer failed without a code: {lines[-1] if lines else '<empty stderr>'}")
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


def test_a_recoverer_that_adopts_after_the_claim_reports_no_work_of_its_own(data_dir, monkeypatch):
    """The adoption path (defect 103) must not read as a second worker (defect 107).

    `DISCOVERED` runs before the lock, so a process that loses the race and adopts the winner's archive has already
    appended one step to `steps_run`. Left there, two processes report having done the work — the duplicate claim again,
    one layer up. The first `_already_archived()` is forced to say "not yet" so that this process runs exactly the
    sequence a loser runs, and the rest of the machine is real.
    """
    directory, uuid = data_dir
    winner = recovery.recover(directory, uuid, now_ns=NOW)
    assert winner.steps_run and winner.capsule_path

    real = recovery.Recovery._already_archived
    calls = {"n": 0}

    def not_yet_once(self):
        calls["n"] += 1
        return False if calls["n"] == 1 else real(self)

    monkeypatch.setattr(recovery.Recovery, "_already_archived", not_yet_once)
    adopted = recovery.recover(directory, uuid, now_ns=NOW)

    assert adopted.state == "ARCHIVED"
    assert adopted.steps_run == [], f"the adopter claims it ran {adopted.steps_run}"
    assert set(adopted.steps_skipped) == set(recovery.STEPS)
    assert adopted.capsule_path == winner.capsule_path and adopted.capsule_root == winner.capsule_root
    assert len(list((Path(directory) / "capsules").glob("*.bbx"))) == 1


def _live_claim() -> bytes:
    """A lock document that names *this* process, so the staleness check has to call it live."""
    plat = recovery._plat
    document = {"pid": os.getpid(), "start_ticks": plat.proc_start_ticks(os.getpid()),
                "boot_id": plat.boot_id(), "at_ns": time.time_ns()}
    return json.dumps(document, sort_keys=True).encode() + b"\n"



def test_a_lock_is_never_observable_without_its_content(data_dir, monkeypatch, tmp_path):
    """The claim is published whole, so a peer cannot read an empty lock and conclude it is stale.

    The first version created the lock with `O_EXCL` and wrote the content afterwards. A reader arriving in that window
    saw a file with no pid in it, the staleness check answered "nobody live", and it unlinked a live claim — two
    recoverers both owning one run. `44820329776` failed exactly that way on `py3.13 / ubuntu-latest`.

    This test watches the publication itself: what `os.link` was handed must already be the full document, and what the
    lock path then holds must be the same bytes.
    """
    directory, uuid = data_dir
    recorder = recovery.Recovery(directory, uuid, now_ns=NOW)
    lock_path = str(tmp_path / "run" / "recovery.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    content = _live_claim()

    published: list[int] = []
    real_link = os.link

    def watching_link(src, dst, *args, **kwargs):
        published.append(os.path.getsize(src))       # the bytes at publication time, before the name exists
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", watching_link)
    assert recorder._take_lock(lock_path, content) is True
    assert published == [len(content)], f"the lock was published with {published} bytes, not {len(content)}"
    assert Path(lock_path).read_bytes() == content
    # A second claimant now reads a complete document and is refused rather than stealing it.
    assert recorder._take_lock(lock_path, b'{"pid": 9}\n') is False
    assert Path(lock_path).read_bytes() == content
    # No temporary file is left beside it.
    assert [p.name for p in Path(lock_path).parent.iterdir()] == ["recovery.lock"]


def test_a_filesystem_without_hard_links_still_claims(data_dir, monkeypatch, tmp_path):
    """The fallback is real: when `os.link` is refused, the in-place create still elects one winner."""
    directory, uuid = data_dir
    recorder = recovery.Recovery(directory, uuid, now_ns=NOW)
    lock_path = str(tmp_path / "run2" / "recovery.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)

    def no_links(*_args, **_kwargs):
        raise OSError(errno.EPERM, "hard links are not supported here")

    monkeypatch.setattr(os, "link", no_links)
    content = _live_claim()
    assert recorder._take_lock(lock_path, content) is True
    assert Path(lock_path).read_bytes() == content
    assert recorder._take_lock(lock_path, b'{"pid": 9}\n') is False
