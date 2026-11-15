"""Seam C (P3-05c): wrap the SDK's own public methods. The last resort, and the only one that survives a client we
never see.

Seams A and B work below the SDK, on `httpx`. That covers every client built with httpx — which is most of them — and
none of the ones it is not, or the ones a library builds and hides before we are installed. This seam goes to the
other end of the call: `openai`'s `chat.completions.create` and `embeddings.create`, `anthropic`'s `messages.create`,
sync and async. A library that swallows our transport patching still has to call its own method.

What it costs is precision, and the cost is recorded rather than hidden:

* **It counts events, not bytes.** A parsed stream event is not a wire chunk: the SDK has decoded SSE, so the byte
  count it could report would be the size of the SDK's objects, not of anything that travelled. `bytes_received`
  stays 0 and every call this seam records is `partial=True`, which is the marker for *this seam saw the call but not
  all of it* (D-022). Any claim about `bytes_received` must come from a seam that watched a socket.
* **The path is a label.** `provider`/`path` are `openai`/`/v1/chat/completions` from the table below, not a URL
  observed on the way past. A client pointed at a proxy or a regional endpoint will still say that, because the seam
  cannot know better.
* **It patches a class, so it needs an SDK version whose shape it recognises.** Every target is looked up by module,
  class and attribute; anything missing is skipped and listed in the handle's `detail["skipped"]`, never guessed at.
  An unknown version means this seam does not install, and the preflight records that instead of the recorder
  pretending.

Everything is inside a `try/except` on the recording side: a seam that raises into the application has failed at its
one job. The call itself is never retried, reissued or replayed (F-rule: side effects belong to the application).
"""
from __future__ import annotations

import functools
import inspect
from typing import Any

from blackbox.cap_sdk.base import Receiver, SeamHandle, SeamUnavailable

NAME = "sdk-methods"

# module, class, method, provider, path label. Both sync and async resources are listed explicitly: patching one and
# not the other is exactly the kind of half-coverage that reads as "supported" in a table and is not.
TARGETS: tuple[tuple[str, str, str, str, str], ...] = (
    ("openai.resources.chat.completions", "Completions", "create", "openai", "/v1/chat/completions"),
    ("openai.resources.chat.completions", "AsyncCompletions", "create", "openai", "/v1/chat/completions"),
    ("openai.resources.embeddings", "Embeddings", "create", "openai", "/v1/embeddings"),
    ("openai.resources.embeddings", "AsyncEmbeddings", "create", "openai", "/v1/embeddings"),
    ("openai.resources.responses", "Responses", "create", "openai", "/v1/responses"),
    ("anthropic.resources.messages", "Messages", "create", "anthropic", "/v1/messages"),
    ("anthropic.resources.messages", "AsyncMessages", "create", "anthropic", "/v1/messages"),
)

# Client plumbing that is not part of the request the model sees. Kept out of the fingerprint so that the same call
# over a different timeout hashes the same.
PLUMBING = frozenset({"timeout", "extra_headers", "extra_body", "extra_query", "extra_json"})


def probe() -> tuple[bool, str]:
    """True when at least one SDK is importable. The reason names the versions, because that is what a bug report
    needs and because an unknown version is the main way this seam fails."""
    found = []
    for sdk in ("openai", "anthropic"):
        try:
            module = __import__(sdk)
        except Exception:
            continue
        found.append(f"{sdk} {getattr(module, '__version__', 'unknown')}")
    if not found:
        return False, "neither openai nor anthropic is importable"
    return True, "importable: " + ", ".join(found)


def _jsonable(value: Any, depth: int = 0) -> Any:
    """A stable, printable form of a request parameter. Unknown objects become their repr, which is what a fingerprint
    of an SDK argument can honestly be without importing the SDK's model classes."""
    if depth > 6:
        return "<deep>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, depth + 1) for v in value]
    return repr(value)


def _request_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: _jsonable(v) for k, v in kwargs.items() if k not in PLUMBING}


class _StreamCounter:
    """A stream wrapper that counts the events the application actually consumed, sync or async.

    Delegation by `__getattr__`, because both SDKs hand back their own stream class with methods that matter to the
    caller (`get_final_completion`, `.response`, `.close`); wrapping only what we need and forwarding the rest keeps
    the SDK's behaviour intact.
    """

    def __init__(self, inner: Any, close: Any) -> None:
        self._inner = inner
        self._close = close

    def __iter__(self) -> Any:
        def iterate() -> Any:
            try:
                for event in self._inner:
                    self._close(event)
                    yield event
            finally:
                self._close(None)
        return iterate()

    def __aiter__(self) -> Any:
        async def iterate() -> Any:
            try:
                async for event in self._inner:
                    self._close(event)
                    yield event
            finally:
                self._close(None)
        return iterate()

    def __enter__(self) -> Any:
        enter = getattr(self._inner, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc: Any) -> Any:
        exit_ = getattr(self._inner, "__exit__", None)
        return None if exit_ is None else exit_(*exc)

    async def __aenter__(self) -> Any:
        aenter = getattr(self._inner, "__aenter__", None)
        if aenter is not None:
            await aenter()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        aexit = getattr(self._inner, "__aexit__", None)
        return None if aexit is None else await aexit(*exc)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


def _is_async(original: Any) -> bool:
    """`AsyncCompletions.create` is not a coroutine function — it is a *sync* decorator around one, so
    `iscoroutinefunction` says False and calling it returns a coroutine. Wrapping it as sync hands the caller a
    `_StreamCounter` where an awaitable was expected; the async check has to unwrap the decorator first."""
    return inspect.iscoroutinefunction(inspect.unwrap(original))


def _wrap(original: Any, recorder: Receiver, provider: str, path: str) -> Any:
    if _is_async(original):
        @functools.wraps(original)
        async def create_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            call, stream = _open(recorder, provider, path, kwargs)
            try:
                result = await original(self, *args, **kwargs)
            except Exception as exc:
                _close(recorder, call, error=f"{type(exc).__name__}: {exc}")
                raise
            if stream:
                return _count(recorder, call, result)
            _finish_unary(recorder, call, result)
            return result

        return create_async

    @functools.wraps(original)
    def create_sync(self: Any, *args: Any, **kwargs: Any) -> Any:
        call, stream = _open(recorder, provider, path, kwargs)
        try:
            result = original(self, *args, **kwargs)
        except Exception as exc:
            _close(recorder, call, error=f"{type(exc).__name__}: {exc}")
            raise
        if stream:
            return _count(recorder, call, result)
        _finish_unary(recorder, call, result)
        return result

    return create_sync


def _open(recorder: Receiver, provider: str, path: str, kwargs: dict[str, Any]) -> tuple[Any, bool]:
    model = kwargs.get("model")
    call = recorder.open_call(provider, path, _request_payload(kwargs),
                              model=model if isinstance(model, str) else (str(model) if model is not None else None),
                              stream=bool(kwargs.get("stream")),
                              partial=True)         # events, not bytes: see the module docstring
    return call, call.stream


def _count(recorder: Receiver, call: Any, result: Any) -> _StreamCounter:
    def close(event: Any) -> None:
        if event is not None:
            recorder.receive(call, b"")      # one event: the unit this seam can honestly count
            return
        _close(recorder, call)

    return _StreamCounter(result, close)


def _finish_unary(recorder: Receiver, call: Any, result: Any) -> None:
    recorder.receive(call, b"")
    _close(recorder, call)


def _close(recorder: Receiver, call: Any, error: str | None = None) -> None:
    recorder.close_call(call, status=0 if error else 200, error=error)


def install(recorder: Receiver, *, targets: tuple[tuple[str, str, str, str, str], ...] = TARGETS) -> SeamHandle:
    """Patch every target that exists. Anything that does not is skipped, with the import or attribute error."""
    import importlib

    patched: list[tuple[Any, str, Any]] = []
    skipped: list[str] = []
    for module_name, class_name, method, provider, path in targets:
        try:
            module = importlib.import_module(module_name)
            cls = getattr(module, class_name)
            original = getattr(cls, method)
        except Exception as exc:
            skipped.append(f"{module_name}.{class_name}.{method}: {type(exc).__name__}")
            continue
        setattr(cls, method, _wrap(original, recorder, provider, path))
        patched.append((cls, method, original))

    if not patched:
        raise SeamUnavailable("no known SDK method was found to patch: " + "; ".join(skipped or ["no SDK importable"]))

    def undo() -> None:
        for cls, method, original in patched:
            setattr(cls, method, original)

    return SeamHandle(name=NAME, undo=undo,
                      detail={"patched": [f"{c.__module__}.{c.__name__}.{m}" for c, m, _ in patched],
                              "skipped": skipped, "units": "parsed stream events, not wire bytes"})
