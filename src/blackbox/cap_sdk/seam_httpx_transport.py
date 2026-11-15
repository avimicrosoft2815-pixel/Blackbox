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

from blackbox.cap_sdk.base import Receiver, SeamHandle, SeamUnavailable
from blackbox.cap_sdk.tee import wrap_stream

NAME = "httpx-transport"


def probe() -> tuple[bool, str]:
    try:
        import httpx  # noqa: F401
    except Exception as exc:                     # pragma: no cover
        return False, f"httpx is not importable: {type(exc).__name__}"
    return True, "httpx is importable; the transport wrapper can tee the stream byte for byte"


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

    def close(self) -> None:
        inner_close = getattr(self.inner, "close", None)
        if inner_close is not None:
            inner_close()

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

    import httpx

    if client is not None:
        original = client._transport
        client._transport = RecordingTransport(original, recorder)

        def undo_instance() -> None:
            client._transport = original

        return SeamHandle(name=NAME, undo=undo_instance,
                          detail={"patched": "client._transport", "form": "instance"})

    original_handle = _patch_transport_class(httpx.HTTPTransport, recorder)

    def undo_class() -> None:
        _restore(httpx.HTTPTransport, "handle_request", original_handle)

    return SeamHandle(name=NAME, undo=undo_class, detail={"patched": "httpx.HTTPTransport", "form": "class"})


def _restore(cls: Any, attribute: str, value: Any) -> None:
    setattr(cls, attribute, value)


def _patch_transport_class(cls: Any, recorder: Receiver) -> Any:
    original = cls.handle_request

    def handle(self: Any, request: Any) -> Any:
        return RecordingTransport(_Bound(original, self), recorder).handle_request(request)

    cls.handle_request = handle
    return original


class _Bound:
    """Turns an unbound `handle_request` plus an instance into something with `handle_request` again."""

    def __init__(self, fn: Any, instance: Any) -> None:
        self.fn = fn
        self.instance = instance

    def handle_request(self, request: Any) -> Any:
        return self.fn(self.instance, request)
