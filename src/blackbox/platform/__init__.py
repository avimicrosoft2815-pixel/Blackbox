"""Platform shims: `posix`, `windows` (P2-03) and `macos` (P2-04).

Each module answers the same questions and publishes a `capabilities()` dict, so nothing about a platform is
implied silently. `current()` picks one by `os.name`/`sys.platform`; an unsupported platform gets `posix` only
if it looks like POSIX, otherwise `NotImplementedError` from the shim itself (the recorder degrades, it does
not crash the user's program).
"""
from __future__ import annotations

import os
import sys
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


def capabilities() -> dict[str, object]:
    mod = current()
    if hasattr(mod, "capabilities"):
        caps: dict[str, object] = mod.capabilities()
        return caps
    return {"platform": mod.__name__.rsplit(".", 1)[-1], "prealloc": "posix_fallocate",
            "directory_fsync": True, "process_start_token": True, "boot_id": True,
            "filesystem_type": "/proc/self/mountinfo", "is_current": True}
