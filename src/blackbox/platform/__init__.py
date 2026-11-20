"""Platform shims: `posix`, `windows` (P2-03) and `macos` (P2-04).

Each module answers the same questions and publishes a `capabilities()` dict, so nothing about a platform is
implied silently. `current()` picks one by `os.name`/`sys.platform`; an unsupported platform gets `posix` only
if it looks like POSIX, otherwise `NotImplementedError` from the shim itself (the recorder degrades, it does
not crash the user's program).
"""
from __future__ import annotations

import os
import sys
import time
from types import ModuleType


def current() -> ModuleType:
    if os.name == "nt":
        from blackbox.platform import windows

        return windows
    if sys.platform == "darwin":
        from blackbox.platform import macos

        return macos
    from blackbox.platform import posix

    return posix


def _sharing_violation(exc: OSError) -> bool:
    """The Windows answer to "somebody else still has this file": WinError 5, 32 or 33 (and `EEXIST`, harmlessly)."""
    if isinstance(exc, FileExistsError):
        return True
    return getattr(exc, "winerror", None) in (5, 32, 33)


def replace(src: str, dst: str, *, budget_s: float = 2.0) -> None:
    """`os.replace`, with the retry that turns a Windows race into a wait instead of a failure.

    CPython opens files without `FILE_SHARE_DELETE`, so renaming *onto* a file another process has open is refused
    outright while that handle lives — and a recovery state file is read by every other recoverer that is racing for the
    same run. On POSIX the rename is atomic and this loop runs once. If the peer never lets go, the refusal is coded
    (`BBX-E210`, "writer is alive; recovery refused") rather than a traceback: "somebody else is finishing this run" is
    an ordinary thing for two recoverers to discover and not a bug in the caller (defect 105).
    """
    deadline = time.monotonic() + budget_s
    delay = 0.002
    while True:
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if not _sharing_violation(exc):
                raise
            if time.monotonic() > deadline:
                from blackbox.errors import bbx

                raise bbx("BBX-E210", f"{dst} is held by another process that is finishing this run") from exc
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


def read(path: str, *, budget_s: float = 2.0) -> bytes:
    """Read a whole file, waiting out the Windows window in which a peer's rename makes the open fail.

    The other half of the race `replace` handles (defects 105 and 106). A rename onto `path` — the way every recovery
    marker and every capsule is published — briefly refuses readers on Windows with `ERROR_ACCESS_DENIED` (which arrives
    as `PermissionError`, `errno` 13, and names `path`, not a source/destination pair). On POSIX the reader sees the old
    file or the new one and nothing else, so this loop runs once there. If the window never closes the last refusal is
    raised: a caller that cannot read its state has to decide, and this shim does not decide for it.
    """
    deadline = time.monotonic() + budget_s
    delay = 0.002
    while True:
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError as exc:
            if not _sharing_violation(exc):
                raise
            if time.monotonic() > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


def capabilities() -> dict[str, object]:
    mod = current()
    if hasattr(mod, "capabilities"):
        caps: dict[str, object] = mod.capabilities()
        return caps
    return {"platform": mod.__name__.rsplit(".", 1)[-1], "prealloc": "posix_fallocate",
            "directory_fsync": True, "process_start_token": True, "boot_id": True,
            "filesystem_type": "/proc/self/mountinfo", "is_current": True}
