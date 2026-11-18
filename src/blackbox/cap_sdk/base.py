"""The contract every seam is written against (P3-05d/P3-07).

A seam's only job is to turn wire bytes into `ModelCall` records: request fingerprint, response fingerprint, the
stream chunk-by-chunk as it actually arrives, and timings. It may not parse a provider's semantics beyond what the
record needs, and it may not hold a copy of a response body after the call ends — the tape has it, and a second copy
in memory is how a recorder becomes a memory leak with good intentions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


class SeamUnavailable(RuntimeError):
    """The seam cannot be installed on this machine, with the reason in the message."""


@dataclass
class ModelCall:
    """One model call, as the seam sees it. `request_fp`/`response_fp` are stable across whitespace and key order."""

    provider: str
    path: str
    request_fp: str = ""
    response_fp: str = ""
    status: int = 0
    chunks: int = 0
    bytes_received: int = 0
    first_byte_ms: float | None = None
    total_ms: float | None = None
    stream: bool = False
    error: str | None = None
    model: str | None = None
    partial: bool = False         # the seam saw the call but not all of it; never a guess, always a marker
    end_reason: str | None = None  # why `partial` is set, in words: a short stream must say how it was short
    linked_run: str | None = None  # `x-blackbox-run` seen on the request: the run this call belongs to (P3B-04)
    call_id: str | None = None     # this call's own id, sent as `x-blackbox-call` when a proxy is in front (D-053)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "path": self.path, "request_fp": self.request_fp,
            "response_fp": self.response_fp, "status": self.status, "chunks": self.chunks,
            "bytes_received": self.bytes_received, "first_byte_ms": self.first_byte_ms,
            "total_ms": self.total_ms, "stream": self.stream, "error": self.error, "model": self.model,
            "partial": self.partial, "end_reason": self.end_reason,
            "linked_run": self.linked_run, "call_id": self.call_id,
        }


def fingerprint(payload: Any) -> str:
    """A hash that survives dict ordering and whitespace — the thing a "same request?" question needs."""
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    elif isinstance(payload, str):
        raw = payload.encode("utf-8", "replace")
    else:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.blake2b(raw, digest_size=16).hexdigest()


class Receiver:
    """The interface a seam calls. A seam depends on this, not on `Recorder`, so a test can pass a stub."""

    installed_seam: str | None = None

    def open_call(self, provider: str, path: str, request: Any, *, model: str | None = None,
                  stream: bool = False, partial: bool = False, linked_run: str | None = None,
                  call_id: str | None = None) -> ModelCall:
        """Open a call. The hints are not decoration: a sink writes the request frame *here*, so a seam that set
        `call.model` afterwards would leave a durable frame saying `model: null` for a call that named its model."""
        raise NotImplementedError

    def receive(self, call: ModelCall, chunk: bytes, *, first_byte_ms: float | None = None) -> None:
        raise NotImplementedError

    def close_call(self, call: ModelCall, *, status: int = 0, total_ms: float | None = None,
                   error: str | None = None) -> None:
        raise NotImplementedError


class Recorder(Receiver):
    """The default receiver. Everything is fail-open: an exception here must never reach the application."""

    def __init__(self, sink: Any = None) -> None:
        self.sink = sink
        self.calls: list[ModelCall] = []
        self.installed_seam: str | None = None

    def open_call(self, provider: str, path: str, request: Any, *, model: str | None = None,
                  stream: bool = False, partial: bool = False, linked_run: str | None = None,
                  call_id: str | None = None) -> ModelCall:
        call = ModelCall(provider=provider, path=path, request_fp=fingerprint(request),
                         model=model, stream=stream, partial=partial, linked_run=linked_run, call_id=call_id)
        self.calls.append(call)
        return call

    def receive(self, call: ModelCall, chunk: bytes, *, first_byte_ms: float | None = None) -> None:
        call.chunks += 1
        call.bytes_received += len(chunk)
        if call.first_byte_ms is None and first_byte_ms is not None:
            call.first_byte_ms = first_byte_ms

    def close_call(self, call: ModelCall, *, status: int = 0, total_ms: float | None = None,
                   error: str | None = None) -> None:
        call.status = status
        call.total_ms = total_ms
        call.error = error
        call.response_fp = fingerprint(f"{call.chunks}:{call.bytes_received}:{status}:{error or ''}")


@dataclass
class SeamHandle:
    """Returned by `install`; `uninstall` runs the seam's own undo and is safe to call twice."""

    name: str
    undo: Any = None                      # callable() -> None supplied by the seam; failures are swallowed
    installed: bool = True
    restored: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def uninstall(self) -> None:
        if self.restored:
            return
        self.restored = True
        self.installed = False
        if self.undo is not None:
            import contextlib

            with contextlib.suppress(Exception):
                self.undo()
