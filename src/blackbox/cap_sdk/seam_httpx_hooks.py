"""Seam A (P3-05a): an `httpx` response *event hook*. The least invasive seam, and the one the preflight tries first.

The hook runs after the response headers have arrived and **before the body has been read** — which is exactly the
window a recorder needs. There is still an unread stream sitting on the response, so the hook can wrap it with the
same tee seam B uses (`tee.py`) and get every chunk as the application consumes it, with no transport patching at
all. That is why this seam is first: it costs one wrapped `__init__` and nothing else.

What it records:

* the request (fingerprint of body, model if it is in the JSON body, and the URL path),
* the response status and the time it took — from before the request went out (`first_byte_ms` is time to the first
  chunk actually read, `total_ms` to the last),
* every chunk as it passes, its byte count, and the time to first byte.

The one thing it cannot see is a response whose stream is already closed when the hook runs (`response.is_closed`,
e.g. a hook added after the body was read, or a response replayed from another seam). Then it records the call with
the body it can see and marks it `partial` — "this call happened, and this seam did not see all of it". A reader of
the tape must never have to guess whether a zero means *nothing arrived* or *nothing was visible*.

`install(recorder, hook_name="response")` patches `httpx.Client.__init__`/`AsyncClient.__init__`, so clients created
*after* install are captured; a client created before it is not, which the preflight reports instead of pretending.

Everything is inside a `try/except`: a capture seam that raises into the application has failed at its one job.
"""
from __future__ import annotations

import time
from typing import Any

from blackbox.cap_sdk.base import ModelCall, Receiver, SeamHandle, SeamUnavailable
from blackbox.cap_sdk.tee import wrap_stream

NAME = "httpx-hooks"


def probe() -> tuple[bool, str]:
    try:
        import httpx
    except Exception as exc:                     # pragma: no cover - the module is absent in a stdlib-only env
        return False, f"httpx is not importable: {type(exc).__name__}"
    if not hasattr(httpx.Client, "__init__"):    # pragma: no cover - belt and braces
        return False, "httpx.Client has no __init__ to wrap"
    return True, "httpx is importable; the response hook can wrap the unread stream"


def _model_of(body: bytes) -> str | None:
    import json

    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if isinstance(parsed, dict):
        model = parsed.get("model")
        if isinstance(model, str):
            return model
    return None


def _patch_init(cls: Any, on_request: Any, on_response: Any, hook_name: str) -> Any:
    """Wrap `cls.__init__` to add two event hooks. Returns the original, for `undo`."""
    original = cls.__init__

    def patched(self: Any, *args: Any, **kwargs: Any) -> None:
        hooks = dict(kwargs.get("event_hooks") or {})
        hooks["request"] = [*list(hooks.get("request") or []), on_request]
        hooks[hook_name] = [*list(hooks.get(hook_name) or []), on_response]
        kwargs["event_hooks"] = hooks
        original(self, *args, **kwargs)

    patched.__doc__ = original.__doc__
    cls.__init__ = patched
    return original


def _restore_init(cls: Any, original: Any) -> None:
    cls.__init__ = original


def install(recorder: Receiver, *, hook_name: str = "response") -> SeamHandle:
    """Patch `httpx.Client`/`AsyncClient` so every request and response is recorded.

    Two hooks, not one: a *request* hook stamps the clock before the request is sent, and the response hook takes the
    measurement from there. With only the response hook, `first_byte_ms` would be measured from the moment the
    headers arrived — an artificially small number, and the whole point of the timing rows is that they are true.
    Timings are therefore *end-to-end*, and `first_byte_ms` means "until the first chunk was actually read".

    Patching `__init__` covers clients created *after* install; a client created before it is not captured, which
    the preflight reports instead of pretending. `uninstall` puts the original `__init__` back.
    """
    ok, reason = probe()
    if not ok:
        raise SeamUnavailable(reason)

    import httpx

    starts: dict[int, int] = {}

    def on_request(request: Any) -> None:
        try:
            if len(starts) > 256:                     # a request that never got a response must not leak
                for stale in list(starts)[: len(starts) - 256]:
                    starts.pop(stale, None)
            starts[id(request)] = time.perf_counter_ns()
        except Exception:
            return

    def on_response(response: Any) -> None:
        try:
            request = response.request
            started = starts.pop(id(request), time.perf_counter_ns())
            body = request.content or b""
            call: ModelCall = recorder.open_call(request.url.host or "unknown", request.url.path or "/", body)
            call.model = _model_of(body)
            status = int(response.status_code)
            if response.is_closed:
                # The body is already gone (replayed, or a hook added late). Record the sighting and say so — a zero
                # here must never be mistaken for "the model returned nothing".
                call.partial = True
                recorder.close_call(call, status=status, total_ms=_elapsed_ms(response))
                return
            call.stream = "text/event-stream" in response.headers.get("content-type", "")
            response.stream = wrap_stream(response.stream, recorder, call, status, started)
        except Exception:
            return                                # fail-open: the application never sees a recorder error

    # `AsyncClient` **awaits** its hooks (`await hook(response)`), so handing it the sync functions raises
    # `TypeError: object NoneType can't be used in 'await' expression` on the first async call — the seam would have
    # broken every async client it was installed on. Async wrappers, not the same functions.
    async def on_request_async(request: Any) -> None:
        on_request(request)

    async def on_response_async(response: Any) -> None:
        on_response(response)

    original_sync = _patch_init(httpx.Client, on_request, on_response, hook_name)
    original_async = _patch_init(httpx.AsyncClient, on_request_async, on_response_async, hook_name)

    def undo() -> None:
        starts.clear()
        _restore_init(httpx.Client, original_sync)
        _restore_init(httpx.AsyncClient, original_async)

    return SeamHandle(name=NAME, undo=undo,
                      detail={"patched": ["httpx.Client.__init__", "httpx.AsyncClient.__init__"], "form": "init"})


def _elapsed_ms(response: Any) -> float | None:
    """httpx's own measurement, available only after the body has been read or the response closed."""
    try:
        return float(response.elapsed.total_seconds() * 1000.0)
    except Exception:
        return None
