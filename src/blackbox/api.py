"""What a process imports to be recorded (`blackbox.api`), and the stall dump that goes with it (P2-13/P3-01).

Two jobs live here, and they are deliberately in one file because they have the same constraint: **nothing in this
module may raise into user code, and nothing may cost a measurable amount when recording is off.**

Lifecycle (P3-01)
    `install()` opens the tape and starts the keeper, and is refcounted and idempotent: two `install()` calls make
    one writer and one `RUN_START`, and the second `uninstall()` — not the first — closes it. The state lives behind
    one `threading.RLock`, so installing from two threads at once cannot produce two writers, which would mean two
    `RUN_START` frames in one tape directory.

    `uninstall()` while a call is in flight does not break that call: an acquisition (`recording()`) registers in
    `_inflight`, and `uninstall()` that finds one defers the close to the last exit. Closing a tape under a frame
    that is half-written would be the worst possible moment to be tidy.

    `BLACKBOX_DISABLED=1` (or `BLACKBOX_HOME` pointing at an unwritable place) makes `install()` return
    `{"enabled": False}` without creating anything; `emit()` is then a no-op that returns 0. This is the fail-open
    path in its cheapest form — the decorators check `enabled()` first, so a disabled process pays one attribute
    read per event.

Stall dump (P2-13)
    On POSIX `install()` also registers `faulthandler` on `SIGUSR1` pointed at `stall_tb.txt` beside `stall.json`,
    so when the launcher's watchdog decides the process has stopped committing, the *process itself* writes every
    thread's stack. A watchdog outside the process could never do that without ptrace. Windows has no `SIGUSR1` and
    `faulthandler.register` only accepts a few signals there, so `install()` records `stacks=False` and the launcher
    reports `stall.json` only.
"""
from __future__ import annotations

import contextlib
import faulthandler
import os
import signal
import sys
import threading
from typing import Any

from blackbox import constants as C

STALL_TXT = "stall_tb.txt"
TRUTHY = {"1", "true", "yes", "on"}



class _Lifecycle:
    """One process-wide recorder. Everything mutable is behind `lock`."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.refs = 0
        self.inflight = 0
        self.writer: Any = None
        self.keeper: Any = None
        self.data_dir: str | None = None
        self.disabled_reason: str | None = None
        self.runs_started = 0
        self.owns_run = False        # True when *this* process wrote RUN_START and therefore owes a RUN_END
        self.closed = 0
        self._stall: dict[str, Any] = {"installed": False, "signal": None, "path": None, "stacks": False,
                                       "reason": None}
        self._stall_handle: Any = None

    # ── state ───────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "enabled": self.writer is not None,
                "refs": self.refs,
                "inflight": self.inflight,
                "data_dir": self.data_dir,
                "disabled_reason": self.disabled_reason,
                "runs_started": self.runs_started,
                "closed": self.closed,
                "writer_uuid": None if self.writer is None else self.writer.writer_uuid.hex(),
                "stall": dict(self._stall),
            }


_STATE = _Lifecycle()


# ── install / uninstall / shutdown ──────────────────────────────────────────


def disabled() -> bool:
    return os.environ.get("BLACKBOX_DISABLED", "").strip().lower() in TRUTHY


def enabled() -> bool:
    """The fast path every decorator takes: one attribute read, no lock, no exception."""
    return _STATE.writer is not None


def install(*, data_dir: str | None = None, signal_number: int | None = None,
            mode: str = "process", keeper: bool = True, **writer_kw: Any) -> dict[str, Any]:
    """Open the tape for this process. Refcounted, idempotent, thread-safe, never raises."""
    with _STATE.lock:
        _install_stall_dump(data_dir, signal_number)
        if _STATE.writer is not None:
            _STATE.refs += 1
            return _STATE.status()
        if disabled():
            _STATE.disabled_reason = "BLACKBOX_DISABLED is set"
            return _STATE.status()
        try:
            from blackbox.tape.keeper import Keeper
            from blackbox.tape.writer import TapeWriter

            root = os.path.abspath(data_dir or os.environ.get("BLACKBOX_HOME") or ".blackbox")
            # Defect 26: on Windows `os.makedirs` under a path that is a *file* did not raise, so the writer opened
            # and 100 calls ran against a tape directory that cannot exist. Asking the question directly is
            # platform-independent: an existing non-directory is refused here, by name, instead of being left to
            # whatever the filesystem's `mkdir` does with it.
            if os.path.exists(root) and not os.path.isdir(root):
                raise NotADirectoryError(f"data directory {root!r} exists and is not a directory")
            writer = TapeWriter(root, mode=mode, **writer_kw).open()
            _STATE.writer = writer
            _STATE.data_dir = root
            # Under `blackbox run` the launcher sets BLACKBOX_RUN_ID in the child's environment. The obvious reading
            # of that is "the launcher owns the run boundary, so the SDK writes no RUN_START" — and that reading is
            # what made `blackbox run -- python app.py` leave two tapes that nothing joined: `correlate` reads a
            # tape's run id from its own RUN_START, so a child tape without one resolves to `None` and cannot link to
            # the launcher's run by run id (P3B-03's "one run with linked nodes", measured 2026-11-16). The SDK's tape
            # is a *different* tape from the launcher's, so naming the run in it is not a second RUN_START in the
            # launcher's tape — it is this tape saying which run it belongs to, which is exactly what §11.10 links on.
            # `attached` distinguishes the two cases for a reader; `owns_run` is per tape, so this tape also closes
            # itself with RUN_END instead of leaving recovery to classify a finished process as interrupted.
            from blackbox.codec import cbor

            attached_run = (os.environ.get("BLACKBOX_RUN_ID") or "").strip() or None
            writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": attached_run or writer.writer_uuid.hex(),
                                                       "argv": None, "pid": os.getpid(), "source": "sdk",
                                                       "attached": attached_run is not None}))
            writer.flush()
            _STATE.runs_started += 1
            _STATE.owns_run = True
            if keeper:
                _STATE.keeper = Keeper(writer).start()
        except Exception as exc:                      # fail-open: the app keeps running, unrecorded and told why
            _STATE.writer = None
            _STATE.keeper = None
            _STATE.disabled_reason = f"{type(exc).__name__}: {exc}"
            return _STATE.status()
        _STATE.refs = 1
        return _STATE.status()


def uninstall() -> dict[str, Any]:
    """Drop one reference. The tape closes when the last reference goes *and* nothing is in flight."""
    with _STATE.lock:
        if _STATE.refs > 0:
            _STATE.refs -= 1
        if _STATE.refs == 0 and _STATE.writer is not None and _STATE.inflight == 0:
            _close_locked()
        return _STATE.status()


def shutdown() -> dict[str, Any]:
    """Close now, whatever the count. Idempotent; an in-flight append is allowed to finish first."""
    with _STATE.lock:
        _STATE.refs = 0
        if _STATE.writer is not None and _STATE.inflight == 0:
            _close_locked()
        return _STATE.status()


def _close_locked() -> None:
    keeper, writer = _STATE.keeper, _STATE.writer
    owns_run = _STATE.owns_run
    _STATE.keeper = None
    _STATE.writer = None                                  # `enabled()` is False from here, before the slow close
    _STATE.owns_run = False
    _STATE.closed += 1
    if keeper is not None:
        with contextlib.suppress(Exception):
            keeper.stop()
    if writer is not None:
        if owns_run:
            with contextlib.suppress(Exception):
                from blackbox.codec import cbor

                writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": writer.writer_uuid.hex(),
                                                         "exit_code": None, "signaled": False, "stalled": False}))
                writer.flush()
        with contextlib.suppress(Exception):
            writer.close()


@contextlib.contextmanager
def recording() -> Any:
    """Hold the tape open for the duration of one call, so `uninstall()` cannot close it underneath."""
    with _STATE.lock:
        _STATE.inflight += 1
        writer = _STATE.writer
    try:
        yield writer
    finally:
        with _STATE.lock:
            _STATE.inflight -= 1
            if _STATE.refs == 0 and _STATE.inflight == 0 and _STATE.writer is not None:
                _close_locked()


def emit(kind: int, payload: bytes = b"", **kw: Any) -> int:
    """Append one frame if recording is on. Returns the seq (0 when nothing was written); never raises."""
    if _STATE.writer is None:
        return 0
    with recording() as writer:
        if writer is None:
            return 0
        try:
            return int(writer.emit(kind, payload, **kw))
        except Exception:                             # a recorder that raises is worse than one that misses a frame
            return 0


# ── stall dump (P2-13) ──────────────────────────────────────────────────────


def _install_stall_dump(data_dir: str | None, signal_number: int | None) -> None:
    if _STATE._stall["installed"] or _STATE._stall["reason"]:
        return                                        # one registration per process, and no retry loop
    path = stall_dump_path(data_dir)
    if not hasattr(signal, "SIGUSR1"):
        _STATE._stall.update({"installed": False, "path": path, "stacks": False,
                              "reason": "SIGUSR1 does not exist on this platform: stall.json only"})
        return
    sig = signal.SIGUSR1 if signal_number is None else signal_number
    try:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        handle = open(path, "a", encoding="utf-8")     # faulthandler owns this handle for the life of the process
        faulthandler.register(sig, file=handle, all_threads=True, chain=False)
    except (OSError, RuntimeError, ValueError) as exc:
        _STATE._stall.update({"installed": False, "path": path, "stacks": False,
                              "reason": f"{type(exc).__name__}: {exc}"})
        return
    _STATE._stall_handle = handle
    _STATE._stall.update({"installed": True, "signal": int(sig), "path": path, "stacks": True, "reason": None})


def stall_dump_path(data_dir: str | None = None) -> str:
    root = os.path.abspath(data_dir or os.environ.get("BLACKBOX_HOME") or ".")
    return os.path.join(root, STALL_TXT)


def stall_status() -> dict[str, Any]:
    return dict(_STATE._stall)


if __name__ == "__main__":                       # pragma: no cover - manual probe
    print(install(), file=sys.stderr)


# ── P3-02: decorators ───────────────────────────────────────────────────────
#
# The disabled path is the contract: check `enabled()` and call straight through. No node is allocated, no frame is
# encoded, no clock is read. `test_api_decorators.py` measures the overhead of a disabled decorated call against the
# undecorated function and fails past 1 µs.


def _wrap(kind: str, fn: Any, name: str | None) -> Any:
    import functools
    import inspect

    from blackbox.rec import context as node_ctx

    label = str(name or getattr(fn, "__qualname__", getattr(fn, "__name__", "call")))

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_inner(*args: Any, **kwargs: Any) -> Any:
            if not enabled():
                return await fn(*args, **kwargs)
            node = node_ctx.open_node(label, kind)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                node_ctx.close_node(node, "error", exc)
                raise
            node_ctx.close_node(node, "ok")
            return result

        return async_inner

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> Any:
        if not enabled():
            return fn(*args, **kwargs)
        node = node_ctx.open_node(label, kind)
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            node_ctx.close_node(node, "error", exc)
            raise
        node_ctx.close_node(node, "ok")
        return result

    return inner


def _decorator(kind: str) -> Any:
    def factory(fn: Any = None, *, name: str | None = None) -> Any:
        if fn is None:
            return lambda f: _wrap(kind, f, name)
        return _wrap(kind, fn, name)

    factory.__name__ = kind
    factory.__doc__ = f"Record one `{kind}` node around the decorated callable (P3-02)."
    return factory


workflow = _decorator("workflow")
agent = _decorator("agent")
tool = _decorator("tool")
retrieval = _decorator("retrieval")


# ── P3-03: context managers, marks, captures ────────────────────────────────


@contextlib.contextmanager
def _node(name: str, kind: str, **fields: Any) -> Any:
    from blackbox.rec import context as node_ctx

    if not enabled():
        yield None
        return
    node = node_ctx.open_node(name, kind, **fields)
    try:
        yield node
    except BaseException as exc:
        node_ctx.close_node(node, "error", exc)
        raise
    node_ctx.close_node(node, "ok")


def run(name: str = "run", **fields: Any) -> Any:
    """`with blackbox.run("ingest"):` — the outermost node of a recorded unit of work (P3-03)."""
    return _node(name, "run", **fields)


class _NodeHandle:
    """One node, usable as a context manager *and* as a decorator — `with step(...)` and `@step(...)`."""

    def __init__(self, name: str, kind: str, fields: dict[str, Any]) -> None:
        self.name = name
        self.kind = kind
        self.fields = fields
        self._cm: Any = None

    def __enter__(self) -> Any:
        self._cm = _node(self.name, self.kind, **self.fields)
        return self._cm.__enter__()

    def __exit__(self, *exc: Any) -> Any:
        return self._cm.__exit__(*exc)

    def __call__(self, fn: Any = None, *, name: str | None = None) -> Any:
        if fn is None:
            return lambda f: _wrap(self.kind, f, name or self.name)
        return _wrap(self.kind, fn, name or self.name)


def step(name: Any = None, **fields: Any) -> Any:
    """A step node: `with blackbox.step("fetch"):` or `@blackbox.step("fetch")` or `@blackbox.step` (P3-02/P3-03)."""
    if callable(name):
        return _wrap("step", name, None)
    return _NodeHandle(str(name or "step"), "step", dict(fields))


def capture(name: str = "capture", **fields: Any) -> Any:
    """A node that exists to carry fields the reader will want beside it (P3-03)."""
    return _node(name, "capture", **fields)


def mark(name: str, **fields: Any) -> int:
    from blackbox.rec import context as node_ctx

    return node_ctx.mark(name, **fields)


def handled_error(error: BaseException, **fields: Any) -> int:
    from blackbox.rec import context as node_ctx

    return node_ctx.handled_error(error, **fields)


def current() -> Any:
    from blackbox.rec import context as node_ctx

    return node_ctx.current()


def freeze(reason: str = "manual", **fields: Any) -> int:
    """Ask for a snapshot of this run (P3-03/§10.5). The keeper does the work; the call only records the request."""
    from blackbox.rec import context as node_ctx

    return node_ctx.mark("freeze", reason=reason, **fields)
