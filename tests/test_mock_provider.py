"""P3-13 — the mock provider itself. Nothing below it can be trusted unless this is real HTTP.

Every capture test downstream depends on one property of this fixture: the bytes arrive over a socket, in chunks,
over time, in the shape an SDK expects. A test double that returns a list would prove nothing about a seam that
claims to tee a stream. So these tests use `http.client` (stdlib) rather than httpx, which also proves the mock is
not accidentally coupled to the library under test.

The scenario knobs are the interesting part: they are the failure shapes a recorder has to survive — a truncated
stream, an error event mid-stream, a 429, a slow first byte.
"""
from __future__ import annotations

import http.client
import json

import pytest

from blackbox.cap_sdk.mock_provider import MockProvider, Scenario


def _post(provider: MockProvider, path: str, payload: dict) -> tuple[int, bytes, list[bytes]]:
    conn = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=10)
    body = json.dumps(payload).encode()
    conn.request("POST", path, body=body, headers={"content-type": "application/json"})
    response = conn.getresponse()
    chunks = []
    while True:
        piece = response.read(64)
        if not piece:
            break
        chunks.append(piece)
    status = response.status
    conn.close()
    return status, b"".join(chunks), chunks


def test_openai_style_stream_arrives_in_several_chunks():
    with MockProvider(Scenario(chunks=4)) as provider:
        status, body, chunks = _post(provider, "/v1/chat/completions", {"model": "mock-1", "stream": True, "messages": []})
    assert status == 200
    assert len(chunks) >= 2, "the body arrived in one piece; the chunk boundary is the thing under test"
    assert body.count(b"data: ") == 5, "four content chunks plus [DONE]"
    assert body.rstrip().endswith(b"data: [DONE]")


def test_anthropic_style_stream_uses_event_lines():
    with MockProvider(Scenario(chunks=3)) as provider:
        status, body, _ = _post(provider, "/v1/messages", {"model": "mock-1", "stream": True})
    assert status == 200
    assert b"event: content_block_delta" in body
    assert b"event: message_stop" in body


def test_the_request_reaches_the_server_and_is_recorded():
    with MockProvider() as provider:
        _post(provider, "/v1/chat/completions", {"model": "mock-1", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert provider.scenario.requests == 1
    assert provider.received[0]["body"]["messages"][0]["content"] == "hi"


def test_a_non_200_status_is_returned_with_a_json_error():
    with MockProvider(Scenario(status=429)) as provider:
        status, body, _ = _post(provider, "/v1/chat/completions", {"model": "mock-1", "stream": True})
    assert status == 429
    assert json.loads(body)["error"]["type"] == "rate_limit_error"


def test_truncation_closes_the_stream_early_without_the_done_marker():
    """The client must see a *short* stream, not an error: that is what a killed connection looks like."""
    with MockProvider(Scenario(chunks=10, truncate_after=3)) as provider:
        status, body, _ = _post(provider, "/v1/chat/completions", {"model": "mock-1", "stream": True})
    assert status == 200
    assert b"[DONE]" not in body
    assert body.count(b"data: ") == 3


def test_an_error_event_mid_stream_is_followed_by_the_done_marker():
    with MockProvider(Scenario(chunks=6, error_after=2)) as provider:
        status, body, _ = _post(provider, "/v1/chat/completions", {"model": "mock-1", "stream": True})
    assert status == 200
    assert b"mock overloaded" in body
    assert body.rstrip().endswith(b"data: [DONE]")


def test_inter_chunk_delay_is_real_time():
    """The knob has to produce a real gap, or a kill test would have no window to land in.

    The assertion is deliberately *not* "elapsed >= nominal": the first version of this test asserted 2 gaps of
    40 ms took >= 80 ms and failed on a Windows cell at 79 ms — a scheduler shaved a millisecond and the test
    called that a bug in the fixture. What matters is that the delay dominates the call, so this measures the same
    request with and without it and asks for most of the nominal gap, not all of it.
    """
    import time

    def elapsed_ms(delay_ms: float) -> float:
        with MockProvider(Scenario(chunks=4, delay_ms=delay_ms)) as provider:
            started = time.monotonic()
            _post(provider, "/v1/chat/completions", {"model": "mock-1", "stream": True})
            return (time.monotonic() - started) * 1000

    quick = elapsed_ms(0.0)
    slow = elapsed_ms(50.0)
    nominal = 3 * 50.0                                   # three gaps between four chunks
    assert slow - quick >= nominal * 0.6, f"3 x 50 ms of delay only added {slow - quick:.0f} ms"
    assert slow >= nominal * 0.6


@pytest.mark.parametrize("path,marker", [("/v1/chat/completions", b"data: "), ("/v1/messages", b"event: ")])
def test_both_dialects_mark_up_their_streams_differently(path, marker):
    with MockProvider(Scenario(chunks=2)) as provider:
        _, body, _ = _post(provider, path, {"model": "mock-1", "stream": True})
    assert marker in body


def test_a_non_streaming_request_gets_json_and_a_streaming_one_gets_sse():
    """A real provider distinguishes on `stream` in the body, so the mock must too: an SDK parses JSON with its own
    model classes and would fail inside *itself* if handed SSE, which is the one failure a fixture must never fake."""
    with MockProvider(Scenario(chunks=3)) as provider:
        _, plain, _ = _post(provider, "/v1/chat/completions", {"model": "mock-1"})
        _, streamed, _ = _post(provider, "/v1/chat/completions", {"model": "mock-1", "stream": True})
    parsed = json.loads(plain)
    assert parsed["object"] == "chat.completion"
    assert parsed["choices"][0]["finish_reason"] == "stop"
    assert parsed["usage"]["total_tokens"] == 10
    assert b"data: " not in plain
    assert b"data: " in streamed


def test_anthropic_style_message_body_and_embeddings():
    with MockProvider(Scenario(chunks=2)) as provider:
        _, message, _ = _post(provider, "/v1/messages", {"model": "mock-1", "max_tokens": 8})
        _, vectors, _ = _post(provider, "/v1/embeddings", {"model": "mock-1", "input": "x"})
    parsed = json.loads(message)
    assert parsed["type"] == "message" and parsed["content"][0]["type"] == "text"
    assert parsed["usage"] == {"input_tokens": 7, "output_tokens": 2}
    assert json.loads(vectors)["data"][0]["embedding"] == [0.1, 0.2, 0.3]
