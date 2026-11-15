"""P3-02 — the decorators: one node per call, the signature survives, and *off* costs nothing.

Two claims are tested here, and they pull in opposite directions. When recording is on, a decorated function must
produce a `NODE_OPEN`/`NODE_CLOSE` pair that a reader can pair up, with the error case carrying the exception type
instead of silently vanishing. When recording is off, the same decorator must be a pass-through that costs less than
a microsecond — because the promise of this product is that a team can leave it installed, and a recorder that taxes
every function call by even 5 µs is one that gets removed after a week.

The overhead test measures the *median* of many calls: a single measurement is dominated by GC and by the machine,
and a p99 bound here would be a flaky test on a shared runner (C40 is the cautionary tale).
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib
import statistics
import time

import pytest

from blackbox import api
from blackbox import constants as C
from blackbox.rec import context as node_ctx
from blackbox.tape import scanner


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("BLACKBOX_DISABLED", raising=False)
    monkeypatch.setenv("BLACKBOX_HOME", str(tmp_path))
    api.shutdown()
    yield
    api.shutdown()


def _frames(tmp_path):
    tape = pathlib.Path(str(tmp_path)) / "tape"
    uuid = sorted(p.name for p in tape.iterdir())[0]
    return scanner.scan(str(tmp_path), uuid).frames


def _decoded(frame):
    from blackbox.codec import cbor

    return cbor.decode(frame["payload"])[0]


def test_each_decorator_kind_writes_one_open_and_one_close(tmp_path):
    api.install(data_dir=str(tmp_path))

    @api.workflow
    def wf() -> str:
        return "w"

    @api.agent(name="planner")
    def ag() -> str:
        return "a"

    @api.tool
    def tl() -> str:
        return "t"

    @api.retrieval
    def rt() -> str:
        return "r"

    @api.step("parse")
    def st() -> str:
        return "s"

    assert [f() for f in (wf, ag, tl, rt, st)] == ["w", "a", "t", "r", "s"]
    api.shutdown()
    frames = _frames(tmp_path)
    opens = [f for f in frames if f["kind"] == C.KIND_NODE_OPEN]
    closes = [f for f in frames if f["kind"] == C.KIND_NODE_CLOSE]
    assert len(opens) == len(closes) == 5
    assert {f["node_slot"] for f in opens} == {f["node_slot"] for f in closes}, "a node opened and never closed"
    kinds = sorted(_decoded(f)["kind"] for f in opens)
    assert kinds == ["agent", "retrieval", "step", "tool", "workflow"], kinds


def test_the_signature_and_the_name_survive_the_decorator():
    def handler(request_id: int, *, retries: int = 3) -> str:
        """docstring that must survive"""
        return f"{request_id}/{retries}"

    before = inspect.signature(handler)
    handler = api.step("inner")(handler)
    assert inspect.signature(handler) == before, "a decorated call must be callable exactly like the original"
    assert handler.__name__ == "handler"
    assert handler.__doc__ == "docstring that must survive"
    assert handler(1, retries=2) == "1/2"


def test_an_exception_closes_the_node_with_its_type_and_is_re_raised(tmp_path):
    api.install(data_dir=str(tmp_path))

    @api.tool
    def boom() -> None:
        raise ValueError("side effect failed")

    with pytest.raises(ValueError):
        boom()
    api.shutdown()
    closes = [f for f in _frames(tmp_path) if f["kind"] == C.KIND_NODE_CLOSE]
    assert len(closes) == 1
    body = _decoded(closes[0])
    assert body.get("outcome") == "error", body
    assert body.get("error_type") == "ValueError", body
    assert "side effect failed" in str(body.get("error"))


def test_a_disabled_decorator_is_a_pass_through_and_costs_under_a_microsecond():
    api.shutdown()                                    # recording off: the fast path

    def raw(n: int) -> int:
        return n + 1

    fast = api.step("hot")(raw)

    def median_ns(fn) -> float:
        samples = []
        for _ in range(20):
            start = time.perf_counter_ns()
            for i in range(5_000):
                fn(i)
            samples.append((time.perf_counter_ns() - start) / 5_000)
        return statistics.median(samples)

    overhead = median_ns(fast) - median_ns(raw)
    assert not api.enabled()
    assert fast(1) == 2
    assert overhead < 1_000, f"disabled decorated call costs {overhead:.0f} ns over the raw call (budget 1 µs)"


def test_an_async_decorator_keeps_the_node_across_an_await(tmp_path):
    api.install(data_dir=str(tmp_path))

    @api.step("io")
    async def fetch() -> str:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return "done"

    assert asyncio.run(fetch()) == "done"
    api.shutdown()
    frames = _frames(tmp_path)
    assert sum(f["kind"] == C.KIND_NODE_OPEN for f in frames) == 1
    assert sum(f["kind"] == C.KIND_NODE_CLOSE for f in frames) == 1


def test_nested_decorators_produce_a_chain_of_slots_not_a_flat_list(tmp_path):
    api.install(data_dir=str(tmp_path))

    @api.tool
    def inner() -> None:
        return None

    @api.workflow
    def outer() -> None:
        inner()

    outer()
    api.shutdown()
    opens = [f for f in _frames(tmp_path) if f["kind"] == C.KIND_NODE_OPEN]
    assert len(opens) == 2
    assert node_ctx.current() is None, "the stack must be empty when the outermost node closes"
