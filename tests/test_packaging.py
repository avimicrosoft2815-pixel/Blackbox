"""P0-01/P0-02: packaging metadata and version consistency."""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def test_requires_python_floor():
    project = _pyproject()["project"]
    assert project["requires-python"].startswith(">=")
    minor = int(project["requires-python"].split(">=")[1].split(".")[1])
    assert minor >= 11, "Python floor must stay >= 3.11 (tomllib, ExceptionGroup, perf_counter_ns)"


def test_no_runtime_dependencies():
    assert _pyproject()["project"].get("dependencies", []) == []


def test_dev_extra_is_pinned_and_minimal():
    extras = _pyproject()["project"]["optional-dependencies"]
    dev = extras["dev"]
    names = {re.split(r"[=<>!]=?", d)[0].strip(): d for d in dev}
    assert set(names) == {"pytest", "hypothesis"}, names
    for spec in dev:
        assert "==" in spec, f"dev dependency must be pinned exactly: {spec}"
    # D-020: the optional HTTP integration lives in its own extra so `dev` stays the two test tools. Nothing here is
    # a runtime dependency — `[project].dependencies` is asserted empty above, and that is what keeps the core
    # stdlib-only.
    for extra, contents in extras.items():
        assert extra in {"dev", "capture", "capture-sdks"}, f"unexpected optional extra {extra!r}"
        for spec in contents:
            assert "==" in spec, f"{extra} dependency must be pinned exactly: {spec}"


def test_version_matches_package():
    sys.path.insert(0, str(ROOT / "src"))
    import blackbox
    assert blackbox.__version__ == _pyproject()["project"]["version"]


def test_build_backend_present():
    build = _pyproject()["build-system"]
    assert build["requires"], "build-system.requires must not be empty"
    assert build["build-backend"]
