"""P3-05b — seam B (the transport tee). The claims here are byte claims, so every one is checked against an oracle.

The oracle is the *unmodified* client: the same request is made twice, once with the seam and once without, and the
two bodies are compared by digest. Anything the seam changes about the bytes on the wire is a bug that would corrupt
every replay built on top of it, so this file is deliberately unfriendly to the seam.

Chunk *boundaries* are separated from chunk *content* on purpose. The mock provider closes the connection to end the
body (there is no `content-length`), which is exactly how a real SSE stream ends, and it means the transport controls
how many reads the client sees. So the boundary test asserts what is actually guaranteed: the recorder's count equals
the number of chunks the application observed in the same run, and no bytes are lost, duplicated or reordered.
"""
from __future__ import annotations

import hashlib

import pytest

from blackbox.cap_sdk.base import Recorder
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.cap_sdk.seam_httpx_transport import NAME, RecordingTransport, install
from blackbox.cap_sdk.tee import wrap_stream

httpx = pytest.importorskip("httpx")   # optional integration: the core suite must pass without it


def _payload() -> dict:
    return {"model": "mock-1", "stream": True, "messages": [{"role": "user", "content": "name three colors"}]}


def _client(base_url: str, recorder: Recorder | None = None) -> httpx.Client:
    client = httpx.Client(base_url=base_url, timeout=10.0)
    if recorder is not None:
        handle = install(recorder, client=client)
        assert handle.installed and not handle.restored
    return client


def _stream_all(client: httpx.Client) -> list[bytes]:
    with client.stream("POST", "/v1/chat/completions", json=_payload()) as response:
        assert response.status_code == 200
        return list(response.iter_bytes())


def test_the_bytes_the_application_receives_are_unchanged():
    with MockProvider(Scenario(chunks=6)) as provider:
        with _client(provider.base_url) as bare:
            expected = b"".join(_stream_all(bare))

        recorder = Recorder()
        with _client(provider.base_url, recorder) as teed:
            actual = b"".join(_stream_all(teed))

    assert hashlib.sha256(actual).hexdigest() == hashlib.sha256(expected).hexdigest()
    assert b"[DONE]" in actual


def test_the_recorder_sees_exactly_the_chunks_the_application_saw():
    recorder = Recorder()
    with MockProvider(Scenario(chunks=6)) as provider, _client(provider.base_url, recorder) as client:
        observed = _stream_all(client)

    call = recorder.calls[0]
    assert call.chunks == len(observed)
    assert call.bytes_received == sum(len(piece) for piece in observed) == len(b"".join(observed))
    assert call.status == 200
    assert call.stream is True
    assert call.error is None


def test_first_byte_is_recorded_inside_the_total():
    recorder = Recorder()
    with MockProvider(Scenario(chunks=4, first_byte_delay_ms=60)) as provider, \
            _client(provider.base_url, recorder) as client:
        _stream_all(client)

    call = recorder.calls[0]
    assert call.first_byte_ms is not None and call.total_ms is not None
    assert call.first_byte_ms >= 50, "the provider waited 60 ms before its first byte; the seam missed most of it"
    assert call.first_byte_ms <= call.total_ms


def test_a_non_streaming_call_records_every_byte_it_received():
    """A buffered `post` is still a stream underneath, so the count is socket reads, not "one chunk". How many reads
    is a transport detail; the number that matters is bytes, and coalescing them into fewer tape events is P3-06."""
    recorder = Recorder()
    with MockProvider(Scenario(chunks=3)) as provider, _client(provider.base_url, recorder) as client:
        response = client.post("/v1/chat/completions", json=_payload())
    call = recorder.calls[0]
    assert response.status_code == 200
    assert call.chunks >= 1
    assert call.bytes_received == len(response.content)


def test_an_abandoned_stream_records_only_what_was_read():
    """A `break` out of the loop is the commonest real pattern; the call must be closed, not left open forever."""
    recorder = Recorder()
    with MockProvider(Scenario(chunks=8, delay_ms=20)) as provider, _client(provider.base_url, recorder) as client:
        request = client.build_request("POST", "/v1/chat/completions", json=_payload())
        response = client.send(request, stream=True)
        first = next(response.iter_bytes())          # then walk away, the way a caller with a timeout does
        assert first
        response.close()

    call = recorder.calls[0]
    assert call.status == 200
    assert call.chunks == 1
    assert call.bytes_received == len(first)
    assert call.total_ms is not None


def test_a_truncated_stream_is_recorded_short_rather_than_completed():
    """The provider closes the socket early. The client cannot tell that from a normal end-of-body — so the seam's
    only honest job is to record the bytes that arrived and *not* invent the ones that did not."""
    recorder = Recorder()
    with MockProvider(Scenario(chunks=10, truncate_after=3, delay_ms=5)) as provider, \
            _client(provider.base_url, recorder) as client:
        body = b"".join(_stream_all(client))

    call = recorder.calls[0]
    assert b"[DONE]" not in body
    assert call.bytes_received == len(body)
    assert call.chunks == len(body.split(b"\n\n")) - 1, "the recorder invented or dropped a chunk"
    assert call.error is None, "a short body is not an error at this layer; the tape stays faithful"


def test_an_error_mid_stream_is_recorded_and_re_raised():
    class Exploding:
        def __iter__(self):
            yield b"data: one\n\n"
            raise httpx.ReadError("socket died mid-event")

        def close(self) -> None:
            return

    recorder = Recorder()
    call = recorder.open_call("127.0.0.1", "/v1/chat/completions", b"{}")
    teed = wrap_stream(Exploding(), recorder, call, 200, 0)

    seen = []
    with pytest.raises(httpx.ReadError):
        for chunk in teed:
            seen.append(chunk)

    assert seen == [b"data: one\n\n"]
    assert call.chunks == 1
    assert call.error is not None and "ReadError" in call.error
    assert call.status == 200


def test_uninstall_restores_the_clients_own_transport():
    with MockProvider(Scenario(chunks=2)) as provider:
        recorder = Recorder()
        client = httpx.Client(base_url=provider.base_url, timeout=10.0)
        original = client._transport
        handle = install(recorder, client=client)
        assert isinstance(client._transport, RecordingTransport)

        handle.uninstall()
        assert client._transport is original
        handle.uninstall()                       # idempotent
        client.post("/v1/chat/completions", json=_payload())
        client.close()

    assert recorder.calls == [], "a call made after uninstall was still recorded"


def test_the_class_form_patches_and_restores_the_transport_class():
    original = httpx.HTTPTransport.handle_request
    recorder = Recorder()
    handle = install(recorder)
    try:
        assert httpx.HTTPTransport.handle_request is not original
        with MockProvider(Scenario(chunks=2)) as provider, \
                httpx.Client(base_url=provider.base_url, timeout=10.0) as client:
            client.post("/v1/chat/completions", json=_payload())
        assert len(recorder.calls) == 1, "a client created after install was not captured"
    finally:
        handle.uninstall()

    assert httpx.HTTPTransport.handle_request is original
    assert handle.name == NAME
    assert handle.detail["form"] == "class"
