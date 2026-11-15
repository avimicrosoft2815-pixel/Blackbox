"""P3-01 — `api.install/uninstall/shutdown`: refcounted, idempotent, thread-safe, fail-open.

The Done-when for this row is two sentences: *double install ⇒ one writer* and *uninstall during a call doesn't
break it*. Both are about a mistake that is invisible until it matters — a second `install()` opening a second tape
means two `RUN_START` frames and two writer UUIDs for one process, and a close in the middle of an append means a
torn last frame in the file a user is about to be told is intact.

So the tests are about counts and races, not about happy paths: how many writers exist, how many runs started, and
what the tape says after a close that raced an append.
"""
from __future__ import annotations

import os
import threading

import pytest

from blackbox import api
from blackbox import constants as C
from blackbox.tape import scanner


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("BLACKBOX_DISABLED", raising=False)
    monkeypatch.setenv("BLACKBOX_HOME", str(tmp_path))
    api.shutdown()
    yield
    api.shutdown()


def _kinds(data_dir):
    status = api._STATE.status()
    writer_uuid = status["writer_uuid"] or os.listdir(os.path.join(str(data_dir), "tape"))[0]
    res = scanner.scan(str(data_dir), writer_uuid)
    return [f["kind"] for f in res.frames], res


def test_double_install_makes_one_writer_and_one_run_start(tmp_path):
    base_runs = api._STATE.status()["runs_started"]
    first = api.install(data_dir=str(tmp_path))
    second = api.install(data_dir=str(tmp_path))
    assert first["enabled"] and second["enabled"]
    assert second["refs"] == 2
    assert second["writer_uuid"] == first["writer_uuid"], "a second install opened a second tape"
    assert second["runs_started"] == base_runs + 1, "RUN_START was written twice for one process"
    api.uninstall()
    assert api.enabled(), "the first uninstall of two must not close the tape"
    api.uninstall()
    assert not api.enabled()
    kinds, res = _kinds(tmp_path)
    assert kinds.count(C.KIND_RUN_START) == 1
    assert kinds.count(C.KIND_RUN_END) == 1, "the tape must be sealed exactly once"
    assert not res.gaps, res.gaps


def test_uninstall_during_a_call_does_not_cut_the_call(tmp_path):
    api.install(data_dir=str(tmp_path))
    inside = threading.Event()
    release = threading.Event()
    written: list[int] = []

    def slow_call() -> None:
        with api.recording() as writer:
            inside.set()
            release.wait(5)
            written.append(writer.emit(C.KIND_NODE_CLOSE, b'{"name":"slow"}'))

    thread = threading.Thread(target=slow_call)
    thread.start()
    assert inside.wait(5)
    api.uninstall()                                   # refs hit zero while the call is mid-append
    assert api.enabled(), "the tape was closed under an in-flight call"
    release.set()
    thread.join(10)
    assert written and written[0] > 0
    api.shutdown()
    kinds, res = _kinds(tmp_path)
    assert kinds.count(C.KIND_NODE_CLOSE) >= 1, "the frame the call was writing never landed"
    assert not res.gaps, res.gaps


def test_install_from_many_threads_makes_one_writer(tmp_path):
    barrier = threading.Barrier(16)
    results: list[dict] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(10)
        got = api.install(data_dir=str(tmp_path))
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    uuids = {r["writer_uuid"] for r in results}
    assert len(uuids) == 1, f"{len(uuids)} writers for 16 installs"
    assert api._STATE.status()["refs"] == 16
    for _ in range(16):
        api.uninstall()
    assert not api.enabled()
    kinds, _ = _kinds(tmp_path)
    assert kinds.count(C.KIND_RUN_START) == 1


def test_disabled_process_creates_nothing_and_emit_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("BLACKBOX_DISABLED", "1")
    status = api.install(data_dir=str(tmp_path))
    assert status["enabled"] is False
    assert "BLACKBOX_DISABLED" in (status["disabled_reason"] or "")
    assert api.enabled() is False
    assert api.emit(C.KIND_NODE_CLOSE, b"{}") == 0
    assert not os.path.exists(os.path.join(str(tmp_path), "tape")), "a disabled process wrote a tape"


def test_a_tape_that_cannot_be_opened_leaves_the_app_running(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    status = api.install(data_dir=str(blocked / "tape"))       # mkdir under a regular file must fail
    assert status["enabled"] is False
    assert status["disabled_reason"], "the failure must be reported, not swallowed silently"
    assert api.emit(C.KIND_NODE_CLOSE, b"{}") == 0               # and still never raises


def test_shutdown_is_idempotent_and_install_works_after_it(tmp_path):
    base = api._STATE.status()["closed"]
    api.install(data_dir=str(tmp_path))
    api.shutdown()
    api.shutdown()                                    # the second call must be a no-op, not a second close
    assert not api.enabled()
    assert api._STATE.status()["closed"] == base + 1
    again = api.install(data_dir=str(tmp_path))
    assert again["enabled"] is True
    assert again["closed"] == base + 1, "a fresh install does not close anything"
    api.shutdown()
    assert api._STATE.status()["closed"] == base + 2
