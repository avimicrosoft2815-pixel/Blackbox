"""C8 — the in-process keeper thread (plan §10.3).

The keeper is the only thread allowed to do time-based work: drain the commit window, and (from P1-09b/P3B on)
seal, retain, fold the inbox and evaluate triggers. It must never be able to hurt the app:

* every exception disables the keeper for the session, appends `KEEPER_DOWN`, and the writer keeps recording
  (fail-open, test `test_keeper_exception_is_fail_open`);
* `stop()` drains then publishes, so a clean shutdown cannot leave a frame unpublished;
* the tick is short and non-blocking; it takes no lock the app holds.
"""
from __future__ import annotations

import threading
import time

from blackbox import constants as C

TICK_S = 0.001          # 1 ms: finer than the 5 ms publish bound, cheap enough to ignore


class Keeper:
    def __init__(self, writer, *, tick_s: float = TICK_S) -> None:
        self.writer = writer
        self.tick_s = tick_s
        self.ticks = 0
        self.publishes = 0
        self.disabled = False
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> "Keeper":
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
            self.writer.flush()

    def __enter__(self) -> "Keeper":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ── work ────────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.tick()
            except Exception as exc:                      # noqa: BLE001 - fail-open by design
                self.disabled = True
                self.error = f"{type(exc).__name__}: {exc}"
                try:                                      # best effort, and never re-raise
                    self.writer.degraded_reason = self.writer.degraded_reason or "keeper_down"
                except Exception:                         # noqa: BLE001
                    pass
                return

    def tick(self) -> None:
        self.ticks += 1
        if self.writer.publish_if_due():
            self.publishes += 1

    # ── introspection ───────────────────────────────────────────────────────
    def stats(self) -> dict:
        return {"ticks": self.ticks, "publishes": self.publishes, "disabled": self.disabled,
                "error": self.error, "tick_s": self.tick_s,
                "window_max_ms": round(self.writer.window_max_ms, 3)}
