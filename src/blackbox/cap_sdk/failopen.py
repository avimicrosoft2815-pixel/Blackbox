"""The one place a recorder is allowed to fail: `FailOpenReceiver` (P0, reported at `ee210ae`).

The report: with seam A installed, a receiver whose `receive()` raised took the host down — the exception came out of
`tee.py` and into the application's `for chunk in response.iter_bytes()`. That is the failure this whole layer exists
to make impossible, and the reason it survived is instructive: the seam's hook was inside a `try`, so the open-call
path was protected and the *per-chunk* path was not tested at all.

So the guard belongs in exactly one place, and the seams hand their receiver through it. Three rules, each with a
test in `tests/test_capture_failopen.py`:

1. **Only `Exception` is caught.** `BaseException` is left alone: `asyncio.CancelledError` and `KeyboardInterrupt` are
   not recorder failures, and swallowing them would hang a cancelled task or eat Ctrl-C.
2. **A failure is counted, never hidden.** `failure_count` is the honest number and `failures` is a bounded sample of
   `method:ExceptionType` strings. The exception *message* is deliberately discarded: a message can contain the very
   model output this layer is supposed to protect, and a degraded-capture note must never become a content leak.
3. **`open_call` still returns a usable `ModelCall`.** A call that cannot be recorded must not leave the tee with
   nothing to annotate, or the chunk path would raise the very `AttributeError` the guard is meant to prevent. The
   substitute is marked `partial=True` and carries an empty fingerprint: unknown, not zero, not "the same as an
   empty request".
"""
from __future__ import annotations

import inspect
from typing import Any

from blackbox.cap_sdk.base import ModelCall, Receiver

MAX_FAILURE_SAMPLES = 64       # enough to see the shape of a failure, bounded enough to survive a broken sink on
                                # every chunk of a long stream


def _accepts(method: Any, name: str) -> bool:
    """Whether `method` can be handed `name` as a keyword at all.

    D-053 added a `call_id` hint to `Receiver.open_call`. A receiver written before it has no such parameter, and
    handing it one would raise `TypeError` — which this wrapper would faithfully count as a recorder failure while
    the receiver silently stopped recording anything. Asking the signature once, up front, keeps such a receiver
    working: it records the call and the frame says `call_id: null`, which is true — no id reached it.
    """
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):                        # a C callable, or something exotic: assume the old shape
        return False
    if name in params:
        return params[name].kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class FailOpenReceiver(Receiver):
    """A receiver whose only promise is that its failures stop here."""

    def __init__(self, inner: Receiver) -> None:
        self.inner = inner
        self.failure_count = 0
        self.failures: list[str] = []
        self._carries_call_id = _accepts(inner.open_call, "call_id")

    # ── state the seams and the tests read through the wrapper ───────────────
    @property
    def installed_seam(self) -> str | None:
        return getattr(self.inner, "installed_seam", None)

    @installed_seam.setter
    def installed_seam(self, name: str | None) -> None:
        self.inner.installed_seam = name

    @property
    def calls(self) -> list[ModelCall]:
        return getattr(self.inner, "calls", [])

    # ── the three callbacks ─────────────────────────────────────────────────
    def open_call(self, provider: str, path: str, request: Any, *, model: str | None = None,
                  stream: bool = False, partial: bool = False, linked_run: str | None = None,
                  call_id: str | None = None) -> ModelCall:
        hints = {"call_id": call_id} if self._carries_call_id else {}
        try:
            return self.inner.open_call(provider, path, request, model=model, stream=stream, partial=partial,
                                        linked_run=linked_run, **hints)
        except Exception as exc:
            self._note("open_call", exc)
            # `request_fp=""` says *not fingerprinted*, which a reader can tell apart from a fingerprint of b"".
            return ModelCall(provider=provider, path=path, model=model, stream=stream, partial=True,
                             linked_run=linked_run, call_id=call_id)

    def receive(self, call: ModelCall, chunk: bytes, *, first_byte_ms: float | None = None) -> None:
        try:
            self.inner.receive(call, chunk, first_byte_ms=first_byte_ms)
        except Exception as exc:
            self._note("receive", exc)

    def close_call(self, call: ModelCall, *, status: int = 0, total_ms: float | None = None,
                   error: str | None = None) -> None:
        try:
            self.inner.close_call(call, status=status, total_ms=total_ms, error=error)
        except Exception as exc:
            self._note("close_call", exc)

    # ── bookkeeping ─────────────────────────────────────────────────────────
    def _note(self, where: str, exc: BaseException) -> None:
        self.failure_count += 1
        if len(self.failures) < MAX_FAILURE_SAMPLES:
            self.failures.append(f"{where}:{type(exc).__name__}")

    def as_dict(self) -> dict[str, Any]:
        """What a degraded-capture report may contain: counts and exception types, never messages."""
        return {"failures": self.failure_count, "sample": list(self.failures),
                "inner": type(self.inner).__name__}


def fail_open(recorder: Receiver) -> FailOpenReceiver | Receiver:
    """Wrap once. Idempotent, because seams wrap and so does the tee, and double-counting a failure is a small lie."""
    if isinstance(recorder, FailOpenReceiver):
        return recorder
    return FailOpenReceiver(recorder)
