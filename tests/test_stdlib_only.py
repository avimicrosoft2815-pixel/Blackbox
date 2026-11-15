"""P0-13: nothing under src/ may import anything outside the standard library.

Deliberately fails if `pytest` or `hypothesis` ever leak into the shipped package.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Modules that are allowed in the core, i.e. stdlib names we actually use or plausibly will.
ALLOWED = set(sys.stdlib_module_names) | {"blackbox"}


def _modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import stays inside the package
                continue
            yield node.lineno, node.module or ""


def _python_files():
    return sorted(SRC.rglob("*.py"))


def test_src_has_files():
    assert _python_files(), "no python sources found under src/"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_only_stdlib_imports(path: Path):
    offenders = []
    for lineno, module in _modules(path):
        root = module.split(".")[0]
        if root and root not in ALLOWED:
            offenders.append(f"{path.relative_to(ROOT)}:{lineno} imports {module!r}")
    assert not offenders, "non-stdlib import(s):\n" + "\n".join(offenders)


def test_dev_tools_never_imported_by_src():
    banned = {"pytest", "hypothesis", "_pytest", "pluggy"}
    hits = [
        f"{p.relative_to(ROOT)}:{lineno} imports {m}"
        for p in _python_files()
        for lineno, m in _modules(p)
        if m.split(".")[0] in banned
    ]
    assert not hits, "test tooling leaked into src/:\n" + "\n".join(hits)
