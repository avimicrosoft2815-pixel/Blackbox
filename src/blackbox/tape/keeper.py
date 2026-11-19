"""C8 — the in-process keeper thread (plan §10.3).

The keeper is the only thread allowed to do time-based work: drain the commit window, and (from P1-09b/P3B on)
seal, retain, fold the inbox and evaluate triggers. It must never be able to hurt the app:

* every exception disables the keeper for the session, appends `KEEPER_DOWN`, and the writer keeps recording
  (fail-open, test `test_keeper_exception_is_fail_open`);
* `stop()` drains then publishes, so a clean shutdown cannot leave a frame unpublished;
* the tick is short and non-blocking; it takes no lock the app holds.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any

from blackbox import constants as C

TICK_S = 0.001          # 1 ms: finer than the 5 ms publish bound, cheap enough to ignore


class Keeper:
    def __init__(self, writer: Any, *, tick_s: float = TICK_S, blob_queue: Any = None,
                 blob_jobs_per_tick: int = 4, blob_writer: Any = None,
                 triggers: Any = None, on_trigger: Any = None) -> None:
        self.writer = writer
        self.tick_s = tick_s
        # C32/D-019: blob durability is the keeper's job in `kernel` mode, and the ordering is the invariant —
        # `sync_pending()` below runs before `publish_if_due()`, so the blob is durable before the tape sync that
        # covers the frame that references it.
        self.blob_writer = blob_writer
        # P1-15: compression is off the writer thread by construction — the app never touches this queue, and a
        # failure inside it is counted by the queue (the raw blob stays readable) instead of disabling the keeper.
        self.blob_queue = blob_queue
        self.blob_jobs_per_tick = blob_jobs_per_tick
        # §10.5: the trigger engine is *fed* by whoever sees the metric and *decided* here, in this thread — the app
        # thread only ever appends a sample (see `trigger.engine.Engine.feed`). `on_trigger` is what a freeze deadline
        # calls; without it a `freeze` still records the fire, it just has nobody to seal for, which is said in the
        # report rather than pretended away.
        self.triggers = triggers
        self.on_trigger = on_trigger
        self.trigger_events = 0
        self.ticks = 0
        self.publishes = 0
        self.disabled = False
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> Keeper:
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="blackbox-keeper", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        """Drain then publish (C3): a stopped keeper never leaves a frame in the window."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        if not self.disabled:
            if self.blob_writer is not None:      # blob before tape, on the way out too
                self.blob_writer.sync_pending()
            self.writer.flush()

    def __enter__(self) -> Keeper:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ── work ────────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.tick()
            except Exception as exc:
                self.disabled = True
                self.error = f"{type(exc).__name__}: {exc}"
                # best effort, and never re-raise
                with contextlib.suppress(Exception):
                    self.writer.degraded_reason = self.writer.degraded_reason or "keeper_down"
                return

    def tick(self) -> None:
        self.ticks += 1
        if self.blob_writer is not None:
            self.blob_writer.sync_pending()
        if self.writer.publish_if_due():
            self.publishes += 1
        if self.blob_queue is not None and len(self.blob_queue):
            self.blob_queue.drain(limit=self.blob_jobs_per_tick)
        if self.triggers is not None:
            self._run_triggers()

    def evaluate_triggers(self, *, force: bool = False) -> int:
        """Ask the engine to decide now. Forced only when the caller has just fed it something the tick cannot wait for
        (the launcher's `exit_code` at the end of a run); the keeper's own 1 ms tick never forces, so the engine's 100 ms
        evaluation interval is what actually paces it.
        """
        if self.triggers is None:
            return 0
        return self._run_triggers(force=force)

    def _run_triggers(self, *, force: bool = False) -> int:
        """Apply what the engine decided: MARK frames for every activity, the callback for a freeze deadline.

        A `TRIGGER` is a MARK whose `name` is `trigger` (the replay/report readers already know `MARK`), so this needs no
        new frame kind and no format change: a reader that does not know about triggers still shows the frame, with its
        payload, instead of tripping over an unknown kind number.
        """
        from blackbox.codec import cbor

        applied = 0
        for activity in self.triggers.tick(force=force) + self.triggers.due(force=force):
            payload = {"name": "trigger" if activity.kind == "fire" else f"trigger_{activity.kind}",
                       "rule": activity.rule, "metric": activity.metric, "value": activity.value,
                       "fingerprint": activity.fingerprint, "action": activity.action}
            if activity.detail:
                payload["detail"] = activity.detail
            self.writer.append(C.KIND_MARK, cbor.encode(payload), priority=2)
            self.trigger_events += 1
            applied += 1
            if activity.kind == "due" and self.on_trigger is not None:
                self.on_trigger(activity)
        return applied

    # ── introspection ───────────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        return {"ticks": self.ticks, "publishes": self.publishes, "disabled": self.disabled,
                "error": self.error, "tick_s": self.tick_s, "trigger_events": self.trigger_events,
                "triggers": self.triggers.stats() if self.triggers is not None else None,
                "blob_queue": self.blob_queue.stats() if self.blob_queue is not None else None,
                "blob_writer": self.blob_writer.stats() if self.blob_writer is not None else None,
                "window_max_ms": round(self.writer.window_max_ms, 3)}
