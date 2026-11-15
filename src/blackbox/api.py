"""What a process imports to be recorded (`blackbox.api`), and the stall dump that goes with it (P2-13).

`install()` is deliberately tiny and dependency-free: on POSIX it registers `faulthandler` on `SIGUSR1` and points it
at `stall_tb.txt` beside the tape's `stall.json`, so when the launcher's watchdog decides the process has stopped
committing, the *process itself* writes every thread's stack. A watchdog running in the launcher could never do that
from outside — `faulthandler.register` is the only supported way to get stacks out of a running interpreter without
ptrace, and it is in the standard library.

Windows has no `SIGUSR1` and `faulthandler.register` only accepts `SIGABRT`/`SIGFPE`/`SIGILL`/`SIGSEGV`/`SIGTERM`
there, so `install()` records `stacks=False` and the launcher reports `stall.json` only. The plan allows that
asymmetry; it does not allow claiming stacks that were never written.

Nothing here raises into user code: an `install()` that cannot register returns a dict saying why.
"""
from __future__ import annotations

import contextlib
import faulthandler
import os
import signal
import sys
from typing import Any

STALL_TXT = "stall_tb.txt"
_registered: dict[str, Any] = {"installed": False, "signal": None, "path": None, "stacks": False, "reason": None}


def stall_dump_path(data_dir: str | None = None) -> str:
    root = os.path.abspath(data_dir or os.environ.get("BLACKBOX_HOME") or ".")
    return os.path.join(root, STALL_TXT)


def install(*, data_dir: str | None = None, signal_number: int | None = None) -> dict[str, Any]:
    """Register the stall stack dump. Idempotent; never raises."""
    if _registered["installed"]:
        return dict(_registered)
    path = stall_dump_path(data_dir)
    if not hasattr(signal, "SIGUSR1"):
        _registered.update({"installed": False, "path": path, "stacks": False,
                            "reason": "SIGUSR1 does not exist on this platform: stall.json only"})
        return dict(_registered)
    sig = signal.SIGUSR1 if signal_number is None else signal_number
    try:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        handle = open(path, "a", encoding="utf-8")   # the faulthandler owns this handle for the process
        faulthandler.register(sig, file=handle, all_threads=True, chain=False)
    except (OSError, RuntimeError, ValueError) as exc:
        _registered.update({"installed": False, "path": path, "stacks": False,
                            "reason": f"{type(exc).__name__}: {exc}"})
        return dict(_registered)
    _registered.update({"installed": True, "signal": int(sig), "path": path, "stacks": True, "reason": None})
    return dict(_registered)


def uninstall() -> None:
    if _registered.get("installed") and hasattr(signal, "SIGUSR1"):
        with contextlib.suppress(RuntimeError, ValueError):
            faulthandler.unregister(_registered["signal"])
    _registered.update({"installed": False, "stacks": False})


def stall_status() -> dict[str, Any]:
    return dict(_registered)


if __name__ == "__main__":                       # pragma: no cover - manual probe
    print(install(), file=sys.stderr)
