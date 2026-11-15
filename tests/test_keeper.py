"""C8/§10.3 — the keeper thread publishes on the window bound and can never hurt the app."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.tape.keeper import Keeper
from blackbox.tape.writer import TapeWriter


def _writer(tmp_path, **kw):
    return TapeWriter(str(tmp_path), segment_bytes=1 << 20, mode="process", **kw).open()


def test_low_rate_event_is_published_within_the_window(tmp_path):
    w = _writer(tmp_path)
    # C40. The window is measured from the *last publish*, not from `flush()`: a flush with nothing pending and a
    # head already published is a no-op, so the reference point was `open()`. On a cold Windows runner more than
    # 200 ms can pass between `open()` and `emit()` (1 MiB preallocation, a scanning antivirus), the window is
    # already expired, `emit` correctly publishes inline, the keeper publishes nothing, and this test failed with
    # `0 >= 1` in the py3.13/windows cell of run 43678767989. A publish here sets the reference point to "now" on
    # every OS, which is what the assertion is about: the keeper drains a window the app left open.
    w.batch_ms = 200.0
    w.publish()                      # the window starts here, whatever the machine did before it
    k = Keeper(w).start()
    try:
        w.emit(C.KIND_MARK, b"x" * 64)
        deadline = time.monotonic() + 2.0
        while w._pending and time.monotonic() < deadline:
            time.sleep(0.002)
        assert w._pending == 0, "keeper did not drain the window"
        assert k.publishes >= 1
    finally:
        k.stop()
        w.close()


def test_a_window_older_than_the_bound_publishes_inline(tmp_path):
    """The other half of C40, stated instead of assumed: an *expired* window must not hold a frame.

    If the last publish is older than `batch_ms`, `emit` publishes in the app thread and the keeper has nothing to
    do. That is the behaviour the flaky Windows cell was actually seeing, and it is correct - a window that old is
    exactly what the bound exists to prevent.
    """
    w = _writer(tmp_path)
    w.batch_ms = 5.0
    w.publish()
    k = Keeper(w).start()
    try:
        time.sleep(0.02)             # 4x the window: the next event is already due
        w.emit(C.KIND_MARK, b"x" * 64)
        assert w._pending == 0, "an expired window must publish the frame inline"
        assert k.publishes == 0, "the keeper was not needed and must not be credited"
    finally:
        k.stop()
        w.close()


def test_priority_event_bypasses_the_window(tmp_path):
    w = _writer(tmp_path)
    w.batch_ms = 60_000.0            # a window no test would ever wait for
    w.emit(C.KIND_DEGRADED, b"")
    assert w._pending == 0, "priority kinds must publish immediately"
    w.close()


def test_stop_publishes_a_pending_frame(tmp_path):
    w = _writer(tmp_path)
    w.batch_ms = 60_000.0
    k = Keeper(w).start()
    w.emit(C.KIND_MARK, b"y")
    assert w._pending == 1
    k.stop()
    assert w._pending == 0
    w.close()


def test_keeper_exception_is_fail_open(tmp_path):
    w = _writer(tmp_path)
    w.batch_ms = 0.0

    class Boom(Keeper):
        def tick(self):
            raise RuntimeError("keeper exploded")

    k = Boom(w, tick_s=0.001).start()
    deadline = time.monotonic() + 0.5
    while not k.disabled and time.monotonic() < deadline:
        time.sleep(0.002)
    assert k.disabled and "keeper exploded" in k.error
    k.stop()
    # the app is unaffected: recording still works after the keeper died
    seq = w.emit(C.KIND_MARK, b"after")
    assert seq > 0
    w.close()


def test_window_max_is_measured_not_promised(tmp_path):
    w = _writer(tmp_path)
    w.batch_ms = 50_000.0
    w.emit(C.KIND_MARK, b"z")
    assert 0.0 <= w.window_ms() < 50_000.0
    w.publish()
    assert w.window_ms() == 0.0
    w.close()


def test_keeper_and_app_thread_never_roll_twice(tmp_path):
    """Defect found by bench_writer: keeper publish raced the app thread and created segment n+1 twice."""
    import threading

    w = TapeWriter(str(tmp_path), segment_bytes=64 << 10, mode="process").open()
    w.batch_bytes = 1024
    w.batch_ms = 1.0
    k = Keeper(w, tick_s=0.0002).start()
    errors: list[BaseException] = []
    payload = b"z" * 200

    def app() -> None:
        try:
            for _ in range(4_000):
                w.emit(C.KIND_MODEL_REQUEST, payload)
        except BaseException as exc:          # the point is to surface it
            errors.append(exc)

    t = threading.Thread(target=app)
    t.start()
    t.join(60)
    k.stop()
    w.flush()
    assert not errors, errors
    assert w.publishes > 0 and w.head.active_segment >= 2      # it did roll, repeatedly
    w.close()
