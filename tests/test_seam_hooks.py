"""P3-05a — seam A (the httpx response hook). The claim to defend: it sees every chunk without patching a transport.

That claim is worth testing rather than asserting because seam A's capability is a timing fact about httpx — hooks
run after the headers and before the body is read — and timing facts rot. If a future httpx moves the hook call, the
stream is already consumed when the hook runs, and this file fails loudly instead of the recorder quietly writing
zero-byte calls that look like model output that never arrived.

The `partial` marker is the other half: when the hook *cannot* see the body, the record has to say so.
"""
from __future__ import annotations

import hashlib

import pytest

from blackbox.cap_sdk.base import Recorder
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.cap_sdk.seam_httpx_hooks import NAME, install

httpx = pytest.importorskip("httpx")   # optional integration: the core suite must pass without it


def _payload() -> dict:
    return {"model": "mock-1", "stream": True, "messages": [{"role": "user", "content": "hello"}]}


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=10.0)


def test_install_makes_later_clients_capture_streams_chunk_by_chunk():
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=5, delay_ms=5)) as provider, _client(provider.base_url) as client, \
                client.stream("POST", "/v1/chat/completions", json=_payload()) as response:
            chunks = list(response.iter_bytes())
    finally:
        handle.uninstall()

    call = recorder.calls[0]
    assert call.chunks == len(chunks)
    assert call.bytes_received == sum(len(piece) for piece in chunks)
    assert call.status == 200
    assert call.stream is True
    assert call.partial is False


def test_the_application_receives_the_same_bytes_with_the_hook_installed():
    with MockProvider(Scenario(chunks=5)) as provider, _client(provider.base_url) as bare, \
            bare.stream("POST", "/v1/chat/completions", json=_payload()) as response:
        expected = b"".join(response.iter_bytes())

    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=5)) as provider, _client(provider.base_url) as client, \
                client.stream("POST", "/v1/chat/completions", json=_payload()) as response:
            actual = b"".join(response.iter_bytes())
    finally:
        handle.uninstall()

    assert hashlib.sha256(actual).hexdigest() == hashlib.sha256(expected).hexdigest()
    assert recorder.calls[0].bytes_received == len(expected)


def test_a_buffered_post_is_recorded_with_its_body_and_model():
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=3)) as provider, _client(provider.base_url) as client:
            response = client.post("/v1/chat/completions", json=_payload())
    finally:
        handle.uninstall()

    call = recorder.calls[0]
    assert call.bytes_received == len(response.content)
    assert call.model == "mock-1", "the model name is in the request body and belongs in the record"
    assert call.request_fp and call.response_fp
    assert call.chunks >= 1


def test_an_already_buffered_response_is_marked_partial_rather_than_recorded_as_empty():
    """The hook cannot wrap a stream that has already been consumed. Say so; do not write a zero-byte call."""
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=2)) as provider, _client(provider.base_url) as client:
            response = client.get("/health")
            response.read()
            response.close()
            assert response.is_closed
            # Re-run the hook the seam installed, over a response whose body is already gone: the replay/late case.
            client._event_hooks["response"][-1](response)
    finally:
        handle.uninstall()

    call = recorder.calls[-1]
    assert call.partial is True
    assert call.bytes_received == 0
    assert call.status == 200


def test_uninstall_restores_the_client_classes_and_stops_recording():
    original_sync = httpx.Client.__init__
    original_async = httpx.AsyncClient.__init__
    recorder = Recorder()
    handle = install(recorder)
    try:
        assert httpx.Client.__init__ is not original_sync
        handle.uninstall()
        assert httpx.Client.__init__ is original_sync
        assert httpx.AsyncClient.__init__ is original_async
        handle.uninstall()                                   # idempotent
        with MockProvider(Scenario(chunks=2)) as provider, _client(provider.base_url) as client:
            client.post("/v1/chat/completions", json=_payload())
    finally:
        handle.uninstall()

    assert recorder.calls == [], "a call made after uninstall was still recorded"
    assert handle.name == NAME


def test_an_existing_event_hook_still_runs_alongside_the_seam():
    seen: list[int] = []
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=2)) as provider:
            client = httpx.Client(base_url=provider.base_url, timeout=10.0,
                                  event_hooks={"response": [lambda r: seen.append(r.status_code)]})
            with client:
                client.post("/v1/chat/completions", json=_payload())
    finally:
        handle.uninstall()

    assert seen == [200], "the seam replaced the caller's hooks instead of adding to them"
    assert len(recorder.calls) == 1


def test_a_recorder_that_raises_never_reaches_the_application():
    class Hostile(Recorder):
        def open_call(self, provider, path, request):        # type: ignore[override]
            raise RuntimeError("recorder exploded")

    handle = install(Hostile())
    try:
        with MockProvider(Scenario(chunks=2)) as provider, _client(provider.base_url) as client:
            response = client.post("/v1/chat/completions", json=_payload())
    finally:
        handle.uninstall()

    assert response.status_code == 200
    assert b"[DONE]" in response.content, "fail-open means the application got its whole body"
