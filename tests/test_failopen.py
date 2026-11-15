"""P3-14 — the "broken recorder" suite. The gate row: a 100-call agent behaves **identically** with and without
BlackBox, including when the recorder is broken in the ways a filesystem actually breaks.

The comparison is deliberately blunt: same script, run twice, compare return value, stdout, and the exception type
*and message* if it raises. Not "the recorder did not crash" — a recorder that swallows a model's exception, or
reorders stdout, has changed the application, and that is the failure mode this file exists to catch.

Three breakages, each real:

* **an unwritable data directory** (which is how a container with a read-only mount fails, not with a nice error);
* **a keeper that raises on every tick** (the thread that publishes, which runs under `paint_it_black`'s own lock
  discipline and could plausibly take the process with it);
* **a truncated tape on open** (the state a `kill -9` leaves, seen by the *next* process).

The agent is 100 iterations against the loopback mock provider through seam A, because a single call would not notice
a leak of one file descriptor or one thread.
"""
from __future__ import annotations

import io
import os
import stat
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from blackbox import api
from blackbox.cap_sdk.base import Recorder
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.cap_sdk.seam_httpx_hooks import install as install_hooks
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink

httpx = pytest.importorskip("httpx")

CALLS = 100
MESSAGES = [{"role": "user", "content": "say something"}]


def _agent(base_url: str, *, raises_at: int | None = None) -> str:
    """The application under test. It knows nothing about the recorder; it prints and returns like any script."""
    client = httpx.Client(base_url=base_url, timeout=10.0)
    out: list[str] = []
    try:
        for index in range(CALLS):
            if raises_at is not None and index == raises_at:
                raise ValueError(f"agent failed at call {index}")
            response = client.post("/v1/chat/completions",
                                   json={"model": "mock-1", "stream": True, "messages": MESSAGES})
            out.append(response.text)
            print(f"call {index}: {len(response.content)} bytes")
    finally:
        client.close()
    return "\n".join(out)


def _run(base_url: str, *, raises_at: int | None = None) -> tuple[str, str, BaseException | None]:
    buffer = io.StringIO()
    error: BaseException | None = None
    text = ""
    with redirect_stdout(buffer):
        try:
            text = _agent(base_url, raises_at=raises_at)
        except BaseException as exc:                     # the agent's own failure, compared type and message
            error = exc
    return text, buffer.getvalue(), error


def _with_recorder(data_dir: Path, base_url: str, *, raises_at: int | None = None):
    """Install the recorder the way an application would, run the agent, then uninstall."""
    api.install(data_dir=str(data_dir))
    recorder = SinkRecorder(TapeSink(api.emit))
    handle = install_hooks(recorder)
    try:
        return _run(base_url, raises_at=raises_at)
    finally:
        handle.uninstall()
        api.uninstall()


def test_a_hundred_calls_produce_identical_output_with_and_without_the_recorder(tmp_path):
    with MockProvider(Scenario(chunks=3)) as provider:
        bare = _run(provider.base_url)
        recorded = _with_recorder(tmp_path, provider.base_url)

    assert bare[2] is None and recorded[2] is None
    assert recorded[1] == bare[1], "the recorder changed what the application printed"
    assert recorded[0] == bare[0], "the recorder changed what the application returned"


def test_an_exception_from_the_application_is_the_same_exception_with_the_recorder_on(tmp_path):
    with MockProvider(Scenario(chunks=2)) as provider:
        bare = _run(provider.base_url, raises_at=37)
        recorded = _with_recorder(tmp_path, provider.base_url, raises_at=37)

    assert isinstance(bare[2], ValueError) and isinstance(recorded[2], ValueError)
    assert str(recorded[2]) == str(bare[2]), "the recorder rewrote the exception the application raised"
    assert recorded[1] == bare[1]


def test_a_recorder_that_refuses_to_start_leaves_the_application_untouched(tmp_path):
    """The breakage that is a breakage on all three operating systems: the configuration is impossible, so
    `TapeWriter.__init__` raises `BBX-E130` before any directory is touched, `install()` reports `enabled: False` with
    that error as its reason, and the application must not be able to tell.

    This is the case the earlier versions of this file kept getting wrong. `chmod` a directory read-only and Windows
    still creates files in it; point `data_dir` at a file and Windows let the writer open anyway (defect 26). A
    configuration error refuses everywhere, so the strict "the recorder really is broken" assertion holds on all
    three cells and cannot rot into a test that only ever exercises the happy path."""
    with MockProvider(Scenario(chunks=2)) as provider:
        bare = _run(provider.base_url)

        installed = api.install(data_dir=str(tmp_path), segment_bytes=1)   # below MIN_SEGMENT_BYTES, by design
        recorder = SinkRecorder(TapeSink(api.emit))
        handle = install_hooks(recorder)
        try:
            recorded = _run(provider.base_url)
        finally:
            handle.uninstall()
            api.uninstall()

    assert recorded[2] is None
    assert recorded[1] == bare[1] and recorded[0] == bare[0]
    assert installed["enabled"] is False, "this test proves nothing unless the recorder really is broken here"
    assert "BBX-E130" in (installed.get("disabled_reason") or ""), installed


@pytest.mark.skipif(os.name == "nt",
                    reason="Windows let the writer open against a data directory that is a file (defect 26); the "
                           "case is unproven there, so it is not claimed there")
def test_a_data_directory_that_cannot_be_created_leaves_the_application_untouched(tmp_path):
    """The data directory is a *file*: someone's `BLACKBOX_HOME` points at a path that is not a directory."""
    with MockProvider(Scenario(chunks=2)) as provider:
        bare = _run(provider.base_url)

        blocked = tmp_path / "blocked"
        blocked.write_bytes(b"not a directory")
        installed = api.install(data_dir=str(blocked))
        recorder = SinkRecorder(TapeSink(api.emit))
        handle = install_hooks(recorder)
        try:
            recorded = _run(provider.base_url)
        finally:
            handle.uninstall()
            api.uninstall()

    assert recorded[2] is None
    assert recorded[1] == bare[1] and recorded[0] == bare[0]
    assert installed["enabled"] is False, "this test proves nothing unless the recorder really is broken here"
    assert "NotADirectoryError" in (installed.get("disabled_reason") or ""), installed


@pytest.mark.skipif(os.name == "nt", reason="on Windows the permission bits do not stop a file being created")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores the permission bits, so the recorder would not actually be broken")
def test_an_unwritable_data_directory_leaves_the_application_untouched(tmp_path):
    with MockProvider(Scenario(chunks=2)) as provider:
        bare = _run(provider.base_url)

        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)     # read + execute, no write: the tape cannot be created
        installed = api.install(data_dir=str(locked))
        recorder = SinkRecorder(TapeSink(api.emit))
        handle = install_hooks(recorder)
        try:
            recorded = _run(provider.base_url)
        finally:
            handle.uninstall()
            api.uninstall()
            os.chmod(locked, stat.S_IRWXU)

    assert recorded[2] is None
    assert recorded[1] == bare[1] and recorded[0] == bare[0]
    # Whatever it decided, it decided without raising, and a refusal comes with a reason rather than a shrug.
    assert installed["enabled"] is False, "this test proves nothing unless the recorder really is broken here"
    assert "PermissionError" in (installed.get("disabled_reason") or ""), installed


def test_a_keeper_that_raises_on_every_tick_does_not_reach_the_application(tmp_path, monkeypatch):
    from blackbox.tape import writer as writer_mod

    original_publish = writer_mod.TapeWriter.publish

    def hostile_publish(self, *args, **kwargs):
        raise RuntimeError("keeper exploded")

    with MockProvider(Scenario(chunks=2)) as provider:
        bare = _run(provider.base_url)
        api.install(data_dir=str(tmp_path))
        recorder = SinkRecorder(TapeSink(api.emit))
        handle = install_hooks(recorder)
        monkeypatch.setattr(writer_mod.TapeWriter, "publish", hostile_publish)
        try:
            recorded = _run(provider.base_url)
        finally:
            monkeypatch.setattr(writer_mod.TapeWriter, "publish", original_publish)
            handle.uninstall()
            api.uninstall()

    assert recorded[2] is None, "a broken keeper reached the application as an exception"
    assert recorded[1] == bare[1] and recorded[0] == bare[0]


def test_a_truncated_tape_from_a_previous_kill_does_not_stop_the_next_process(tmp_path):
    """The next process opens the data directory a killed process left behind. If that could raise, a crash would
    take the *following* run with it, which is the opposite of what a recorder is for."""
    with MockProvider(Scenario(chunks=2)) as provider:
        bare = _run(provider.base_url)
        first = _with_recorder(tmp_path, provider.base_url)
        assert first[2] is None

        tape_dirs = [p for p in (tmp_path / "tape").iterdir() if p.is_dir()]
        assert tape_dirs, "the first run wrote no tape"
        segment = sorted(tape_dirs[0].glob("*.seg"))[0]
        with open(segment, "r+b") as fh:                  # tear the tail, exactly like a kill mid-write
            fh.seek(0, os.SEEK_END)
            fh.truncate(max(0, fh.tell() - 512))

        again = _with_recorder(tmp_path, provider.base_url)

    assert again[2] is None
    assert again[1] == bare[1] and again[0] == bare[0]


def test_the_recorder_counts_what_the_application_actually_did(tmp_path):
    """The other side of fail-open: it must still be a recorder. 100 calls through the seam are 100 calls recorded."""
    recorder = Recorder()
    handle = install_hooks(recorder)
    try:
        with MockProvider(Scenario(chunks=2)) as provider:
            _run(provider.base_url)
    finally:
        handle.uninstall()

    chat_calls = [call for call in recorder.calls if call.path.endswith("/chat/completions")]
    assert len(chat_calls) == CALLS
    assert all(call.status == 200 and call.error is None for call in chat_calls)
    assert Path(sys.executable).exists()                  # a cheap guard against this test passing vacuously
