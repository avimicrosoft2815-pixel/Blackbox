"""C26: the platform layer is a dispatch, not a set of modules anyone may reach into.

`tape/lease.py` used to `from blackbox.platform import posix` and call `boot_id`, `pid_alive` and
`proc_start_ticks` directly, so the `ctypes` `OpenProcess`/`GetProcessTimes` path written for P2-03 could never
be reached and a Windows run called `os.kill(pid, 0)` — a console signal there, not a liveness probe. This test
walks `src/` and fails if any module outside `blackbox/platform/` imports a platform shim by name.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))      # this file must pass on its own, not only after another test set it
SRC = ROOT / "src" / "blackbox"
SHIMS = {"posix", "windows", "macos"}


def _platform_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "blackbox.platform" or node.module.startswith("blackbox.platform."):
                for alias in node.names:
                    if alias.name in SHIMS or node.module.rsplit(".", 1)[-1] in SHIMS:
                        found.append(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("blackbox.platform."):
                    found.append(alias.name)
    return found


def test_only_the_platform_package_imports_a_shim_by_name():
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.parent.name == "platform" and path.parent.parent.name == "blackbox":
            continue                              # the dispatch itself, and the shims importing each other
        for hit in _platform_imports(path):
            offenders.append(f"{path.relative_to(ROOT)}: {hit}")
    assert offenders == [], (
        "these modules bypass platform.current() and would misbehave on another OS: " + "; ".join(offenders))


def test_nothing_outside_the_platform_layer_calls_os_open():
    """C29: a bare `os.open` is a text-mode descriptor on Windows. One hazard, one guard."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.parent.name == "platform":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "os.open(" in stripped and "open_fd" not in stripped:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped[:80]}")
    # directory handles for fsync are the one documented exception: they are never written through.
    offenders = [o for o in offenders if "O_RDONLY" not in o]
    assert offenders == [], (
        "these descriptors would be text mode on Windows; use platform.open_fd: " + "; ".join(offenders))


def test_the_dispatch_is_the_only_path_into_a_shim():
    init = (SRC / "platform" / "__init__.py").read_text(encoding="utf-8")
    assert "def current()" in init
    for shim in SHIMS:
        assert f"from blackbox.platform import {shim}" in init, f"current() must be able to return {shim}"


def test_the_lease_reaches_the_platform_through_current():
    lease = (SRC / "tape" / "lease.py").read_text(encoding="utf-8")
    assert "from blackbox.platform import current as _plat_current" in lease
    for name in ("boot_id", "pid_alive", "proc_start_ticks"):
        assert f"_plat.{name}(" in lease, f"the lease must call {name} on the dispatched shim"
        assert f"posix.{name}(" not in lease


def test_a_posix_liveness_probe_refuses_to_run_off_posix(monkeypatch):
    """A mis-dispatch must fail loudly, never signal a console (C26)."""
    import os

    import pytest

    from blackbox.platform import posix

    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(NotImplementedError):
        posix.pid_alive(os.getpid())
