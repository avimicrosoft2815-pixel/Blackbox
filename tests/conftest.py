"""Shared test hygiene for three operating systems (C25).

On POSIX an open fd does not stop a directory from being deleted; on Windows it does (`WinError 32`), so a test
that leaks a writer makes the *next* test fail in teardown, which is a confusing way to learn about a leak. The
fixture below closes every `TapeWriter` a test leaves open, so a leak is reported as what it is.

It finds them by **exact type**, never by probing attributes: the first version called `getattr(obj, "_head", None)`
on every object the collector knows about, modules included, and a module whose PEP 562 `__getattr__` raises —
`anthropic.lib._extras` — turned that into a teardown error for nine unrelated tests on Windows (defect 25,
`tests/_hygiene.py`, `tests/test_gc_hygiene.py`).
"""
from __future__ import annotations

import contextlib
import gc
import os

import pytest
from _hygiene import leaked_writers


@pytest.fixture(autouse=True)
def close_leaked_handles() -> object:
    yield
    if os.name != "nt":
        return
    from blackbox.tape.writer import TapeWriter

    for writer in leaked_writers(gc.get_objects(), TapeWriter):
        with contextlib.suppress(Exception):
            writer.abort()


# ── a hung test names itself ────────────────────────────────────────────────────────────────────────────────────────
# The 3.12 cells spent fifteen minutes inside the unit step while 3.11 and 3.13 finished in three, and the job log of a
# running step is not readable from here: the cheapest evidence available was a guess. A test that overruns now prints a
# line in pytest's own `FAILED <id>` shape — which `tools/ci_summary.py` turns into an annotation, so the CLI shows it —
# dumps every thread's stack, and exits. It is here rather than in a plugin because `dev` is exactly two pinned test
# tools (asserted in `test_packaging.py`), and this needs nothing but the standard library.

TEST_TIMEOUT_S = float(os.environ.get("BLACKBOX_TEST_TIMEOUT", "120"))
# Captured at import time, before pytest swaps fd 1 for its capture file: the watchdog's line is the one piece of output
# in this suite that must reach the log no matter what the capture layer is doing.
_REAL_STDOUT = os.dup(1) if os.name != "nt" else None




@pytest.fixture(autouse=True)
def _fail_a_test_that_overruns(request: pytest.FixtureRequest) -> object:
    import faulthandler
    import sys
    import threading

    finished = threading.Event()

    def watch() -> None:
        if finished.wait(TEST_TIMEOUT_S):
            return
        # The real file descriptors, not `sys.stdout`: pytest has replaced those with capture objects, and `os._exit`
        # does not let a capture buffer reach the log. This is the one place where the output *must* survive.
        # The stack of the *main* thread, compacted: the dump below is complete but long, and the one line that names
        # the culprit should not depend on how much of the tail survives to the annotation.
        import traceback

        where = ""
        main = sys._current_frames().get(threading.main_thread().ident)
        if main is not None:
            frames = [frame for frame in traceback.extract_stack(main) if "pytest" not in frame.filename]
            if frames:
                tail = frames[-1]
                where = f" at {tail.filename}:{tail.lineno} in {tail.name}"
        line = (f"FAILED {request.node.nodeid} — ran longer than {TEST_TIMEOUT_S:.0f} s{where}\n").encode()
        with contextlib.suppress(OSError):
            os.write(_REAL_STDOUT if _REAL_STDOUT is not None else 1, line)
        faulthandler.dump_traceback(file=sys.__stderr__, all_threads=True)
        os._exit(1)

    watcher = threading.Thread(target=watch, name="test-watchdog", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        finished.set()
