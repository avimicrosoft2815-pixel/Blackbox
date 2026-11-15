"""ADR-005 + DV-010: no part of a tape is memory-mapped, and nothing in `src/` imports `mmap`.

The failure this prevents is on record: the active segment was mapped, and a page fault on a segment whose blocks
had gone (ENOSPC, or a file truncated under the mapping) raised SIGBUS inside the user's process — the one failure
class S17 and P1-20 exist to remove. ADR-005 moved the writer to `pwrite`; DV-010 removed the last mapping, the
head. A test is the only thing that keeps the import from coming back the next time someone wants a fast path.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_module_imports_mmap() -> None:
    offenders = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imports_mmap = isinstance(node, ast.Import) and any(alias.name == "mmap" for alias in node.names)
            from_mmap = isinstance(node, ast.ImportFrom) and node.module == "mmap"
            if imports_mmap or from_mmap:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], "a tape file is mapped again: " + ", ".join(offenders)


def test_no_attribute_call_on_an_mmap_object() -> None:
    """`mmap.mmap(...)` without the import is the other way back; the AST sees the name either way."""
    offenders = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "mmap":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], "mmap is still referenced: " + ", ".join(offenders)
