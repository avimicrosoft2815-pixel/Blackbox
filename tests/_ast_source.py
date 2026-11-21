"""`ast.parse` for the tests that read our own source, with one narrow retry for a CPython 3.11 bug (defect 121).

Several tests are static: they parse every file in `src/` and assert on the syntax tree rather than on prose, because a
substring search flags the word "requests" in a docstring. That is the right shape, and the assertion it carries is about
*our* code. The parser, however, is CPython's, and on 3.11 it can fail for a reason that has nothing to do with the file
being read:

    SystemError: AST constructor recursion depth mismatch (before=120, after=172)

seen once in a full-suite run of `tests/test_diagnose.py` and never in isolation. `ast.parse` records the thread's
recursion state on the way in and checks it on the way out; a garbage-collection pass that runs Python `__del__` code
during the parse — here, asyncio tasks and futures that earlier proxy tests left for the collector, which announce
themselves as "Task was destroyed but it is pending!" — can make the two numbers differ. It is an interpreter bug, not a
finding about the source, and the same parse of the same bytes succeeds immediately afterwards.

So: one retry, after a `gc.collect()` that gets the destructors out of the way *before* the parse starts, and only for
that exact `SystemError`. Anything else — a `SyntaxError`, an `OSError`, a different `SystemError` — propagates on the
first attempt, because a retry that swallowed those would hide the defect a static test exists to find.
"""
from __future__ import annotations

import ast
import gc
from pathlib import Path

RECURSION_MISMATCH = "AST constructor recursion depth mismatch"


def parse_source(path: Path) -> ast.Module:
    """Parse one file's source, retrying once if CPython's parser reports its own recursion mismatch."""
    source = path.read_text(encoding="utf-8")
    try:
        return ast.parse(source, filename=str(path))
    except SystemError as first:
        if RECURSION_MISMATCH not in str(first):
            raise
        gc.collect()
        try:
            return ast.parse(source, filename=str(path))
        except SystemError as second:
            raise SystemError(
                f"{path}: CPython's parser failed twice with {RECURSION_MISMATCH!r} ({first}; {second}). That is the "
                "interpreter bug recorded as defect 121, not a finding about this tree; re-run the test on its own.") from second
