"""P3-05a/05b, async half — the path that was implemented and unexercised until defect 20 was closed.

There was a reason to be suspicious of it rather than merely apologetic: an `httpx.AsyncClient` hands back an
`AsyncByteStream`, and both seams used to wrap response streams in the **sync** tee. That is not "untested" — the
first async call would have failed inside httpx, with the recorder installed and the application broken, which is the
opposite of fail-open. So this file exists for the same reason as the others: the claim is that these seams record
every chunk, and an async claim that nobody ran is not that claim.

`asyncio.run` is stdlib, so an async harness costs nothing here.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from blackbox.cap_sdk.base import Recorder
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.cap_sdk.seam_httpx_hooks import install as install_hooks
from blackbox.cap_sdk.seam_httpx_transport import install as install_transport

httpx = pytest.importorskip("httpx")


def _payload() -> dict:
    return {"model": "mock-1", "stream": True, "messages": [{"role": "user", "content": "hi"}]}


async def _read_all(client: httpx.AsyncClient) -> list[bytes]:
    async with client.stream("POST", "/v1/chat/completions", json=_payload()) as response:
        assert response.status_code == 200
        return [chunk async for chunk in response.aiter_bytes()]


def test_seam_a_records_an_async_stream_chunk_by_chunk():
    with MockProvider(Scenario(chunks=4, delay_ms=5)) as provider:
        recorder = Recorder()
        handle = install_hooks(recorder)
        try:
            async def run() -> list[bytes]:
                async with httpx.AsyncClient(base_url=provider.base_url, timeout=10.0) as client:
                    return await _read_all(client)

            chunks = asyncio.run(run())
        finally:
            handle.uninstall()

    streamed = [call for call in recorder.calls if call.path.endswith("/chat/completions")]
    assert len(streamed) == 1
    call = streamed[0]
    assert call.chunks == len(chunks)
    assert call.bytes_received == sum(len(piece) for piece in chunks)
    assert call.status == 200
    assert call.error is None


def test_seam_a_forwards_an_async_stream_byte_for_byte():
    with MockProvider(Scenario(chunks=4)) as provider:
        async def bare() -> bytes:
            async with httpx.AsyncClient(base_url=provider.base_url, timeout=10.0) as client:
                return b"".join(await _read_all(client))

        expected = asyncio.run(bare())

        recorder = Recorder()
        handle = install_hooks(recorder)
        try:
            async def teed() -> bytes:
                async with httpx.AsyncClient(base_url=provider.base_url, timeout=10.0) as client:
                    return b"".join(await _read_all(client))

            actual = asyncio.run(teed())
        finally:
            handle.uninstall()

    assert hashlib.sha256(actual).hexdigest() == hashlib.sha256(expected).hexdigest()


def test_seam_b_records_an_async_stream_through_the_instance_install():
    with MockProvider(Scenario(chunks=4, delay_ms=5)) as provider:
        recorder = Recorder()

        async def run() -> list[bytes]:
            client = httpx.AsyncClient(base_url=provider.base_url, timeout=10.0)
            handle = install_transport(recorder, client=client)
            try:
                return await _read_all(client)
            finally:
                handle.uninstall()
                await client.aclose()

        chunks = asyncio.run(run())

    call = recorder.calls[0]
    assert call.chunks == len(chunks) >= 1
    assert call.bytes_received == sum(len(piece) for piece in chunks)
    assert call.status == 200


def test_seam_b_records_an_async_stream_through_the_class_patch():
    recorder = Recorder()
    handle = install_transport(recorder)
    try:
        with MockProvider(Scenario(chunks=3, delay_ms=5)) as provider:
            async def run() -> int:
                async with httpx.AsyncClient(base_url=provider.base_url, timeout=10.0) as client:
                    return len(await _read_all(client))

            count = asyncio.run(run())
    finally:
        handle.uninstall()

    assert count >= 1
    assert len(recorder.calls) == 1
    assert recorder.calls[0].bytes_received > 0
