"""Defect 25: the Windows teardown fixture must find writers without touching anything else.

The first version of `tests/conftest.py` walked `gc.get_objects()` and called `getattr(obj, "_head", None)` on
**every live object** to find leaked `TapeWriter`s. Live objects include the SDKs' `LazyProxy` instances: `anthropic`
keeps `google_auth` and its siblings behind `anthropic._utils._proxy.LazyProxy`, whose `__getattr__` calls
`__load__()` and raises `MissingDependencyError` when the optional dependency is not installed. So a `getattr` on a
proxy for `google-auth` raised, in a fixture teardown, and pytest attributed the `ERROR` to whichever test was
running — nine innocent property tests, on Windows only (the block returns early elsewhere), once CI began installing
`anthropic` (`capture-sdks`).

Both conditions had to hold at once, which is exactly why the suite had been green.

The rule that fixes it: ask the **exact type** and never the attributes. `type(obj) is TapeWriter` cannot trigger
`__getattr__`, `__getattribute__`, a property or a descriptor. The objects below enforce that by raising if anyone
so much as asks them a question.
"""
from __future__ import annotations

import gc

import pytest
from _hygiene import leaked_writers


class Hostile:
    """The shape of the bug: an object in the collector that raises on any attribute access."""

    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"the cleanup fixture touched {name!r} on an object that is not a writer")

    @property
    def __class__(self) -> type:                      # deliberately lying: that is the point
        raise RuntimeError("the cleanup fixture asked for __class__")


class FakeWriter:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


def test_a_hostile_object_is_never_touched():
    hostile = Hostile()
    gc.collect()
    assert hostile not in leaked_writers(gc.get_objects(), FakeWriter)


def test_a_real_candidate_is_found_and_a_subclass_is_not_mistaken_for_one():
    writer = FakeWriter()

    class SubWriter(FakeWriter):
        pass

    found = leaked_writers([writer, SubWriter(), Hostile(), object(), None, 3], FakeWriter)
    assert found == [writer], "exact type only: a subclass is a different class and a proxy is not a writer"


def test_the_real_sdk_proxy_still_raises_the_way_it_did():
    """If `anthropic` ever stops raising from its lazy proxies, the two tests above stop modelling the defect and
    this file should be replaced rather than kept for decoration."""
    pytest.importorskip("anthropic")
    from anthropic._utils._proxy import LazyProxy

    class Missing(LazyProxy[object]):
        def __load__(self) -> object:
            raise RuntimeError("optional dependency absent, exactly like google-auth here")

    proxy = Missing()
    assert proxy not in leaked_writers([proxy], FakeWriter), "the fixture must not ask a proxy anything"
    with pytest.raises(RuntimeError):
        assert proxy.anything is None
