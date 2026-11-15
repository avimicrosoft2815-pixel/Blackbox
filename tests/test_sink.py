"""P3-06 — the sink. The Done-when is two numbers: a 2 000-token stream becomes **≤ 40 chunk events**, and the bytes
forwarded are identical (sha256 compare).

Both halves are tested separately, because they pull in opposite directions. Coalescing fewer frames is easy if you
drop bytes; keeping bytes exact is easy if you write one frame per read. The tests here hold both: the payloads of
the `MODEL_CHUNK` frames concatenate back to the body, and the count of those frames stays small on a long stream.

The frames are also checked where they matter — on a real tape, read back by the same `scanner` a recovery uses, so
"the sink wrote it" means "a reader finds it", not "the sink's own counter went up".
"""
from __future__ import annotations

import hashlib
import json

import pytest

from blackbox import constants as C
from blackbox.cap_sdk.base import Recorder
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.cap_sdk.seam_httpx_transport import install as install_transport
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink
from blackbox.redact import Redactor
from blackbox.tape import scanner, segments
from blackbox.tape.writer import TapeWriter

httpx = pytest.importorskip("httpx")


class Recorder0:
    """Collects what the sink emitted, so the coalescing logic can be tested without a filesystem."""

    def __init__(self) -> None:
        self.frames: list[tuple[int, bytes, dict]] = []

    def __call__(self, kind: int, payload: bytes = b"", **fields) -> int:
        self.frames.append((kind, payload, fields))
        return len(self.frames)

    def kinds(self) -> list[str]:
        return [C.KIND_NAMES[kind] for kind, _, _ in self.frames]


def _feed(sink: TapeSink, chunk: str, count: int) -> None:
    call = sink_recorder(sink).open_call("127.0.0.1", "/v1/chat/completions", b"{}")
    for _ in range(count):
        sink.on_chunk(call, chunk.encode())
    sink.on_close(call)


def sink_recorder(sink: TapeSink) -> SinkRecorder:
    return SinkRecorder(sink)


def test_two_thousand_chunks_become_few_frames_and_exact_bytes():
    emit = Recorder0()
    sink = TapeSink(emit, content="full")
    recorder = SinkRecorder(sink)
    call = recorder.open_call("127.0.0.1", "/v1/chat/completions", b"{}")
    piece = b"data: {\"token\": \"x\"}\n\n"
    for _ in range(2000):
        recorder.receive(call, piece)
    recorder.close_call(call, status=200, total_ms=12.0)

    chunks = [(kind, payload) for kind, payload, _ in emit.frames if kind == C.KIND_MODEL_CHUNK]
    assert 0 < len(chunks) <= 40, f"{len(chunks)} chunk frames for 2 000 chunks; the plan says <= 40"
    assert b"".join(payload for _, payload in chunks) == piece * 2000
    assert sum(len(payload) for _, payload in chunks) == len(piece) * 2000 == call.bytes_received

    summary = json.loads(next(payload for kind, payload, _ in emit.frames if kind == C.KIND_MODEL_RESPONSE))
    assert summary["chunks_raw"] == 2000
    assert summary["chunks_events"] == 2000, "the recorder counts raw chunks; the sink counts frames"
    assert summary["bytes_received"] == len(piece) * 2000


def test_the_frame_order_is_request_chunks_response():
    emit = Recorder0()
    recorder = SinkRecorder(TapeSink(emit, content="full"))
    call = recorder.open_call("127.0.0.1", "/v1/messages", b"{}")
    recorder.receive(call, b"one")
    recorder.receive(call, b"two")
    recorder.close_call(call, status=200)
    assert emit.kinds() == ["MODEL_REQUEST", "MODEL_CHUNK", "MODEL_RESPONSE"]
    request = json.loads(emit.frames[0][1])
    assert request["provider"] == "127.0.0.1" and request["coalesce_bytes"] == 8 * 1024


def test_a_call_that_errors_ends_with_model_error_not_a_response():
    emit = Recorder0()
    recorder = SinkRecorder(TapeSink(emit, content="full"))
    call = recorder.open_call("127.0.0.1", "/v1/chat/completions", b"{}")
    recorder.receive(call, b"partial")
    recorder.close_call(call, status=200, error="ReadError: socket died")
    assert emit.kinds() == ["MODEL_REQUEST", "MODEL_CHUNK", "MODEL_ERROR"]
    assert json.loads(emit.frames[-1][1])["error"] == "ReadError: socket died"


def test_the_time_bound_flushes_even_when_the_byte_bound_does_not():
    """100 ms is the other half of the coalescing rule: a slow stream must not sit in a buffer waiting for 8 KiB.

    Redaction is off *here on purpose*: this test is about the timer, and three bytes of `abc` are, to the default
    policy, one unfinished token that is deliberately held (see `test_redaction.py`, which tests that separately).
    Making the policy part of this test would test neither rule.
    """
    now = [0]
    emit = Recorder0()
    sink = TapeSink(emit, coalesce_ms=100.0, clock=lambda: now[0], redactor=Redactor("off"), content="full")
    recorder = SinkRecorder(sink)
    call = recorder.open_call("127.0.0.1", "/x", b"{}")
    recorder.receive(call, b"a")
    now[0] += 50_000_000
    recorder.receive(call, b"b")
    assert emit.kinds() == ["MODEL_REQUEST"], "50 ms is not 100 ms"
    now[0] += 60_000_000
    recorder.receive(call, b"c")
    assert emit.kinds() == ["MODEL_REQUEST", "MODEL_CHUNK"]
    recorder.close_call(call, status=200)
    assert emit.kinds()[-1] == "MODEL_RESPONSE"


def test_a_sink_that_raises_never_reaches_the_application_or_stops_the_bytes():
    class Exploding(Recorder):
        def open_call(self, provider, path, request):        # type: ignore[override]
            raise RuntimeError("sink exploded")

    recorder = SinkRecorder(Exploding())
    call = recorder.open_call("127.0.0.1", "/x", b"{}")
    recorder.receive(call, b"still counted")
    recorder.close_call(call, status=200)
    assert recorder.sink_errors == 3, "every hook is attempted and every failure is counted"
    assert call.bytes_received == len(b"still counted")


def test_frames_land_on_a_real_tape_and_a_scanner_finds_them(tmp_path):
    writer = TapeWriter(str(tmp_path), segment_bytes=256 * 1024).open()
    uuid = writer.writer_uuid.hex()
    sink = TapeSink(writer.emit, content="full")
    recorder = SinkRecorder(sink)
    try:
        with MockProvider(Scenario(chunks=6, delay_ms=2)) as provider:
            client = httpx.Client(base_url=provider.base_url, timeout=10.0)
            handle = install_transport(recorder, client=client)
            try:
                with client.stream("POST", "/v1/chat/completions",
                                   json={"model": "mock-1", "stream": True}) as response:
                    body = b"".join(response.iter_bytes())
            finally:
                handle.uninstall()
                client.close()
        writer.publish()
    finally:
        writer.close()

    result = scanner.scan(str(tmp_path), uuid)
    kinds = [C.KIND_NAMES[f["kind"]] for f in result.frames]
    assert kinds.count("MODEL_REQUEST") == 1
    assert kinds.count("MODEL_RESPONSE") == 1
    assert kinds.count("MODEL_CHUNK") >= 1
    streamed = b"".join(f["payload"] for f in result.frames if f["kind"] == C.KIND_MODEL_CHUNK)
    assert hashlib.sha256(streamed).hexdigest() == hashlib.sha256(body).hexdigest()
    summary = json.loads(next(f["payload"] for f in result.frames if f["kind"] == C.KIND_MODEL_RESPONSE))
    assert summary["bytes_received"] == len(body)
    assert summary["status"] == 200 and summary["error"] is None
    assert len(segments.list_segments(str(tmp_path / "tape" / uuid))) >= 1


def test_an_unfinished_call_is_left_as_chunks_with_no_response(tmp_path):
    """This is the shape a `kill -9` leaves behind, and recovery's classification depends on it exactly: the last
    kind the node produced is `MODEL_CHUNK`, so the node reads `INTERRUPTED_MID_STREAM` with a chunk and byte count.
    """
    writer = TapeWriter(str(tmp_path), segment_bytes=256 * 1024).open()
    uuid = writer.writer_uuid.hex()
    sink = TapeSink(writer.emit, content="full")
    recorder = SinkRecorder(sink)
    try:
        call = recorder.open_call("127.0.0.1", "/v1/chat/completions", b"{}")
        for _ in range(300):
            recorder.receive(call, b"x" * 32)               # 9 600 B: one flush, no close
        writer.publish()
    finally:
        writer.close()

    result = scanner.scan(str(tmp_path), uuid)
    kinds = [C.KIND_NAMES[f["kind"]] for f in result.frames]
    model_kinds = [kind for kind in kinds if kind.startswith("MODEL_")]
    # SEG_END/CHAIN frames close the segment and come after it; the *model* sequence is what recovery walks.
    assert model_kinds[-1] == "MODEL_CHUNK"
    assert "MODEL_RESPONSE" not in kinds
    written = sum(len(f["payload"]) for f in result.frames if f["kind"] == C.KIND_MODEL_CHUNK)
    # 9 600 B arrived and the byte bound flushed 8 KiB of it; the last 1 408 B were still in the group when the writer
    # closed, which is exactly what a kill costs. The invariant is the bound, not the total: at most one group is
    # ever at risk.
    assert 8192 <= written <= 9600, written
    assert 9600 - written < 8 * 1024


def test_the_byte_bound_is_respected():
    """16 KiB of chunks against a 8 KiB bound is two frames; against a 64 KiB bound it is one — flushed by close,
    not by the byte bound. Both end with the same bytes, which is the invariant that matters.

    `coalesce_ms` is pushed far out of reach on purpose. The sink has *two* triggers — the byte bound and a 100 ms
    window — and this test is about the first one; leaving the clock in meant that a loaded macOS runner could take
    more than 100 ms between `open_call` and the fourth `receive`, legitimately flush early, and fail the assertion
    about the byte bound (defect 38). A test of a byte bound must not be able to be answered by a clock.
    """
    emit = Recorder0()
    recorder = SinkRecorder(TapeSink(emit, coalesce_bytes=8 * 1024, coalesce_ms=60_000.0, content="full"))
    call = recorder.open_call("127.0.0.1", "/x", b"{}")
    for _ in range(4):
        recorder.receive(call, b"y" * 4096)
    before_close = [payload for kind, payload, _ in emit.frames if kind == C.KIND_MODEL_CHUNK]
    assert 1 <= len(before_close) <= 2
    recorder.close_call(call, status=200)

    emit2 = Recorder0()
    recorder2 = SinkRecorder(TapeSink(emit2, coalesce_bytes=64 * 1024, coalesce_ms=60_000.0, content="full"))
    call2 = recorder2.open_call("127.0.0.1", "/x", b"{}")
    for _ in range(4):
        recorder2.receive(call2, b"y" * 4096)
    assert not [1 for kind, _, _ in emit2.frames if kind == C.KIND_MODEL_CHUNK], "16 KiB is under a 64 KiB bound"
    recorder2.close_call(call2, status=200)
    bounded = [payload for kind, payload, _ in emit2.frames if kind == C.KIND_MODEL_CHUNK]
    assert len(bounded) == 1
    assert b"".join(bounded) == b"y" * 4096 * 4
