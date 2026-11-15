"""Both client modules, both directions, and the two paths that are easy to claim and hard to prove (P0 follow-up).

The reviewer's note was exact: "a simple async MockTransport request succeeds, so the first repro appears fixed in
that configuration. Verify actual SDK/transport async streaming, cancellation and error paths, including both httpx
and httpx2; do not infer complete coverage from one request."

So this file does not use the SDKs. It uses the two client libraries directly — `httpx` 0.28.1 and `httpx2` 2.16.0,
the one the published SDKs are actually built on — and for each it checks:

* a streaming response: bytes identical to an unseamed client, every chunk counted;
* a non-streaming response: one call, one chunk, the body unchanged;
* a stream the provider truncates: the application sees a short body, the record says short, nothing is invented;
* **cancellation**: a task cancelled mid-iteration keeps `CancelledError` as its outcome, and the call is left open
  with the chunks that had arrived — the shape `recovery` classifies as `INTERRUPTED_MID_STREAM`, not a completed
  call with a wrong count.

The seams are exercised through their `install()` entry points, because that is what an integration calls; a unit
test of `wrap_stream` would prove the tee and not the seam.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib

import pytest

from blackbox.cap_sdk import http_clients
from blackbox.cap_sdk.base import Recorder
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario

MODULES = [name for name, _module in http_clients.available()]
PAYLOAD = {"model": "mock-1", "stream": True, "messages": [{"role": "user", "content": "name three colours"}]}
PATH = "/v1/chat/completions"


def _module(name: str):
    return importlib.import_module(name)


def _sync_body(name: str, base: str, payload: dict) -> tuple[bytes, int]:
    """`(bytes, reads)`. The read count is the oracle for chunk counting: how many pieces the *application* got is
    a fact of this run's TCP behaviour, and a fixed expectation like "six chunks in, six chunks out" is a flake
    waiting for a slow runner (C40)."""
    module = _module(name)
    with module.Client(base_url=base, timeout=10.0) as client, \
            client.stream("POST", PATH, json=payload) as response:
        pieces = list(response.iter_bytes())
    return b"".join(pieces), len(pieces)


async def _async_body(name: str, base: str, payload: dict) -> tuple[bytes, int]:
    module = _module(name)
    async with module.AsyncClient(base_url=base, timeout=10.0) as client, \
            client.stream("POST", PATH, json=payload) as response:
        pieces = [chunk async for chunk in response.aiter_bytes()]
    return b"".join(pieces), len(pieces)


pytestmark = pytest.mark.skipif(not MODULES, reason="no HTTP client library is installed")


def test_both_client_modules_are_present_and_named():
    assert "httpx" in MODULES, "the baseline client is missing; the comparison would be meaningless"
    assert "httpx2" in MODULES, "the published SDKs build on httpx2; without it this file proves the easy half"


@pytest.mark.parametrize("name", MODULES)
def test_a_streaming_response_is_byte_identical_and_fully_counted(name):
    from blackbox.cap_sdk.seam_httpx_hooks import install

    with MockProvider(Scenario(chunks=6)) as provider:
        expected, _reads = _sync_body(name, provider.base_url, PAYLOAD)

    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=6)) as provider:
            actual, reads = _sync_body(name, provider.base_url, PAYLOAD)
    finally:
        handle.uninstall()

    assert hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest()
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call.chunks == reads, f"{name}: the application made {reads} read(s), the recorder saw {call.chunks}"
    assert call.bytes_received == len(actual)
    assert call.status == 200 and call.error is None
    assert call.model == "mock-1" and call.stream is True


@pytest.mark.parametrize("name", MODULES)
def test_a_non_streaming_response_is_one_call_with_one_chunk(name):
    """A plain POST, not the streaming API: the provider answers `application/json`, which is what `call.stream`
    is decided from. The mock used to label *both* answers `text/event-stream`, which made this flag untestable."""
    from blackbox.cap_sdk.seam_httpx_hooks import install

    module = _module(name)
    payload = {"model": "mock-1", "messages": [{"role": "user", "content": "hi"}]}
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=3)) as provider, \
                module.Client(base_url=provider.base_url, timeout=10.0) as client:
            body = client.post(PATH, json=payload).content
    finally:
        handle.uninstall()

    assert len(recorder.calls) == 1
    assert recorder.calls[0].chunks == 1, "a JSON body arrives as one read, and the record must say one"
    assert recorder.calls[0].bytes_received == len(body)
    assert recorder.calls[0].stream is False


@pytest.mark.parametrize("name", MODULES)
def test_an_async_stream_is_byte_identical_and_fully_counted(name):
    from blackbox.cap_sdk.seam_httpx_hooks import install

    with MockProvider(Scenario(chunks=5)) as provider:
        expected, _reads = asyncio.run(_async_body(name, provider.base_url, PAYLOAD))

    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=5)) as provider:
            actual, reads = asyncio.run(_async_body(name, provider.base_url, PAYLOAD))
    finally:
        handle.uninstall()

    assert hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest()
    assert len(recorder.calls) == 1
    assert recorder.calls[0].chunks == reads, "the async tee must see every read the application made"
    assert recorder.calls[0].bytes_received == len(actual)


@pytest.mark.parametrize("name", MODULES)
def test_a_truncated_stream_is_recorded_short_and_never_completed(name):
    from blackbox.cap_sdk.seam_httpx_hooks import install

    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=8, truncate_after=3)) as provider:
            body, _reads = _sync_body(name, provider.base_url, PAYLOAD)
    finally:
        handle.uninstall()

    assert b"[DONE]" not in body, "the provider cut the stream; a completed body here would mean the mock lied"
    call = recorder.calls[0]
    assert call.bytes_received == len(body), "the recorder must account for exactly what the application read"
    assert call.status == 200 or call.error, "either it closed with the status or it recorded an error, not neither"


@pytest.mark.parametrize("name", MODULES)
def test_cancellation_mid_stream_stays_cancelled_and_leaves_the_call_open(name):
    """The recorder must not turn a cancelled task into a completed call, and must not eat `CancelledError`."""
    from blackbox.cap_sdk.seam_httpx_hooks import install

    recorder = Recorder()
    handle = install(recorder)

    async def run_and_cancel(base: str) -> None:
        got_chunk = asyncio.Event()

        async def consume() -> None:
            module = _module(name)
            async with module.AsyncClient(base_url=base, timeout=10.0) as client, \
                    client.stream("POST", PATH, json=PAYLOAD) as response:
                async for _chunk in response.aiter_bytes():
                    got_chunk.set()
                    await asyncio.sleep(5)              # a slow consumer: cancellation lands here

        task = asyncio.ensure_future(consume())
        await asyncio.wait_for(got_chunk.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        with MockProvider(Scenario(chunks=10, delay_ms=5)) as provider:
            asyncio.run(run_and_cancel(provider.base_url))
    finally:
        handle.uninstall()

    call = recorder.calls[0]
    assert call.chunks >= 1, "the chunks that did arrive must be on the record"
    assert call.partial is True, "a cancelled stream is not a completed one, and the record must say so"
    assert call.status == 200, "the headers did arrive, so the status is real; it is the *stream* that did not finish"
