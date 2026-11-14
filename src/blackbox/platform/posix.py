"""POSIX specifics: process start token, boot id, directory fsync, filesystem detection (P2-02).

No psutil, no third-party code. Windows equivalents live in `blackbox.platform.windows` (later phase);
every caller must tolerate `NotImplementedError` on an unsupported platform.
"""
from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path

NAME = "posix"

_NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "fuse.sshfs", "fuse.rclone", "afs"}


def proc_start_ticks(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat (clock ticks since boot) — the PID-reuse token."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    close = data.rfind(")")
    if close < 0:
        return None
    fields = data[close + 2:].split()
    try:
        return int(fields[19])       # field 22 overall, 20th after the comm field
    except (IndexError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def boot_id() -> str | None:
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip()
    except OSError:
        return None


def fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fdatasync(fd: int) -> None:
    if hasattr(os, "fdatasync"):
        os.fdatasync(fd)
    else:  # pragma: no cover - Windows
        os.fsync(fd)


def fs_type(path: str) -> str:
    """Filesystem type of `path` from /proc/self/mountinfo (longest matching mount point)."""
    real = os.path.realpath(path)
    best, best_len = "unknown", -1
    try:
        with open("/proc/self/mountinfo") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                parts = left.split()
                if len(parts) < 5:
                    continue
                mount_point = parts[4].replace("\\040", " ")
                fstype = right.split()[0] if right else "unknown"
                if (real == mount_point or real.startswith(mount_point.rstrip("/") + "/")) \
                        and len(mount_point) > best_len:
                    best, best_len = fstype, len(mount_point)
    except OSError:
        return "unknown"
    return best


def is_network_fs(path: str) -> bool:
    return fs_type(path) in _NETWORK_FS


def is_regular_file(path: str) -> bool:
    try:
        return stat_mod.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def pwrite(fd: int, buf, off: int) -> None:
    """Positional write. The only route to `os.pwrite` in this repository (C22)."""
    view = buf if isinstance(buf, (bytes, bytearray, memoryview)) else bytes(buf)
    os.pwrite(fd, view, off)


def pread(fd: int, n: int, off: int) -> bytes:
    """Positional read, the counterpart of `pwrite`."""
    return os.pread(fd, n, off)


def is_current() -> bool:
    """True when this shim is the one `platform.current()` would return on this machine."""
    import sys

    return os.name != "nt" and sys.platform != "darwin"


def capabilities() -> dict[str, object]:
    """What this shim promises, in the same shape as `windows.capabilities()` / `macos.capabilities()`."""
    return {"platform": NAME, "supported": True, "prealloc": "posix_fallocate",
            "directory_fsync": True, "mapped_view_flush": "mmap.flush",
            "process_start_token": Path("/proc/self/stat").exists(), "boot_id": True,
            "filesystem_type": "/proc/self/mountinfo", "is_current": is_current(),
            "note": "the shim every number in this repository was measured through"}
