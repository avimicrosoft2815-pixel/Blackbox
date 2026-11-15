"""Shared test hygiene for three operating systems (C25).

On POSIX an open fd does not stop a directory from being deleted; on Windows it does (`WinError 32`), so a test
that leaks a writer makes the *next* test fail in teardown, which is a confusing way to learn about a leak. The
fixture below closes every `TapeWriter` a test leaves open, so a leak is reported as what it is.

It finds them by **exact type**, never by probing attributes: the first version called `getattr(obj, "_head", None)`
on every object the collector knows about, modules included, and a module whose PEP 562 `__getattr__` raises —
`anthropic.lib._extras` — turned that into a teardown error for nine unrelated tests on Windows (defect 25,
`tests/_hygiene.py`, `tests/test_gc_hygiene.py`).
"""
from __future__ import annotations

import contextlib
import gc
import os

import pytest
from _hygiene import leaked_writers


@pytest.fixture(autouse=True)
def close_leaked_handles() -> object:
    yield
    if os.name != "nt":
        return
    from blackbox.tape.writer import TapeWriter

    for writer in leaked_writers(gc.get_objects(), TapeWriter):
        with contextlib.suppress(Exception):
            writer.abort()
