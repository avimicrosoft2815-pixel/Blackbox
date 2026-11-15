"""Seam B (P3-05b): a transport wrapper that tees the *bytes*.

Seam A is a response hook and needs the client class to accept event hooks. Seam B sits one layer lower and needs
nothing from the SDK at all: it wraps `httpx.HTTPTransport` (or any transport with `handle_request`), calls through,
and wraps the response stream before handing it back. That is what makes it the fallback for the case the plan calls
out — *the SDK owns the client and hands us nothing* — since a library that builds its own client and ignores hooks
still goes through `HTTPTransport.handle_request`.

The tee itself lives in `tee.py`, shared with seam A: the recording of a chunk must not depend on which seam found
the stream. What makes a partial stream reconstructable after `kill -9` is that each chunk is written to the tape as
it arrives — the recorder has the chunks received, with byte counts, before the process dies.

Three things it deliberately does not do:

* **It does not buffer the whole body, and it does not truncate a chunk.** A 2 000-token stream must not turn into
  a list held by the recorder, so each chunk is handed to the sink and dropped; and no chunk is ever cut to fit a
  buffer, because a truncated chunk would make `bytes_received` a lie for large reads. Event coalescing (P3-06) is
  the sink's decision, made with the full byte count in hand.
* **It does not change what the application receives.** The bytes are re-yielded unchanged via
  `httpx.Response(..., stream=...)`, so a caller that checks `sha256` over the body gets the same digest with or
  without the seam — which is the test.
* **It does not swallow errors.** A mid-stream error is recorded and then re-raised in the same shape httpx would
  have raised.

Chunk *coalescing* (8 KiB / 100 ms, P3-06) belongs one level above this file: the seam reports every read, and the
sink decides how many events to write. That split is why the byte counts here are exact even when the event count
is small.
"""
from __future__ import annotations

import time
from typing import Any

from blackbox.cap_sdk import http_clients
from blackbox.cap_sdk.base import Receiver, SeamHandle, SeamUnavailable
from blackbox.cap_sdk.tee import wrap_stream

NAME = "httpx-transport"


def probe() -> tuple[bool, str]:
    found = http_clients.available()
    if not found:
        return False, "no known HTTP client module is importable (httpx, httpx2)"
    return True, "transport wrapper can tee the stream byte for byte for: " + ", ".join(n for n, _ in found)


class RecordingTransport:
    """A drop-in for `httpx.BaseTransport`. Wraps any transport whose `handle_request` returns a `Response`."""

    def __init__(self, inner: Any, recorder: Receiver) -> None:
        self.inner = inner
        self.recorder = recorder

    def handle_request(self, request: Any) -> Any:
        started = time.perf_counter_ns()
        body = getattr(request, "content", b"") or b""
        call = self.recorder.open_call(request.url.host or "unknown", request.url.path or "/", body)
        call.stream = True
        try:
            response = self.inner.handle_request(request)
        except Exception as exc:
            self.recorder.close_call(call, error=f"{type(exc).__name__}: {exc}",
                                     total_ms=(time.perf_counter_ns() - started) / 1_000_000)
            raise
        # Hand the response back *unread*: the client decides when to consume it, exactly as without the seam. Reading
        # here would fill `_content`, and httpx then replays a cached body in one piece — the caller would still get
        # the right bytes but would silently lose every chunk boundary, which is the thing the recorder exists for.
        response.stream = wrap_stream(response.stream, self.recorder, call, int(response.status_code), started)
        return response

    async def handle_async_request(self, request: Any) -> Any:
        """The async twin of `handle_request`: same bookkeeping, same tee, awaited instead of iterated."""
        started = time.perf_counter_ns()
        body = getattr(request, "content", b"") or b""
        call = self.recorder.open_call(request.url.host or "unknown", request.url.path or "/", body)
        call.stream = True
        try:
            response = await self.inner.handle_async_request(request)
        except Exception as exc:
            self.recorder.close_call(call, error=f"{type(exc).__name__}: {exc}",
                                     total_ms=(time.perf_counter_ns() - started) / 1_000_000)
            raise
        response.stream = wrap_stream(response.stream, self.recorder, call, int(response.status_code), started)
        return response

    async def aclose(self) -> None:
        aclose_inner = getattr(self.inner, "aclose", None)
        if aclose_inner is not None:
            await aclose_inner()

    def close(self) -> None:
        inner_close = getattr(self.inner, "close", None)
        if inner_close is not None:
            inner_close()

    async def __aenter__(self) -> Any:
        aenter = getattr(self.inner, "__aenter__", None)
        if aenter is not None:
            await aenter()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        aexit = getattr(self.inner, "__aexit__", None)
        if aexit is not None:
            return await aexit(*exc)
        await self.aclose()
        return None

    # `httpx.Client.__enter__` enters its transport, so a wrapper that is not a context manager breaks `with Client()`
    def __enter__(self) -> Any:
        enter = getattr(self.inner, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc: Any) -> Any:
        exit_ = getattr(self.inner, "__exit__", None)
        if exit_ is not None:
            return exit_(*exc)
        self.close()
        return None


def install(recorder: Receiver, *, client: Any = None) -> SeamHandle:
    """Wrap an existing client's transport (`client=`), the way the SDK's client factory hands one over.

    With no client given, this patches `httpx.HTTPTransport.handle_request` so clients created afterwards are
    covered. Patching a *class* is riskier than wrapping an instance, so the preflight prefers the instance form and
    `SUPPORTED.md` says which one each integration uses.
    """
    ok, reason = probe()
    if not ok:
        raise SeamUnavailable(reason)

    if client is not None:
        original = client._transport
        client._transport = RecordingTransport(original, recorder)

        def undo_instance() -> None:
            client._transport = original

        return SeamHandle(name=NAME, undo=undo_instance,
                          detail={"patched": "client._transport", "form": "instance"})

    # Two classes, not one: an async client builds an `AsyncHTTPTransport`, so patching only `HTTPTransport` records
    # sync clients and silently misses every async one.
    patched: list[tuple[Any, dict[str, Any]]] = []
    for _module_name, module in http_clients.available():
        for attribute in ("HTTPTransport", "AsyncHTTPTransport"):
            transport_class = getattr(module, attribute, None)
            if transport_class is not None:
                patched.append((transport_class, _patch_transport_class(transport_class, recorder)))

    def undo_class() -> None:
        for transport_class, originals in patched:
            for attribute, original in originals.items():
                _restore(transport_class, attribute, original)

    return SeamHandle(name=NAME, undo=undo_class,
                      detail={"patched": [f"{cls.__module__}.{cls.__name__}" for cls, _ in patched],
                              "modules": [cls.__module__.split(".")[0] for cls, _ in patched],
                              "form": "class"})


def _restore(cls: Any, attribute: str, value: Any) -> None:
    setattr(cls, attribute, value)


def _patch_transport_class(cls: Any, recorder: Receiver) -> dict[str, Any]:
    """Patch both directions, because a client that is never used synchronously still has to be recorded."""
    originals: dict[str, Any] = {}
    if hasattr(cls, "handle_request"):
        sync_original = cls.handle_request

        def handle(self: Any, request: Any) -> Any:
            return RecordingTransport(_Bound(sync_original, self), recorder).handle_request(request)

        cls.handle_request = handle
        originals["handle_request"] = sync_original
    if hasattr(cls, "handle_async_request"):
        async_original = cls.handle_async_request

        async def handle_async(self: Any, request: Any) -> Any:
            return await RecordingTransport(_AsyncBound(async_original, self), recorder).handle_async_request(request)

        cls.handle_async_request = handle_async
        originals["handle_async_request"] = async_original
    if not originals:                                  # pragma: no cover - a transport with neither direction
        raise SeamUnavailable("this transport exposes neither handle_request nor handle_async_request")
    return originals


class _Bound:
    """Turns an unbound `handle_request` plus an instance into something with `handle_request` again."""

    def __init__(self, fn: Any, instance: Any) -> None:
        self.fn = fn
        self.instance = instance

    def handle_request(self, request: Any) -> Any:
        return self.fn(self.instance, request)


class _AsyncBound:
    """The same for `handle_async_request`, which the recorder awaits rather than calls."""

    def __init__(self, fn: Any, instance: Any) -> None:
        self.fn = fn
        self.instance = instance

    async def handle_async_request(self, request: Any) -> Any:
        return await self.fn(self.instance, request)
