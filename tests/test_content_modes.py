"""Content modes (Appendix A `content`, §15.1): what the tape is allowed to keep of the model's own text.

The plan's default is `hash`, and until this test file existed the sink wrote the body regardless — a documented
default that the code ignored is worse than no default, because a user reads `content=hash` and believes the prompt
is not on the disk.

Three modes, three promises, each checked on a real tape and each checked again through `recovery`, which is the only
reader that has to work when the process died before it could explain itself:

* `full` — the payload is the body, so concatenating chunk frames gives the response back.
* `hash` — the payload is `[bbx-hash sha256=… bytes=N]`: the digest and the count, never the text.
* `off` — the payload is `[bbx-off bytes=N]`: the count alone.

In all three the byte count a chunk stands for is recoverable, because a tape that cannot say how many bytes arrived
cannot answer "was the call cut short?" — the question `recovery` exists for.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from blackbox import api
from blackbox import constants as C
from blackbox.cap_sdk.sink import CONTENT_MODES, SinkRecorder, TapeSink, chunk_bytes
from blackbox.rec import recovery
from blackbox.tape import scanner

ROOT = Path(__file__).resolve().parents[1]
SECRET = b"sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX000"          # a shape the redactor knows; content modes must not leak it


class Recorder0:
    def __init__(self) -> None:
        self.frames: list[tuple[int, bytes, dict]] = []

    def __call__(self, kind: int, payload: bytes, **fields: object) -> None:
        self.frames.append((kind, payload, dict(fields)))

    def chunks(self) -> list[bytes]:
        return [payload for kind, payload, _ in self.frames if kind == C.KIND_MODEL_CHUNK]

    def summary(self) -> dict:
        return json.loads(next(payload for kind, payload, _ in self.frames if kind == C.KIND_MODEL_RESPONSE))


def _run(content: str, body: bytes, *, redactor=None) -> Recorder0:
    emit = Recorder0()
    sink = TapeSink(emit, content=content) if redactor is None else TapeSink(emit, content=content, redactor=redactor)
    recorder = SinkRecorder(sink)
    call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
    for start in range(0, len(body), 40):
        recorder.receive(call, body[start:start + 40])
    recorder.close_call(call, status=200)
    return emit


def test_the_default_is_the_plan_default_and_not_whatever_was_convenient(monkeypatch):
    """Appendix A says `hash`; a process that was told nothing must not be the one keeping every token."""
    monkeypatch.delenv("BLACKBOX_CONTENT", raising=False)
    emit = Recorder0()
    assert TapeSink(emit).content == "hash"


def test_the_content_mode_comes_from_the_environment_and_a_misspelt_one_is_refused(monkeypatch):
    monkeypatch.setenv("BLACKBOX_CONTENT", "off")
    assert TapeSink(Recorder0()).content == "off"
    monkeypatch.setenv("BLACKBOX_CONTENT", "ful")
    with pytest.raises(ValueError, match="unknown content mode"):
        TapeSink(Recorder0())
    assert CONTENT_MODES == ("full", "hash", "off")


def test_full_keeps_the_body_byte_for_byte():
    body = b"data: {\"token\": \"x\"}\n\n" * 7
    emit = _run("full", body)
    assert b"".join(emit.chunks()) == body
    assert emit.summary()["bytes_stored"] == len(body)
    assert sum(chunk_bytes(payload) for payload in emit.chunks()) == len(body)


def test_hash_keeps_a_digest_and_a_count_and_nothing_else():
    body = b"data: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n\n" * 5
    emit = _run("hash", body, redactor=TapeSink(Recorder0()).redactor)
    stored = b"".join(emit.chunks())
    assert body[:16] not in stored, "the body is on the tape in hash mode"
    assert stored.startswith(b"[bbx-hash sha256=")
    assert sum(chunk_bytes(payload) for payload in emit.chunks()) == len(body)
    digests = [payload.split(b" ")[1].split(b"=")[1] for payload in emit.chunks()]
    # Each frame's digest is the digest of *its* bytes, so a capsule can be checked against a body it does not hold.
    lengths = [chunk_bytes(payload) for payload in emit.chunks()]
    recomputed = []
    cursor = 0
    for length in lengths:
        recomputed.append(hashlib.sha256(body[cursor:cursor + length]).hexdigest().encode())
        cursor += length
    assert digests == recomputed


def test_off_keeps_a_count_and_no_content_at_all(monkeypatch):
    body = b"Z" * 500
    emit = _run("off", body)
    stored = b"".join(emit.chunks())
    assert b"Z" not in stored
    assert stored.startswith(b"[bbx-off bytes=")
    assert sum(chunk_bytes(payload) for payload in emit.chunks()) == len(body)


def test_a_secret_in_the_body_does_not_reach_a_hash_tape(tmp_path):
    """Hash mode is not a substitute for redaction, and this is what that means in bytes: the body never arrives."""
    api.install(data_dir=str(tmp_path), publish_ms=0.0)
    try:
        sink = TapeSink(api.emit, content="hash")
        recorder = SinkRecorder(sink)
        call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
        recorder.receive(call, b"key " + SECRET + b" end")
        recorder.close_call(call, status=200)
    finally:
        api.uninstall()

    uuid = next((tmp_path / "tape").iterdir()).name
    whole = b"".join(frame["payload"] for frame in scanner.scan(str(tmp_path), uuid).frames)
    assert SECRET not in whole
    assert b"sk-proj" not in whole
    assert b"[bbx-hash sha256=" in whole


def test_the_request_frame_declares_the_content_mode(tmp_path):
    """A reader has to be able to tell "the body is absent by policy" from "the body is missing"."""
    api.install(data_dir=str(tmp_path), publish_ms=0.0)
    try:
        recorder = SinkRecorder(TapeSink(api.emit, content="off"))
        call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
        recorder.receive(call, b"abc")
        recorder.close_call(call, status=200)
    finally:
        api.uninstall()

    uuid = next((tmp_path / "tape").iterdir()).name
    frames = scanner.scan(str(tmp_path), uuid).frames
    request = json.loads(next(f["payload"] for f in frames if f["kind"] == C.KIND_MODEL_REQUEST))
    summary = json.loads(next(f["payload"] for f in frames if f["kind"] == C.KIND_MODEL_RESPONSE))
    assert request["content"] == "off" and summary["content"] == "off"


def test_recovery_counts_the_bytes_a_kill_lost_in_every_content_mode():
    """The count survives a mode change, because the count is what `recovery` needs and the text is not.

    Fed the frames a kill would leave behind — request, chunks, no response — in each mode, `recovery` must report
    the same partial byte total it would have reported from the raw payloads.
    """
    body = b"data: {\"token\": \"x\"}\n\n" * 9
    for mode in CONTENT_MODES:
        emit = Recorder0()
        # `coalesce_bytes=1`: every read is its own frame, so the frames below are exactly what a kill would leave.
        sink = TapeSink(emit, content=mode, coalesce_bytes=1)
        recorder = SinkRecorder(sink)
        call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
        for start in range(0, len(body), 40):
            recorder.receive(call, body[start:start + 40])
        # no close_call: this is the shape a SIGKILL leaves

        frames = [{"kind": kind, "payload": payload, "wall_ns": 1, **fields}
                  for kind, payload, fields in emit.frames]
        record = recovery.classify_nodes(frames)[0]
        assert record.status == recovery.INTERRUPTED_MID_STREAM, mode
        assert record.partial_stream_bytes == len(body), f"{mode} lost the byte count"
        assert record.chunks == len(emit.chunks())


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="SIGKILL does not exist on Windows")
def test_the_default_content_mode_still_survives_a_kill_with_the_right_count(tmp_path):
    """The row a user actually gets: no `BLACKBOX_CONTENT`, an unmodified SDK script, `kill -9` mid-stream."""
    pytest.importorskip("openai")
    from blackbox.cap_sdk.mock_provider import MockProvider, Scenario

    child = ROOT / "tests" / "_day14_child.py"
    with MockProvider(Scenario(chunks=40, delay_ms=40, chunk_text="token ")) as provider:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "BLACKBOX_HOME": str(tmp_path)}
        env.pop("BLACKBOX_CONTENT", None)
        env.pop("BLACKBOX_DISABLED", None)
        proc = subprocess.Popen([sys.executable, str(child), str(tmp_path), provider.openai_base_url(), "5"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        deadline = time.monotonic() + 30
        while provider.sent_chunks < 5 and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.005)
        if proc.poll() is None:
            proc.kill()
        out, _ = proc.communicate(timeout=30)
        sent_bytes = provider.sent_bytes

    assert "SEAM=" in (out or ""), (out or "")[-600:]
    result = recovery.recover(str(tmp_path))
    node = next(n for n in result.node_statuses if n["status"] == recovery.INTERRUPTED_MID_STREAM)
    assert 0 < node["partial_stream_bytes"] <= sent_bytes
