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
* **No positional I/O.** Windows has no `os.pwrite`/`os.pread`, so `pwrite`/`pread` below are `os.lseek` plus
  `os.write`/`os.read`. That is legal here and only here, for one stated reason: every fd the tape opens has a
  single user (the writer, or one reader), and the writer serialises publish/roll/close under its own lock, so
  no second thread can move the file offset between the seek and the transfer. A shared fd would make this
  wrong, and the docstring is the warning.
* **No mapping anywhere.** Since ADR-005/C21 the recorder and its readers use `pread`/`pwrite` only, so
  `FlushViewOfFile` is no longer on any path. `flush_mapped_view` remains for a caller that maps a sealed
  segment itself (a capsule tool), where `mmap.flush()` is the right call.
* **PID reuse token.** `OpenProcess` + `GetProcessTimes` are reached through `ctypes` (C20), because they are
  the Windows equivalent of `/proc/<pid>/stat` field 22: the process *creation* time, which is what defeats PID
  reuse. `PROCESS_QUERY_LIMITED_INFORMATION` keeps the call working for a process owned by another user where
  the token allows it. If `ctypes` or `kernel32` is unavailable the shim returns `None`, and the lease falls
  back to a mtime staleness check rather than trusting a bare PID (fail-safe direction).
* **Filesystem type.** No `/proc`. The shim reports `unknown`, which the network-filesystem check treats as
  "not known to be local" only when the caller asks explicitly.
"""
from __future__ import annotations

import os
import stat as stat_mod
from typing import Any

NAME = "windows"
SUPPORTED = os.name == "nt"
PREALLOC_METHOD = "zero-fill"          # never `truncate`: that would make the segment sparse
DIRECTORY_FSYNC = False
MAPPED_VIEW_FLUSH = "mmap.flush"       # FlushViewOfFile + FlushFileBuffers, via CPython


# ── the lease's start-time check (P2-03, C20) ────────────────────────────────────────────────────────────
# `GetProcessTimes` returns 100 ns units since 1601-01-01; the caller only needs monotonic identity, so the raw
# value is passed through and never compared against a wall clock.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
FILETIME_TICKS_PER_SECOND = 10_000_000


def _kernel32() -> tuple:
    """The loaded `kernel32`, or `None` when ctypes/the DLL is not there (then the caller must not guess)."""
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    if not hasattr(_kernel32, "_cached"):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)      # type: ignore[attr-defined]
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _kernel32._cached = (k32, FILETIME)      # type: ignore[attr-defined]
    cached: tuple = _kernel32._cached            # type: ignore[attr-defined]
    return cached


def proc_start_ticks(pid: int) -> int | None:
    """Process creation time in 100 ns ticks via `OpenProcess` + `GetProcessTimes`, or `None`.

    `None` means "no token available", which the lease must treat as *cannot prove liveness* — never as "dead"
    and never as "same process". The unit test on Linux feeds a fake `kernel32` through `ctypes`, so the
    argument marshalling and the handle lifetime are exercised even though Windows is not available here.
    """
    if pid <= 0:
        return None
    try:
        k32, filetime = _kernel32()
    except (OSError, ImportError, AttributeError):
        return None
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel_t, user_t = filetime(), filetime(), filetime(), filetime()
        if not k32.GetProcessTimes(handle, created, exited, kernel_t, user_t):
            return None
        ticks: int = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks
    finally:
        k32.CloseHandle(handle)


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


_BOOTID_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"


def _winreg() -> Any:
    import winreg

    return winreg


def boot_id() -> str | None:
    """A per-boot identity, from the registry's `BootId` counter (C26).

    `HKLM\\...\\PrefetchParameters\\BootId` is a DWORD that Windows increments at every boot, so two runs on
    the same machine have different values — that is exactly what the lease's "did the machine reboot under me"
    check needs. When the value cannot be read, the fallback is a boot *time* derived from
    `GetTickCount64` (uptime), rounded to the minute: labelled a heuristic, because two reboots inside the same
    minute would look alike. `None` is returned only when neither is available, and the lease treats that as
    "cannot prove a reboot", never as "same boot".
    """
    try:
        winreg = _winreg()
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BOOTID_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "BootId")
        return f"winreg-bootid-{int(value)}"
    except (OSError, ImportError, ValueError):
        pass
    try:
        import ctypes

        uptime_ms = ctypes.windll.kernel32.GetTickCount64()        # type: ignore[attr-defined]
        import time

        boot_epoch_s = int(time.time() - uptime_ms / 1000)
        return f"heuristic-boot-minute-{boot_epoch_s // 60}"
    except (OSError, ImportError, AttributeError):
        return None


def fsync_dir(path: str) -> None:
    """No-op by design: Windows cannot fsync a directory from Python, and NTFS journals the rename."""
    return None


def fdatasync(fd: int) -> None:
    """Windows has no `fdatasync`; `FlushFileBuffers` (what `os.fsync` calls) flushes the file's data."""
    os.fsync(fd)


def pwrite(fd: int, buf, off: int) -> None:
    """`pwrite` on Windows: seek, then write. See the module docstring for why the offset is safe to move."""
    view = memoryview(buf)
    os.lseek(fd, off, os.SEEK_SET)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def pread(fd: int, n: int, off: int) -> bytes:
    """`pread` on Windows: seek, then read exactly `n` bytes unless the file ends first."""
    os.lseek(fd, off, os.SEEK_SET)
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def is_current() -> bool:
    return os.name == "nt"


def flush_mapped_view(mm) -> None:
    """`FlushViewOfFile`, for a caller that still holds a mapping: CPython's `mmap.flush()` does exactly that.

    With ADR-005 the recorder has no mapping, so `kernel`/`paranoid` reach the device through `fdatasync` above
    and this exists for readers (`blackbox verify`, capsules) that map a sealed segment to hash it.
    """
    mm.flush()


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
            "process_start_token": True, "boot_id": True, "filesystem_type": "unknown",
            "is_current": is_current(),
            "note": ("this machine's shim" if is_current()
                     else "not executed in this environment: written for another platform")}
