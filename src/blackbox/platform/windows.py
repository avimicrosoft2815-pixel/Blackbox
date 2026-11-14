"""Windows specifics (P2-03): the same six questions `posix` answers, answered without POSIX.

This module is **written but not run here** — this environment is Linux only, and the Windows CI cell has
never executed (C7). Every function either works on Windows, or fails in a documented, typed way that the
caller already tolerates (the plan's rule: a platform shim may raise `NotImplementedError`, and the recorder
must degrade rather than crash the user's program).

What differs on Windows, and why each difference is safe:

* **Preallocation.** There is no `posix_fallocate`. `os.ftruncate` creates a *sparse* file, which is exactly
  what the tape must never map (a fault on an unallocated page under `ENOSPC` is an access violation that
  kills the process). So the shim reports `zero-fill` and `prealloc.preallocate` writes the blocks out.
* **Directory fsync.** Windows has no handle-based directory flush from Python. `fsync_dir` is a no-op with a
  reason: `MoveFileEx`/`os.replace` on NTFS is metadata-journalled, and the plan already treats directory
  fsync as best-effort everywhere (`posix.fsync_dir` is wrapped in `contextlib.suppress(OSError)`).
* **Mapped-view flush.** `mmap.flush()` calls `FlushViewOfFile` + `FlushFileBuffers` on CPython/Windows, which
  is what `kernel` mode needs; there is no separate call to make.
* **PID reuse token.** `CreateToolhelp32Snapshot` is not reachable from stdlib. The shim returns `None`, and
  the lease code treats "no token" as "cannot prove liveness", i.e. it falls back to a mtime-based staleness
  check instead of trusting the PID (fail-safe direction).
* **Filesystem type.** No `/proc`. The shim reports `unknown`, which the network-filesystem check treats as
  "not known to be local" only when the caller asks explicitly.
"""
from __future__ import annotations

import os
import stat as stat_mod

NAME = "windows"
SUPPORTED = os.name == "nt"
PREALLOC_METHOD = "zero-fill"          # never `truncate`: that would make the segment sparse
DIRECTORY_FSYNC = False
MAPPED_VIEW_FLUSH = "mmap.flush"       # FlushViewOfFile + FlushFileBuffers, via CPython


def proc_start_ticks(pid: int) -> int | None:
    """No stdlib way to read a process creation time on Windows: `None` means "cannot prove liveness"."""
    return None


def pid_alive(pid: int) -> bool:
    """Best-effort liveness. `OpenProcess` is not in stdlib; `os.kill(pid, 0)` is not a liveness probe here."""
    if pid <= 0:
        return False
    try:                                # CTRL_C_EVENT-style no-op is not available; tasklist is the fallback
        import subprocess

        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],   # noqa: S603,S607
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return True                     # cannot prove it is dead → assume alive (fail-safe for the lease)
    return str(pid) in out.stdout


def boot_id() -> str | None:
    """No `/proc/sys/kernel/random/boot_id`; a caller that needs a boot identity must supply one."""
    return None


def fsync_dir(path: str) -> None:
    """No-op by design: Windows cannot fsync a directory from Python, and NTFS journals the rename."""
    return None


def fdatasync(fd: int) -> None:
    """Windows has no `fdatasync`; `FlushFileBuffers` (what `os.fsync` calls) flushes data and metadata."""
    os.fsync(fd)


def fs_type(path: str) -> str:
    """No `/proc`: report `unknown` rather than guess from an extension or a drive letter."""
    return "unknown"


def is_network_fs(path: str) -> bool:
    """UNC paths (`\\\\server\\share`) are network paths; a mapped drive cannot be told apart from a local one."""
    return os.path.abspath(path).startswith("\\\\")


def is_regular_file(path: str) -> bool:
    try:
        return stat_mod.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def capabilities() -> dict[str, object]:
    """What a reviewer (and `blackbox doctor`) should see, so nothing here is implied silently."""
    return {"platform": NAME, "supported": SUPPORTED, "prealloc": PREALLOC_METHOD,
            "directory_fsync": DIRECTORY_FSYNC, "mapped_view_flush": MAPPED_VIEW_FLUSH,
            "process_start_token": False, "boot_id": False, "filesystem_type": "unknown",
            "tested_here": False,
            "note": "written for P2-03; not executed in this environment (Linux only) and not yet run in CI"}
