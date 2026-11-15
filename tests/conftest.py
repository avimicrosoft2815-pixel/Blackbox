"""Shared test hygiene for three operating systems (C25).

On POSIX an open fd does not stop a directory from being deleted; on Windows it does (`WinError 32`), so a test
that leaks a writer makes the *next* test fail in teardown, which is a confusing way to learn about a leak. The
fixture below closes every `TapeWriter` and `HeadFile` a test leaves open, so a leak is reported as what it is.
"""
from __future__ import annotations

import contextlib
import gc
import os

import pytest


@pytest.fixture(autouse=True)
def close_leaked_handles():
    yield
    if os.name != "nt":
        return
    # Windows only: find live writers and close them before pytest deletes tmp_path.
    for obj in gc.get_objects():
        for attr in ("_head",):
            handle = getattr(obj, attr, None)
            if handle is not None and hasattr(handle, "close") and type(obj).__name__ == "TapeWriter":
                with contextlib.suppress(Exception):
                    obj.abort()
