"""Interruption child (used by `tests/test_interruption_lab.py` and `bench/interruption_lab.py`). Not a test module.

Usage: python tests/_interruption_child.py <scenario> <data_dir>

Writes real frames with a real `TapeWriter` and then dies **without unwinding** (`os._exit` where the scenario wants a
clean kill, a signal where it wants the platform's own hardest death). The point is that nothing here is simulated at the
recovery layer: the tape the parent recovers is the tape a killed process left, which is the only kind of evidence the
diagnosis is supposed to be good at.

Scenarios, one per §14.4 row plus the two terminal cases:

* `mid-stream`       — request, two chunks, then death: the class that must say "cut", not "failed".
* `before-first-byte`— request, then death before any byte.
* `in-tool`          — a tool node opened and never closed: the side effect may have happened.
* `after-response`   — response recorded, then death while the app was handling it.
* `provider-error`   — an error frame is the terminal one; the node was still never closed.
* `clean`            — a run that reaches `RUN_END` and exits 0, so the diagnosis must find nothing interrupted.

A child that is killed by a signal cannot flush anything after the kill; that is the point. `PYTHONUNBUFFERED` is set by
the parent so the frames written before the kill are the frames the tape has.
"""
from __future__ import annotations

import os
import signal
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.tape.writer import TapeWriter

SCENARIOS = ("mid-stream", "before-first-byte", "in-tool", "after-response", "provider-error", "clean")


def die_hard() -> None:
    """Die the way a kill does: no `finally`, no flush, no `RUN_END`."""
    os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))
    os._exit(1)                                                  # only reached if the signal was ignored


def main(argv: list[str]) -> int:
    scenario, data_dir = argv[1], argv[2]
    writer = TapeWriter(data_dir, segment_bytes=C.MIN_SEGMENT_BYTES).open()
    writer.batch_bytes, writer.batch_ms = 0, 0.0
    run_id = "r_lab_" + scenario.replace("-", "_")

    def emit(kind: int, payload: dict | bytes, slot: int = 0) -> None:
        blob = payload if isinstance(payload, bytes) else cbor.encode(payload)
        writer.append(kind, blob, node_slot=slot, priority=2, run_slot=1)
        writer.publish()

    emit(C.KIND_RUN_START, {"run_id": run_id, "source": "lab"})
    emit(C.KIND_NODE_OPEN, {"name": "agent", "kind": "run"}, slot=1)
    emit(C.KIND_NODE_OPEN, {"name": "call-model", "kind": "step"}, slot=2)

    if scenario == "before-first-byte":
        emit(C.KIND_MODEL_REQUEST, {"provider": "anthropic", "model": "claude-x", "capture_seam": "sdk"}, slot=2)
        die_hard()

    if scenario == "mid-stream":
        emit(C.KIND_MODEL_REQUEST, {"provider": "anthropic", "model": "claude-x", "capture_seam": "sdk"}, slot=2)
        emit(C.KIND_MODEL_CHUNK, {"text": "the first part"}, slot=2)
        emit(C.KIND_MODEL_CHUNK, {"text": "the second part"}, slot=2)
        die_hard()

    if scenario == "in-tool":
        emit(C.KIND_NODE_CLOSE, {"name": "call-model", "kind": "step", "outcome": "ok"}, slot=2)
        emit(C.KIND_NODE_OPEN, {"name": "charge-card", "kind": "tool", "tool": "charge-card"}, slot=3)
        die_hard()

    if scenario == "after-response":
        emit(C.KIND_MODEL_REQUEST, {"provider": "anthropic", "capture_seam": "sdk"}, slot=2)
        emit(C.KIND_MODEL_CHUNK, {"text": "done"}, slot=2)
        emit(C.KIND_MODEL_RESPONSE, {"stop_reason": "end_turn"}, slot=2)
        die_hard()

    if scenario == "provider-error":
        emit(C.KIND_MODEL_REQUEST, {"provider": "openai", "capture_seam": "sdk"}, slot=2)
        emit(C.KIND_MODEL_ERROR, {"type": "RateLimitError", "error": "429"}, slot=2)
        die_hard()

    if scenario == "clean":
        emit(C.KIND_MODEL_REQUEST, {"provider": "anthropic", "capture_seam": "sdk"}, slot=2)
        emit(C.KIND_MODEL_RESPONSE, {"stop_reason": "end_turn"}, slot=2)
        emit(C.KIND_NODE_CLOSE, {"name": "call-model", "kind": "step", "outcome": "ok"}, slot=2)
        emit(C.KIND_NODE_CLOSE, {"name": "agent", "kind": "run", "outcome": "ok"}, slot=1)
        emit(C.KIND_RUN_END, {"run_id": run_id, "exit_code": 0})
        writer.close()
        return 0

    print(f"unknown scenario {scenario!r}; expected one of {', '.join(SCENARIOS)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
