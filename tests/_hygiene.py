"""The Windows handle-hygiene predicate, in a module a test can call with poisoned objects.

Why this is not just four lines inside `conftest.py`: the first version walked `gc.get_objects()` and did
`getattr(obj, "_head", None)` on **every live object** to find leaked `TapeWriter`s. Modules are garbage-collected
objects too, and a module may define a PEP 565 `__getattr__` that raises for names it does not know —
`anthropic.lib._extras._common` does exactly that, raising `MissingDependencyError` for an optional dependency it
cannot find. So the cleanup fixture raised during teardown of whatever test happened to be running, and pytest
reported `ERROR tests/property/test_tape_model.py::…` for nine innocent tests.

It only showed up on Windows (the block returns early elsewhere) and only after CI started installing `anthropic`
(the module was not in `sys.modules` before). Both conditions had to hold, which is why it survived the suite.

The fix is to ask the *type* first and the attributes never: `type(obj) is TapeWriter` cannot trigger a `__getattr__`,
an `isinstance` hook, or a property. This module exists so that rule is asserted against an object that raises if
anybody touches it, instead of being trusted.
"""
from __future__ import annotations

from typing import Any


def leaked_writers(objects: list[Any], writer_type: type) -> list[Any]:
    """Only the writers, identified by exact type. Touching nothing else is the whole point."""
    return [obj for obj in objects if type(obj) is writer_type]
