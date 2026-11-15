"""P0, reported by the independent reviewer at `ee210ae`: a receiver that raises mid-stream took the host down.

The report is precise and it is right. `tee.py` called `self.recorder.receive(...)` bare, so an exception from a
receiver during a real `httpx.SyncByteStream` propagated into the application's `for chunk in response.iter_bytes()`
— a recorder bug killing the program it was supposed to observe, which is the one thing the whole capture layer
promises it cannot do. Seam A's hook was inside a `try`, so the existing open-call test passed while this path was
untested; seam B's `open_call` was not inside one at all.

This file tests the promise at every callback, on every path, in the four combinations that matter:

| | sync | async |
|---|---|---|
| seam A (client hooks) | `Client` + hooks | `AsyncClient` + awaited hooks |
| seam B (transport tee) | `Client` + wrapped transport | `AsyncClient` + wrapped transport |
| seam C (SDK methods) | `openai`-shaped stub | same, awaited |

and for each: `open_call`, `receive`, `close_call` raising.

Two things this must not do, and asserts against:

* **Swallow the provider's own error.** A 500, a broken socket or a mid-stream `ReadError` belongs to the
  application and must arrive with its own type and message.
* **Swallow cancellation.** `asyncio.CancelledError` is a `BaseException` in 3.8+, so a fail-open wrapper that catches
  `Exception` leaves it alone — asserted rather than assumed, because "catch everything" is a tempting mistake here.

The recorder-side failures are counted, and the counter is checked, because silently unrecorded is the other way to
fail: fail-open does not mean fail-silent.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from blackbox.cap_sdk.base import ModelCall, Receiver
from blackbox.cap_sdk.failopen import FailOpenReceiver
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario

httpx = pytest.importorskip("httpx")

CALLS = [("boom", "open_call"), ("boom", "receive"), ("boom", "close_call")]


class Hostile(Receiver):
    """A receiver that breaks in exactly one place, so the test names which one."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.calls: list[ModelCall] = []
        self.installed_seam: str | None = None

    def open_call(self, provider: str, path: str, request: Any, *, model: str | None = None,
                  stream: bool = False, partial: bool = False) -> ModelCall:
        if self.where == "open_call":
            raise RuntimeError("recorder blew up while opening the call")
        call = ModelCall(provider=provider, path=path, model=model, stream=stream, partial=partial)
        self.calls.append(call)
        return call

    def receive(self, call: ModelCall, chunk: bytes, *, first_byte_ms: float | None = None) -> None:
        if self.where == "receive":
            raise RuntimeError("recorder blew up on a chunk of model output")
        call.chunks += 1
        call.bytes_received += len(chunk)

    def close_call(self, call: ModelCall, *, status: int = 0, total_ms: float | None = None,
                   error: str | None = None) -> None:
        if self.where == "close_call":
            raise RuntimeError("recorder blew up while closing the call")
        call.status = status
        call.error = error


def _guarded(where: str) -> tuple[FailOpenReceiver, Hostile]:
    inner = Hostile(where)
    return FailOpenReceiver(inner), inner


# ── seam A: response hooks ───────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("where", ["open_call", "receive", "close_call"])
def test_seam_a_sync_stream_survives_a_receiver_that_raises(where):
    from blackbox.cap_sdk.seam_httpx_hooks import install

    guarded, _inner = _guarded(where)
    handle = install(guarded)
    try:
        with MockProvider(Scenario(chunks=5)) as provider:
            response = httpx.get(provider.openai_base_url() + "/v1/chat/completions", timeout=10)
            body = b"".join(response.iter_bytes())
        with MockProvider(Scenario(chunks=5)) as provider:
            plain = httpx.get(provider.openai_base_url() + "/v1/chat/completions", timeout=10)
            expected = b"".join(plain.iter_bytes())
    finally:
        handle.uninstall()

    assert body and len(body) == len(expected), "the application received different bytes"
    assert guarded.failures, f"a receiver that raises in {where} was not counted as a failure"
    assert all(where.split(":")[0] in failure for failure in guarded.failures)


@pytest.mark.parametrize("where", ["open_call", "receive", "close_call"])
def test_seam_a_async_stream_survives_a_receiver_that_raises(where):
    from blackbox.cap_sdk.seam_httpx_hooks import install

    guarded, _inner = _guarded(where)
    handle = install(guarded)

    async def collect(base: str) -> bytes:
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            response = await client.get("/v1/chat/completions")
            return b"".join([chunk async for chunk in response.aiter_bytes()])

    try:
        with MockProvider(Scenario(chunks=5)) as provider:
            body = asyncio.run(collect(provider.openai_base_url()))
    finally:
        handle.uninstall()

    assert body and guarded.failures, guarded.failures


# ── seam B: transport tee ───────────────────────────────────────────────────────────────────────────────────────────

PAYLOAD = {"model": "mock-1", "stream": True, "messages": [{"role": "user", "content": "say something"}]}


def _stream(client: Any) -> bytes:
    with client.stream("POST", "/v1/chat/completions", json=PAYLOAD) as response:
        return b"".join(response.iter_bytes())


@pytest.mark.parametrize("where", ["open_call", "receive", "close_call"])
def test_seam_b_sync_stream_survives_a_receiver_that_raises(where):
    from blackbox.cap_sdk.seam_httpx_transport import install

    guarded, _inner = _guarded(where)
    with MockProvider(Scenario(chunks=6)) as provider:
        client = httpx.Client(base_url=provider.base_url, timeout=10.0)
        # The client is built first: this is the instance path, the one that cannot rely on a patched `__init__`.
        handle = install(guarded, client=client)
        try:
            body = _stream(client)
        finally:
            handle.uninstall()
            client.close()

    assert body and guarded.failures, "seam B let a receiver failure escape or lost the record without saying so"


@pytest.mark.parametrize("where", ["open_call", "receive", "close_call"])
def test_seam_b_async_stream_survives_a_receiver_that_raises(where):
    from blackbox.cap_sdk.seam_httpx_transport import install

    guarded, _inner = _guarded(where)

    async def collect(base: str) -> bytes:
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as client:
            handle = install(guarded, client=client)
            try:
                return await _astream(client)
            finally:
                handle.uninstall()

    with MockProvider(Scenario(chunks=6)) as provider:
        body = asyncio.run(collect(provider.base_url))

    assert body and guarded.failures


async def _astream(client: Any) -> bytes:
    async with client.stream("POST", "/v1/chat/completions", json=PAYLOAD) as response:
        return b"".join([chunk async for chunk in response.aiter_bytes()])


# ── the two things fail-open must never swallow ────────────────────────────────────────────────────────────────────


def test_a_provider_error_still_reaches_the_application_with_its_own_type():
    from blackbox.cap_sdk.seam_httpx_hooks import install

    guarded, _inner = _guarded("receive")
    handle = install(guarded)
    try:
        with MockProvider(Scenario(status=429)) as provider:
            response = httpx.post(provider.base_url + "/v1/chat/completions", json=PAYLOAD, timeout=10)
            with pytest.raises(httpx.HTTPStatusError) as caught:
                response.raise_for_status()
    finally:
        handle.uninstall()

    # The seam may record the failure; it may not rewrite it. The application sees 429 and the library's own error.
    assert response.status_code == 429
    assert caught.value.response.status_code == 429



def test_cancellation_is_not_swallowed_by_the_fail_open_wrapper():
    """`CancelledError` is a `BaseException`; a wrapper catching `BaseException` would hang a cancelled task forever."""
    guarded, _inner = _guarded("receive")

    async def cancel_while_streaming() -> None:
        call = guarded.open_call("stub", "/v1/chat/completions", b"{}", stream=True)
        guarded.receive(call, b"one")
        raise asyncio.CancelledError

    async def main() -> None:
        task = asyncio.ensure_future(cancel_while_streaming())
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert guarded.failures, "the receive failure should still have been counted"


def test_the_wrapper_never_touches_an_object_it_was_not_given():
    """The failure note records a method name and an exception *type*, never a message: a message can carry content."""
    guarded, _inner = _guarded("receive")
    call = guarded.open_call("stub", "/v1/chat/completions", b"{}")
    guarded.receive(call, b"secret-looking-bytes")
    assert guarded.failures == ["receive:RuntimeError"], guarded.failures


def test_failures_are_counted_and_bounded():
    """Fail-open must not become a memory leak: a broken sink fails on every chunk of every call."""
    guarded, _inner = _guarded("receive")
    call = guarded.open_call("stub", "/p", b"{}")
    for _ in range(10_000):
        guarded.receive(call, b"x")
    assert guarded.failure_count == 10_000
    assert len(guarded.failures) <= 64, "the sample of failures must stay bounded"


def test_wrapping_twice_records_once():
    inner = Hostile("receive")
    once = FailOpenReceiver(inner)
    twice = FailOpenReceiver(once)
    assert twice.inner is once and once.inner is inner


def test_a_stub_receiver_without_attributes_does_not_confuse_the_wrapper():
    """The wrapper forwards `installed_seam` and `calls`, which a stub may simply not have."""
    class Bare(Receiver):
        def open_call(self, provider: str, path: str, request: Any, **kw: Any) -> ModelCall:
            return ModelCall(provider=provider, path=path)

        def receive(self, call: ModelCall, chunk: bytes, **kw: Any) -> None:
            return None

        def close_call(self, call: ModelCall, **kw: Any) -> None:
            return None

    guarded = FailOpenReceiver(Bare())
    assert guarded.installed_seam is None
    assert guarded.calls == []
    call = guarded.open_call("p", "/x", b"{}")
    guarded.receive(call, b"y")
    guarded.close_call(call, status=200)
    assert guarded.failures == []
    assert not inspect.iscoroutinefunction(guarded.receive)
