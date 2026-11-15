"""The stream tee both HTTP seams share (P3-05a/P3-05b): one implementation of "record each chunk as it passes".

Seam A reaches a response through an `httpx` event hook, seam B through a wrapped transport. In both cases the thing
that has to happen next is identical — the stream is still unread, so it can be wrapped in something that yields the
same bytes and writes each chunk to the recorder on its way past. Writing that twice would be writing it wrong twice
eventually, so it lives here and both seams call `wrap_stream`.

Two properties are load-bearing and are why this is not just a generator:

* **The bytes are passed through untouched.** No decoding here: the transport's raw bytes are what a replay needs,
  and a chunk boundary is information a decoder would destroy.
* **The recorder is told even when the application abandons the stream.** A `break`, a timeout or a `kill -9` leaves
  the call open; `close()` still records the chunks that arrived, because a partial call honestly labelled is worth
  more than a complete-looking one that is a guess.

**Two tee classes, chosen by the stream.** `httpx.AsyncClient` hands back an `AsyncByteStream`, and a sync wrapper
around one is not merely untested — it fails the moment anything iterates it. So `wrap_stream` asks the stream which
kind it is (`__aiter__` or not) and returns the matching tee. An earlier version had only the sync one and a seam that
would have broken every async client it was installed on; the async test is why it is not still there (defect 20).

`httpx.SyncByteStream`/`AsyncByteStream` are the base classes because `httpx.Client` asserts on them, but the imports
happen inside the factories — httpx is an optional integration and importing this module must not require it.
"""
from __future__ import annotations

import time
from typing import Any

from blackbox.cap_sdk.base import ModelCall, Receiver

_TEE_STREAM: Any = None       # built on first use, because httpx may not be installed


def tee_stream_class() -> Any:
    """`httpx.SyncByteStream` subclass, built lazily. Returned, not exported, so the import stays optional."""
    global _TEE_STREAM
    if _TEE_STREAM is not None:
        return _TEE_STREAM

    import httpx

    class TeeStream(httpx.SyncByteStream):
        """Yields exactly the inner stream's chunks, recording each one as it passes through."""

        def __init__(self, inner: Any, recorder: Receiver, call: ModelCall, status: int, started: int) -> None:
            self.inner = inner
            self.recorder = recorder
            self.call = call
            self.status = status
            self.started = started
            self.done = False

        def _elapsed_ms(self) -> float:
            return (time.perf_counter_ns() - self.started) / 1_000_000

        def __iter__(self) -> Any:
            iterator = iter(self.inner)
            while True:
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                except Exception as exc:
                    # An error mid-stream is recorded *and* re-raised: hiding it would hand the application a silent
                    # short stream, and the tape would claim a call that completed.
                    self._finish(error=f"{type(exc).__name__}: {exc}")
                    raise
                if chunk:
                    first_ms = self._elapsed_ms() if self.call.first_byte_ms is None else None
                    self.recorder.receive(self.call, chunk, first_byte_ms=first_ms)
                yield chunk
            self._finish()

        def _finish(self, error: str | None = None) -> None:
            if self.done:
                return
            self.done = True
            self.recorder.close_call(self.call, status=self.status, total_ms=self._elapsed_ms(), error=error)

        def close(self) -> None:
            # The application may abandon a stream (a `break`, a timeout, a `kill -9` never gets here). Closing early
            # is still a completed observation: the call is recorded with the bytes that actually arrived.
            close_inner = getattr(self.inner, "close", None)
            if close_inner is not None:
                close_inner()
            self._finish()

    _TEE_STREAM = TeeStream
    return TeeStream


_ASYNC_TEE_STREAM: Any = None


def async_tee_stream_class() -> Any:
    """`httpx.AsyncByteStream` subclass, built lazily for the same reason as the sync one."""
    global _ASYNC_TEE_STREAM
    if _ASYNC_TEE_STREAM is not None:
        return _ASYNC_TEE_STREAM

    import httpx

    class AsyncTeeStream(httpx.AsyncByteStream):
        def __init__(self, inner: Any, recorder: Receiver, call: ModelCall, status: int, started: int) -> None:
            self.inner = inner
            self.recorder = recorder
            self.call = call
            self.status = status
            self.started = started
            self.done = False

        def _elapsed_ms(self) -> float:
            return (time.perf_counter_ns() - self.started) / 1_000_000

        async def __aiter__(self) -> Any:
            iterator = self.inner.__aiter__()
            while True:
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    self._finish(error=f"{type(exc).__name__}: {exc}")
                    raise
                if chunk:
                    first_ms = self._elapsed_ms() if self.call.first_byte_ms is None else None
                    self.recorder.receive(self.call, chunk, first_byte_ms=first_ms)
                yield chunk
            self._finish()

        def _finish(self, error: str | None = None) -> None:
            if self.done:
                return
            self.done = True
            self.recorder.close_call(self.call, status=self.status, total_ms=self._elapsed_ms(), error=error)

        async def aclose(self) -> None:
            aclose_inner = getattr(self.inner, "aclose", None)
            if aclose_inner is not None:
                await aclose_inner()
            self._finish()

    _ASYNC_TEE_STREAM = AsyncTeeStream
    return AsyncTeeStream


def wrap_stream(stream: Any, recorder: Receiver, call: ModelCall, status: int, started: int) -> Any:
    """Wrap `stream` so every chunk is recorded as it is yielded to the application.

    The stream decides which tee: an async client's stream has `__aiter__`, and wrapping it in the sync tee would
    break it rather than record it.
    """
    if hasattr(stream, "__aiter__"):
        return async_tee_stream_class()(stream, recorder, call, status, started)
    return tee_stream_class()(stream, recorder, call, status, started)
