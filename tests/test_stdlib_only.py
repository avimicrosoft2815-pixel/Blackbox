"""P0-13: nothing under src/ may import anything outside the standard library.

Deliberately fails if `pytest` or `hypothesis` ever leak into the shipped package.

The one exception is the capture layer's optional `httpx` integration (D-020), and it is not a blanket one: the file
must be named in `_capture_policy.OPTIONAL_HTTP`, and the import must sit **inside a function**, so `import blackbox`
never needs httpx. `test_httpx_is_never_imported_at_module_scope` below is the half that actually enforces optionality.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import _capture_policy
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
    scoped = _capture_policy.function_scoped_modules(path) if _capture_policy.is_exempt(path) else set()
    offenders = []
    for lineno, module in _modules(path):
        root = module.split(".")[0]
        if root and root not in ALLOWED and not (root == "httpx" and root in scoped):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno} imports {module!r}")
    assert not offenders, "non-stdlib import(s):\n" + "\n".join(offenders)


@pytest.mark.parametrize("rel", _capture_policy.OPTIONAL_HTTP)
def test_httpx_is_never_imported_at_module_scope(rel: str):
    """Optional means: `import blackbox` works on a machine with no httpx. A module-scope import breaks exactly that."""
    path = SRC / "blackbox" / rel
    if not path.exists():
        return                       # the module has not landed yet; the rule applies from the day it does
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert not any(n.split(".")[0] == "httpx" for n in names), f"{rel}:{node.lineno} imports httpx at module scope"


def test_dev_tools_never_imported_by_src():
    banned = {"pytest", "hypothesis", "_pytest", "pluggy"}
    hits = [
        f"{p.relative_to(ROOT)}:{lineno} imports {m}"
        for p in _python_files()
        for lineno, m in _modules(p)
        if m.split(".")[0] in banned
    ]
    assert not hits, "test tooling leaked into src/:\n" + "\n".join(hits)
