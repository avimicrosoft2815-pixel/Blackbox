"""Every claim in `docs/guarantees.md` has to point at a test that exists (defect 112).

The §24 row reads *"`docs/guarantees.md` exists and every claim links to a test ID and a published result"*, and until
2026-11-20 the file linked to four paths that had never existed in this repository — `tests/kill/test_kill_stages.py`,
`tests/test_head.py`, `tests/test_prealloc_sigbus.py`, `tests/test_recovery_idempotence.py` — while every row said
`planned`. A guarantees table whose links do not resolve is worse than no table: it reads as evidence.

This test collects the tree and asserts that every `tests/…` path and every `::node` named in the table is in the
collection. It does **not** assert that a `pass` in the table is true — that is a reviewer's read of a run, and the row's
own state says so.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARANTEES = ROOT / "docs" / "guarantees.md"

PATH = re.compile(r"`(tests/[A-Za-z0-9_./-]+\.py)(::([A-Za-z0-9_]+))?`")


def _defined(path: Path) -> set[str]:
    """Every function and class name a test file defines, read from its own source rather than from a collector run.

    A node id in the table is `file::name`; what the reader needs to know is that the name is there. Reading the AST is
    what makes this cheap enough to run inside the suite it is checking, and a rename shows up as a missing name exactly
    as it would in a run.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def _collected() -> tuple[set[str], set[str]]:
    files: set[str] = set()
    ids: set[str] = set()
    for path in sorted((ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        files.add(relative)
        for name in _defined(path):
            ids.add(f"{relative}::{name}")
    return files, ids


def test_every_path_and_node_the_guarantees_table_names_is_in_the_tree():
    # The claims table only. The prose above it names the four paths that never existed, on purpose: that sentence is the
    # record of the defect this test closes, and checking it would make the file unable to describe its own history.
    text = "\n".join(line for line in GUARANTEES.read_text(encoding="utf-8").splitlines() if line.startswith("| "))
    files, ids = _collected()
    assert files, "the collector returned no test ids; this test would then guard nothing"
    problems: list[str] = []
    for match in PATH.finditer(text):
        path, _, node = match.group(1), match.group(2), match.group(3)
        if path not in files and not (ROOT / path).exists():
            problems.append(f"{path} does not exist")
        elif node:
            # A node named without its file (the `…::test_s2` shorthand) is checked against every id ending in it.
            qualified = f"{path}::{node}"
            if qualified not in ids:
                problems.append(f"{qualified} is not collected")
    assert not problems, "docs/guarantees.md names tests that are not there:\n" + "\n".join(problems)


def test_the_table_does_not_read_as_evidence_it_does_not_have():
    """No row may still say `planned`, and no row may claim a result without naming a test."""
    text = GUARANTEES.read_text(encoding="utf-8")
    table = [line for line in text.splitlines() if line.startswith("| ") and "`tests/" in line]
    assert table, "the claims table is empty or lost its shape"
    stale = [line for line in table if "planned" in line.lower()]
    assert not stale, "a row still says planned:\n" + "\n".join(stale)
    for line in table:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) >= 3, f"a row lost a column: {line}"
        assert cells[-1], f"a row claims nothing about its result: {line}"
