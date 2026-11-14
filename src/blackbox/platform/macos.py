"""macOS specifics (P2-04): POSIX with three differences that matter to a mapped tape.

Written but **not run here** (this environment is Linux); the macOS CI cell is written and has never executed
(C7). Where macOS has no equivalent, the shim says so instead of pretending:

* **Preallocation.** `os.posix_fallocate` does not exist on Darwin. `fcntl(F_PREALLOCATE)` is the real call and
  is not exposed by Python's stdlib, so the shim reports `zero-fill` and `prealloc.preallocate` writes the
  blocks out. This is the safe direction: an APFS file that is sparse would put a page fault under the mapping,
  which is the `SIGBUS` class S17 exists to prevent.
* **Directory fsync.** `os.fsync` on a directory fd raises `EINVAL` on APFS/HFS+; the shim returns silently,
  which matches `posix.fsync_dir`'s best-effort contract.
* **`F_FULLFSYNC`.** `os.fsync` on macOS is `fsync(2)`: it returns once the data is in the *device's* cache,
  not on the media. `fcntl(F_FULLFSYNC)` is the call that forces it, and it **is** in the stdlib
  (`fcntl.F_FULLFSYNC`, Availability: macOS). `fdatasync` below issues it whenever the constant exists, and
  `capabilities()["f_fullfsync"]` / the durability note are derived from `f_fullfsync_supported()` rather than
  asserting anything about this interpreter.
"""
from __future__ import annotations

import os
import stat as stat_mod

try:                                    # `fcntl` does not exist on Windows, and this module must stay importable
    import fcntl
except ImportError:                 # pragma: no cover - exercised only on Windows
    fcntl = None                    # type: ignore[assignment]

NAME = "macos"
SUPPORTED = os.uname().sysname == "Darwin" if hasattr(os, "uname") else False
PREALLOC_METHOD = "zero-fill"          # F_PREALLOCATE is not reachable from stdlib
DIRECTORY_FSYNC = False                # fsync(dir) raises EINVAL on APFS/HFS+
MAPPED_VIEW_FLUSH = "mmap.flush"       # msync(MS_SYNC) via CPython
def f_fullfsync_supported() -> bool:
    """True when `fcntl.F_FULLFSYNC` can be issued from this interpreter (it usually cannot)."""
    return fcntl is not None and hasattr(fcntl, "F_FULLFSYNC")


def durability_note() -> str:
    """What `kernel`/`paranoid` can actually promise here, derived from the runtime, not asserted in prose."""
    if f_fullfsync_supported():
        return ("fdatasync issues fcntl(F_FULLFSYNC), which is the call that flushes the drive's cache on "
                "macOS; measured latency on a Mac is still unmeasured here")
    return ("fcntl.F_FULLFSYNC is not available in this interpreter, so fdatasync falls back to os.fsync, "
            "which does not flush the drive cache: F2 on macOS is weaker than on Linux and is labelled so")


def proc_start_ticks(pid: int) -> int | None:
    """No `/proc`. `sysctl(KERN_PROC_PID)` is not in stdlib, so liveness falls back to `pid_alive`."""
    return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # exists, owned by someone else
    return True


def boot_id() -> str | None:
    """`kern.boottime` is reachable via sysctl(3) only; `None` means "no boot identity available"."""
    return None


def fsync_dir(path: str) -> None:
    """Best effort: fsync on a directory fd is EINVAL on APFS/HFS+, and the plan already tolerates that."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


def fdatasync(fd: int) -> None:
    """The sync `kernel`/`paranoid` use on macOS.

    C20 asked for the confirmation: `os.fsync` on Darwin is `fsync(2)`, which returns once the data is in the
    *device's* cache, not on the platter/flash. `F_FULLFSYNC` is what forces it, and Python exposes it as
    `fcntl.F_FULLFSYNC`. So it is issued when the constant exists, and the fallback to plain `os.fsync` is
    recorded in `capabilities()["durability_note"]` rather than assumed.
    """
    if fcntl is not None and hasattr(fcntl, "F_FULLFSYNC"):
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def fs_type(path: str) -> str:
    """`statfs` is not in stdlib; report `unknown` rather than guess."""
    return "unknown"


def is_network_fs(path: str) -> bool:
    """Only `/Volumes/…` can be a network mount and not reliably so; the honest answer is "unknown"."""
    return os.path.abspath(path).startswith("/Volumes/")


def is_regular_file(path: str) -> bool:
    try:
        return stat_mod.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def pwrite(fd: int, buf, off: int) -> None:
    """Darwin has `os.pwrite`; this module simply keeps the call in the platform layer (C22)."""
    view = buf if isinstance(buf, (bytes, bytearray, memoryview)) else bytes(buf)
    os.pwrite(fd, view, off)


def pread(fd: int, n: int, off: int) -> bytes:
    return os.pread(fd, n, off)


def is_current() -> bool:
    import sys

    return sys.platform == "darwin"


def capabilities() -> dict[str, object]:
    return {"platform": NAME, "supported": SUPPORTED, "prealloc": PREALLOC_METHOD,
            "directory_fsync": DIRECTORY_FSYNC, "mapped_view_flush": MAPPED_VIEW_FLUSH,
            "process_start_token": False, "boot_id": False, "filesystem_type": "unknown",
            "f_fullfsync": f_fullfsync_supported(), "durability_note": durability_note(),
            "is_current": is_current(),
            "note": "written for P2-04; not executed in this environment (Linux only) and not yet run in CI"}
