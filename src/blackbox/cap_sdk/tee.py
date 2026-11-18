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
* **A recorder that raises cannot reach the application.** Every receiver call goes through `fail_open`
  (`cap_sdk/failopen.py`). This was a real defect, reported at `ee210ae`: `receive()` was called bare, so a receiver
  bug propagated out of `iter_bytes()` and killed the host. The unit of the fix is the *callback*, not the seam — the
  earlier test exercised `open_call` and nothing else.

**Two tee classes, chosen by the stream.** `httpx.AsyncClient` hands back an `AsyncByteStream`, and a sync wrapper
around one is not merely untested — it fails the moment anything iterates it. So `wrap_stream` asks the stream which
kind it is (`__aiter__` or not) and returns the matching tee. An earlier version had only the sync one and a seam that
would have broken every async client it was installed on; the async test is why it is not still there (defect 20).

`SyncByteStream`/`AsyncByteStream` are the base classes because the client asserts on them with `isinstance`, and the
base must come from **the same module as the stream**: `httpx2.Client` checks its own class, so a tee subclassing
httpx's would be rejected by an httpx2 client (D-025). The factories are cached per module and the import happens
inside them — a client library is an optional integration and importing this module must not require one.
"""
from __future__ import annotations

import time
from typing import Any

from blackbox.cap_sdk import http_clients
from blackbox.cap_sdk.base import ModelCall, Receiver
from blackbox.cap_sdk.failopen import fail_open

_TEE_STREAMS: dict[str, Any] = {}       # built on first use, per client module
_ASYNC_TEE_STREAMS: dict[str, Any] = {}

#: What a stream of that path must contain before it counts as finished — one entry per convention this build can
#: **vouch for**, each with the provider it belongs to. Anything not listed here is *unknown*, and an unknown ending is
#: recorded as unknown rather than assumed complete (the rule the owner set: never claim a stream is complete when its
#: ending is unknown). The markers can straddle two chunks, which is why the tee keeps a tail instead of testing each
#: chunk on its own.
TERMINATORS: tuple[tuple[str, bytes, str], ...] = (
    ("/chat/completions", b"[DONE]", "openai"),                 # OpenAI chat completions
    ("/completions", b"[DONE]", "openai"),                      # OpenAI legacy completions
    ("/responses", b"response.completed", "openai"),            # OpenAI Responses API
    ("/messages", b"message_stop", "anthropic"),                # Anthropic messages
    ("streamGenerateContent", b'"finishReason"', "google"),      # Gemini streaming (last event carries finishReason)
    ("/api/chat", b'"done":true', "ollama"),                    # Ollama NDJSON chat
    ("/api/generate", b'"done":true', "ollama"),                # Ollama NDJSON generate
)
TAIL_BYTES = 128

#: The three values `ending` can take, and what each one *claims*:
ENDING_TERMINATED = "terminated"       # the provider's own terminator was seen: the stream finished
ENDING_UNTERMINATED = "unterminated"   # a known convention, and its terminator never came: the stream was cut
ENDING_UNVERIFIED = "unverified"       # no convention this build knows: **no claim either way**


def convention_for(path: str) -> tuple[str, bytes, str] | None:
    """The row that describes this path's stream convention, or `None` when this build has none for it.

    **The path must end with the suffix**, and the longest matching suffix wins. Two earlier drafts got this wrong and
    both were the same mistake in different clothes: a substring test made `/v1/messages/batches` "Anthropic messages"
    (a batch listing is not a stream), and it made `/v1/chat/completions` match the legacy `/completions` row as well, so
    one path carried two providers' markers. A convention is a claim about a specific endpoint, and the endpoint is the
    end of the path.
    """
    bare = (path or "").split("?", 1)[0]
    best: tuple[str, bytes, str] | None = None
    for suffix, marker, provider in TERMINATORS:
        if bare.endswith(suffix) and (best is None or len(suffix) > len(best[0])):
            best = (suffix, marker, provider)
    return best


def expected_terminators(path: str) -> list[bytes]:
    """The markers this path's stream must end with, or an empty list when the path's convention is not known here."""
    row = convention_for(path)
    return [row[1]] if row is not None else []


def expected_provider(path: str) -> str:
    """Which convention this path belongs to (`openai`, `anthropic`, `google`, `ollama`), or `"unrecognised"`.

    The word is recorded in the capsule: a reader has to be able to tell "the provider finished" from "this build had no
    convention to judge this stream by", and `unrecognised` is the honest second answer.
    """
    row = convention_for(path)
    return row[2] if row is not None else "unrecognised"


class TerminatorWatch:
    """Remembers whether a stream carried its provider's terminator, without holding the stream.

    Only the last `TAIL_BYTES` are kept: a marker split across two chunks still matches, and a 200 MB stream costs 128
    bytes of memory. `reason()` is what the tee records when the body ended cleanly and the marker never came — the case
    that used to be indistinguishable from a completed stream (`MODEL_RESPONSE` with `partial: False` for a provider that
    hung up after three of six chunks; defect 66).
    """

    def __init__(self, path: str) -> None:
        self.path = path or ""
        self.markers = expected_terminators(path)
        self.provider = expected_provider(path)
        self.tail = b""
        self.marked_unterminated = False

    def feed(self, chunk: bytes) -> None:
        if self.markers and chunk:
            self.tail = (self.tail + bytes(chunk))[-TAIL_BYTES:]

    def saw_terminator(self) -> bool:
        return any(marker in self.tail for marker in self.markers)

    def ending(self) -> str:
        """One of `ENDING_TERMINATED`, `ENDING_UNTERMINATED`, `ENDING_UNVERIFIED` — the capsule's own claim."""
        if not self.markers:
            return ENDING_UNVERIFIED
        return ENDING_TERMINATED if self.saw_terminator() else ENDING_UNTERMINATED

    def reason(self) -> str | None:
        """Why the ending is not `terminated`, in words for the capsule; `None` when it is."""
        if self.ending() == ENDING_TERMINATED:
            return None
        if self.ending() == ENDING_UNVERIFIED:
            return (f"this build does not know {self.path or 'this path'}'s stream convention, so whether the stream "
                    f"finished is not recorded: the body ended, and no terminator was expected or looked for")
        return (f"the stream ended without the provider terminator "
                f"({self.markers[0].decode('ascii', 'replace')}); the body was closed by the other end, not finished")


def tee_stream_class(module_name: str = "httpx") -> Any:
    """`SyncByteStream` subclass for that client module, built lazily so the import stays optional."""
    if module_name in _TEE_STREAMS:
        return _TEE_STREAMS[module_name]

    import importlib

    base_module = importlib.import_module(module_name)

    # The base class is chosen at runtime (httpx or httpx2), which is the whole point: mypy cannot name it.
    class TeeStream(base_module.SyncByteStream):  # type: ignore[name-defined,misc]
        """Yields exactly the inner stream's chunks, recording each one as it passes through."""

        def __init__(self, inner: Any, recorder: Receiver, call: ModelCall, status: int, started: int) -> None:
            self.inner = inner
            self.recorder = recorder
            self.call = call
            self.status = status
            self.started = started
            self.done = False
            self.watch = TerminatorWatch(call.path)

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
                    self._finish(error=f"{type(exc).__name__}: {exc}", completed=False)
                    raise
                if chunk:
                    first_ms = self._elapsed_ms() if self.call.first_byte_ms is None else None
                    self.watch.feed(chunk)
                    self.recorder.receive(self.call, chunk, first_byte_ms=first_ms)
                yield chunk
            self._finish(completed=True)

        def _finish(self, error: str | None = None, completed: bool = True) -> None:
            if self.done:
                return
            self.done = True
            if completed and error is None and not _is_stream(self.call):
                # A body that is not an event stream has no provider terminator to look for, and httpx raises rather
                # than handing back a short read of one: reaching the end of the iteration means the body ended inside
                # its own framing. Looking for `[DONE]` in a JSON body made every ordinary call read as a cut stream
                # (defect 78) — the same mistake the proxy made in the other direction (defect 77).
                self.call.ending = None
                self.call.ending_provider = None
                self.call.end_reason = None
                self.call.partial = False
            elif completed and error is None:
                self.call.ending = self.watch.ending()
                self.call.ending_provider = self.watch.provider
                missing = self.watch.reason()
                if missing is not None:
                    # `unterminated`: a convention we know, and its terminator never came — the stream was cut, and the
                    # bytes that arrived are what a reader gets (defect 66). `unverified`: no convention, so the capsule
                    # records that it does not know rather than claiming completion.
                    self.call.end_reason = missing
                    self.call.partial = self.call.ending == ENDING_UNTERMINATED
            if not completed and error is None:
                # Closed early — a `break`, a timeout, a cancelled consumer. The bytes that arrived are real; the
                # call did not finish, and a capsule must be able to tell those apart (D-023's `partial`, applied
                # to a stream the seam watched end to end).
                self.call.partial = True
            self.recorder.close_call(self.call, status=self.status, total_ms=self._elapsed_ms(), error=error)

        def close(self) -> None:
            # The application may abandon a stream (a `break`, a timeout, a `kill -9` never gets here). Closing early
            # is still a completed observation: the call is recorded with the bytes that actually arrived.
            close_inner = getattr(self.inner, "close", None)
            if close_inner is not None:
                close_inner()
            self._finish(completed=False)

    _TEE_STREAMS[module_name] = TeeStream
    return TeeStream


def async_tee_stream_class(module_name: str = "httpx") -> Any:
    """`AsyncByteStream` subclass for that client module, built lazily for the same reason as the sync one."""
    if module_name in _ASYNC_TEE_STREAMS:
        return _ASYNC_TEE_STREAMS[module_name]

    import importlib

    base_module = importlib.import_module(module_name)

    class AsyncTeeStream(base_module.AsyncByteStream):  # type: ignore[name-defined,misc]
        def __init__(self, inner: Any, recorder: Receiver, call: ModelCall, status: int, started: int) -> None:
            self.inner = inner
            self.recorder = recorder
            self.call = call
            self.status = status
            self.started = started
            self.done = False
            self.watch = TerminatorWatch(call.path)

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
                    self._finish(error=f"{type(exc).__name__}: {exc}", completed=False)
                    raise
                if chunk:
                    first_ms = self._elapsed_ms() if self.call.first_byte_ms is None else None
                    self.watch.feed(chunk)
                    self.recorder.receive(self.call, chunk, first_byte_ms=first_ms)
                yield chunk
            self._finish(completed=True)

        def _finish(self, error: str | None = None, completed: bool = True) -> None:
            if self.done:
                return
            self.done = True
            if completed and error is None and not _is_stream(self.call):
                # A body that is not an event stream has no provider terminator to look for, and httpx raises rather
                # than handing back a short read of one: reaching the end of the iteration means the body ended inside
                # its own framing. Looking for `[DONE]` in a JSON body made every ordinary call read as a cut stream
                # (defect 78) — the same mistake the proxy made in the other direction (defect 77).
                self.call.ending = None
                self.call.ending_provider = None
                self.call.end_reason = None
                self.call.partial = False
            elif completed and error is None:
                self.call.ending = self.watch.ending()
                self.call.ending_provider = self.watch.provider
                missing = self.watch.reason()
                if missing is not None:
                    # `unterminated`: a convention we know, and its terminator never came — the stream was cut, and the
                    # bytes that arrived are what a reader gets (defect 66). `unverified`: no convention, so the capsule
                    # records that it does not know rather than claiming completion.
                    self.call.end_reason = missing
                    self.call.partial = self.call.ending == ENDING_UNTERMINATED
            if not completed and error is None:
                # Closed early — a `break`, a timeout, a cancelled consumer. The bytes that arrived are real; the
                # call did not finish, and a capsule must be able to tell those apart (D-023's `partial`, applied
                # to a stream the seam watched end to end).
                self.call.partial = True
            self.recorder.close_call(self.call, status=self.status, total_ms=self._elapsed_ms(), error=error)

        async def aclose(self) -> None:
            aclose_inner = getattr(self.inner, "aclose", None)
            if aclose_inner is not None:
                await aclose_inner()
            self._finish(completed=False)

    _ASYNC_TEE_STREAMS[module_name] = AsyncTeeStream
    return AsyncTeeStream


def _is_stream(call: Any) -> bool:
    """Was this call a stream? The seam says so from the response's `content-type`; `wrap_stream` callers that do not
    say are treated as streams, which is the conservative reading (a terminator is looked for rather than assumed)."""
    return bool(getattr(call, "stream", True))


def wrap_stream(stream: Any, recorder: Receiver, call: ModelCall, status: int, started: int) -> Any:
    """Wrap `stream` so every chunk is recorded as it is yielded to the application.

    Two choices, both from the stream rather than from a guess: an async stream (`__aiter__`) needs the async tee, and
    either kind needs the tee whose base class matches the module that produced it, or the client's own
    `isinstance` assertion rejects the wrapper.
    """
    module_name = type(stream).__module__.split(".")[0]
    if module_name not in ("httpx", "httpx2"):         # a test double or an unknown client: any valid base will do
        available = http_clients.available()
        module_name = available[0][0] if available else "httpx"
    guarded = fail_open(recorder)
    if hasattr(stream, "__aiter__"):
        return async_tee_stream_class(module_name)(stream, guarded, call, status, started)
    return tee_stream_class(module_name)(stream, guarded, call, status, started)
