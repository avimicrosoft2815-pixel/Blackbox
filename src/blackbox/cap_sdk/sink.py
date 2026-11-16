"""P3-06: the sink — where a chunk stops being an in-memory observation and becomes a frame on a tape.

The seams record; this writes. The gap between those two verbs is the whole product claim: an in-memory `ModelCall`
has the chunk counts right up until the process dies, at which point it is gone. So every chunk that arrives is
written to the tape *as it arrives*, through the writer's group commit, and a `kill -9` costs whatever was never
published rather than everything.

Coalescing, and why it is not cheating:

* A 2 000-token stream is 2 000 socket reads. One frame per read would be a tape twice the size of the payload and a
  recovery scan that spends its time on 60-byte frames. So chunks are grouped until 8 KiB or 100 ms, whichever comes
  first, and each group is one `MODEL_CHUNK` frame whose **payload is the raw bytes, unchanged**.
* The byte count therefore stays exact — the payloads concatenate back to the body, which is the test — and the unit
  of `chunks_received` is a chunk *frame*, not a socket read. The capsule says so: the sink's parameters and the two
  counts (`chunks_events`, `chunks_raw`) are in the response summary, and `capture_seam` and the coalescing
  parameters travel with the run.
* Nothing is buffered beyond one group: a sink that held the whole body would be the memory leak the seams avoid.

**Redaction happens here, before the write, not on the way out** (P3-11): the sink is handed a `redact.Redactor`
and what it frames is already clean. A secret that reaches the tape and is hidden later is a secret on the disk, in
the capsule, in the backup and in the support ticket. The summary records what was removed — counts by kind and two
byte totals — and never the bytes.

Two invariants this file must not break:

1. **Fail-open.** A sink that raises must not reach the application and must not stop the bytes. Every hook is
   wrapped, and a failed write is counted (`write_errors`) rather than raised.
2. **The tape owns the ordering.** `MODEL_REQUEST` before any chunk, `MODEL_RESPONSE` (or `MODEL_ERROR`) after the
   last one. Recovery reads the *last* kind per node to classify an interrupted call (`INTERRUPTED_MID_STREAM`), so
   the intermediate state has to look like exactly that: chunks and no response.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from blackbox import constants as C
from blackbox.cap_sdk.base import ModelCall, Receiver
from blackbox.redact import FAILED, Redactor

DEFAULT_COALESCE_BYTES = 8 * 1024
DEFAULT_COALESCE_MS = 100.0
CONTENT_MODES = ("full", "hash", "off")
ENV_CONTENT = "BLACKBOX_CONTENT"


def _content_of(payload: bytes) -> int | None:
    """The byte count a non-`full` chunk payload declares, or `None` when the payload *is* the content."""
    if not payload.startswith(b"[bbx-") or not payload.endswith(b"]"):
        return None
    marker = b" bytes="
    index = payload.rfind(marker)
    if index < 0:
        return None
    try:
        return int(payload[index + len(marker):-1])
    except ValueError:
        return None


def chunk_bytes(payload: bytes) -> int:
    """How many response bytes a `MODEL_CHUNK` payload stands for, whatever the content mode was.

    In `full` mode the payload is the body, so the answer is its length. In `hash` and `off` mode the payload is a
    header that *declares* the count (`[bbx-hash sha256=… bytes=N]`, `[bbx-off bytes=N]`), because a tape that
    cannot say how many bytes arrived is a tape that cannot answer "was the call cut short?".
    """
    declared = _content_of(payload)
    return len(payload) if declared is None else declared


class _DefaultPolicy:
    """Sentinel: "you did not choose a redaction policy, so use the documented default" (P3-11)."""

    def __repr__(self) -> str:                              # pragma: no cover - debugging aid
        return "<default redaction policy>"


@dataclass
class _Group:
    """The bytes waiting to become one frame, and the timing facts worth keeping about them."""

    chunks: list[bytes] = field(default_factory=list)
    bytes: int = 0
    raw_chunks: int = 0
    stored_chunks: int = 0
    stored_bytes: int = 0
    first_ns: int = 0
    last_ns: int = 0


class TapeSink:
    """Turns seam events into `MODEL_REQUEST` / `MODEL_CHUNK` / `MODEL_RESPONSE` frames.

    `emit` is the writer's own `emit(kind, payload, **fields)`; passing the callable rather than the writer keeps this
    testable without a filesystem and keeps the sink from reaching into the writer's state.
    """

    def __init__(self, emit: Callable[..., int], *, node_slot: int = 0,
                 coalesce_bytes: int = DEFAULT_COALESCE_BYTES, coalesce_ms: float = DEFAULT_COALESCE_MS,
                 clock: Callable[[], int] = time.monotonic_ns, redactor: Any = None,
                 content: str | None = None) -> None:
        self._emit = emit
        # `None` means "no redaction, and the caller said so" — it is not the default. The default is the
        # documented policy, `Redactor.from_config()`: full redaction unless `BLACKBOX_REDACTION` chooses otherwise.
        self.redactor = Redactor.from_config() if redactor is None else redactor
        # Content mode (Appendix A, §15.1): what the tape is allowed to keep of the model's own text. `None` means
        # "use the documented default", which is `BLACKBOX_CONTENT` or, unset, the plan's `hash`: a recorder whose
        # default keeps every token of every prompt is a recorder that quietly copies the customer's data.
        chosen = (content or os.environ.get(ENV_CONTENT) or "hash").strip().lower()
        if chosen not in CONTENT_MODES:
            raise ValueError(f"unknown content mode {chosen!r}; expected one of {CONTENT_MODES}")
        self.content = chosen
        self.node_slot = node_slot
        self.coalesce_bytes = coalesce_bytes
        self.coalesce_ms = coalesce_ms
        self._clock = clock
        self.groups: dict[int, _Group] = {}
        self.raw_chunks: dict[int, int] = {}
        self.redaction_failures = 0
        self.stored: dict[int, tuple[int, int]] = {}
        self.events_written = 0
        self.write_errors = 0
        self.bytes_written = 0

    # ── hooks the Recorder calls ────────────────────────────────────────────
    def on_open(self, call: ModelCall, seam: str | None) -> None:
        self._safe(C.KIND_MODEL_REQUEST, {
            "provider": call.provider, "path": call.path, "model": call.model,
            "request_fp": call.request_fp, "stream": call.stream, "partial": call.partial,
            "capture_seam": seam, "content": self.content, "linked_run": call.linked_run,
            "call_id": call.call_id,
            "coalesce_bytes": self.coalesce_bytes, "coalesce_ms": self.coalesce_ms,
        })
        self.groups[id(call)] = _Group(first_ns=self._clock())
        self.raw_chunks[id(call)] = 0
        self.stored[id(call)] = (0, 0)

    def on_chunk(self, call: ModelCall, chunk: bytes, *, first_byte_ms: float | None = None) -> None:
        group = self.groups.get(id(call))
        if group is None:                                  # a chunk after close: count it, do not invent a call
            self.write_errors += 1
            return
        # Counted before redaction, because it is a fact about the *call*: how many reads the application made. What
        # the tape stores is a different number — redaction merges and drops segments — and the two are reported
        # separately so neither can be read as a fingerprint of the other.
        self.raw_chunks[id(call)] = self.raw_chunks.get(id(call), 0) + 1
        if self.redactor is not None and self.content != "off":
            # `feed` returns only what it can prove is not the beginning of a secret, so the buffer it holds is
            # bounded by its window and the tape never sees a half-key. In `off` mode there is nothing to redact:
            # the tape keeps a byte count, so a redaction pass would be work whose result is discarded.
            try:
                chunk = self.redactor.feed(chunk)
            except Exception:
                # Fail closed, in the architecture's sense: the call keeps its place in the stream, the content does
                # not, and the number is in the summary. Dropping the segment silently would be a hole nobody sees.
                self.redaction_failures += 1
                chunk = FAILED
            if not chunk:
                return
        now = self._clock()
        group.chunks.append(chunk)
        group.bytes += len(chunk)
        group.raw_chunks += 1
        group.stored_chunks += 1
        group.stored_bytes += len(chunk)
        stored_count, stored_bytes = self.stored.get(id(call), (0, 0))
        self.stored[id(call)] = (stored_count + 1, stored_bytes + len(chunk))
        group.last_ns = now
        if first_byte_ms is not None:
            group.first_ns = now
        due_bytes = group.bytes >= self.coalesce_bytes
        due_ms = (now - group.first_ns) >= int(self.coalesce_ms * 1_000_000)
        if due_bytes or due_ms:
            self._flush(call, group, now)

    def on_close(self, call: ModelCall) -> None:
        group = self.groups.pop(id(call), None)
        if group is None:
            return
        if self.redactor is not None and self.content != "off":
            try:
                tail = self.redactor.flush()
            except Exception:
                self.redaction_failures += 1
                tail = FAILED
            if tail:
                group.chunks.append(tail)
                group.bytes += len(tail)
                stored_count, stored_bytes = self.stored.get(id(call), (0, 0))
                self.stored[id(call)] = (stored_count + 1, stored_bytes + len(tail))
        self._flush(call, group, self._clock())
        summary = {
            "provider": call.provider, "path": call.path, "model": call.model, "status": call.status,
            "chunks_events": call.chunks, "chunks_raw": self.raw_chunks.pop(id(call), 0),
            "chunks_stored": self.stored.get(id(call), (0, 0))[0],
            "bytes_stored": self.stored.get(id(call), (0, 0))[1],
            "bytes_received": call.bytes_received, "first_byte_ms": call.first_byte_ms, "total_ms": call.total_ms,
            "content": self.content,
            "response_fp": call.response_fp, "partial": call.partial, "error": call.error,
            "redaction": ({**self.redactor.as_dict(), "failures": self.redaction_failures}
                          if self.redactor is not None else
                          {"mode": "off", "counts": {}, "failures": 0, "note": "no redactor"}),
        }
        kind = C.KIND_MODEL_ERROR if call.error else C.KIND_MODEL_RESPONSE
        self._safe(kind, summary)

    # ── internals ───────────────────────────────────────────────────────────
    def _flush(self, call: ModelCall, group: _Group, now: int) -> None:
        if not group.chunks:
            return
        raw = b"".join(group.chunks)
        # In `full` mode the payload is the raw bytes and nothing else, so a reader concatenates the frames to get the
        # body back. In `hash` and `off` mode it is a bracketed header that declares the byte count and, for `hash`,
        # the digest: the tape still answers "how much arrived", and the model's text is not on it at all.
        if self.content == "full":
            payload = raw
        elif self.content == "hash":
            digest = hashlib.sha256(raw).hexdigest().encode()
            payload = b"[bbx-hash sha256=" + digest + b" bytes=" + str(len(raw)).encode() + b"]"
        else:
            payload = b"[bbx-off bytes=" + str(len(raw)).encode() + b"]"
        self._safe(C.KIND_MODEL_CHUNK, payload, mono_ns=group.last_ns or now)
        self.bytes_written += len(payload)
        group.chunks.clear()
        group.bytes = 0
        group.first_ns = now

    def _safe(self, kind: int, payload: Any, **fields: Any) -> None:
        try:
            body = payload if isinstance(payload, bytes) else json.dumps(payload, sort_keys=True).encode()
            self._emit(kind, body, node_slot=self.node_slot, **fields)
            self.events_written += 1
        except Exception:
            self.write_errors += 1                          # counted, never raised (fail-open)


class SinkRecorder(Receiver):
    """A `Recorder` that also forwards to a sink, in order, and never lets the sink break the call."""

    def __init__(self, sink: Any = None) -> None:
        from blackbox.cap_sdk.base import Recorder

        self._recorder = Recorder()
        self.sink = sink
        self.installed_seam: str | None = None
        self.sink_errors = 0

    @property
    def calls(self) -> list[ModelCall]:
        return self._recorder.calls

    def open_call(self, provider: str, path: str, request: Any, *, model: str | None = None,
                  stream: bool = False, partial: bool = False, linked_run: str | None = None,
                  call_id: str | None = None) -> ModelCall:
        call = self._recorder.open_call(provider, path, request, model=model, stream=stream, partial=partial,
                                        linked_run=linked_run, call_id=call_id)
        self._forward("on_open", call, self.installed_seam)
        return call

    def receive(self, call: ModelCall, chunk: bytes, *, first_byte_ms: float | None = None) -> None:
        self._recorder.receive(call, chunk, first_byte_ms=first_byte_ms)
        self._forward("on_chunk", call, chunk, first_byte_ms=first_byte_ms)

    def close_call(self, call: ModelCall, *, status: int = 0, total_ms: float | None = None,
                   error: str | None = None) -> None:
        self._recorder.close_call(call, status=status, total_ms=total_ms, error=error)
        self._forward("on_close", call)

    def _forward(self, name: str, *args: Any, **kwargs: Any) -> None:
        if self.sink is None:
            return
        try:
            getattr(self.sink, name)(*args, **kwargs)
        except Exception:
            self.sink_errors += 1                           # the app never sees a recorder error (fail-open)
