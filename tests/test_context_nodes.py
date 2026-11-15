"""P3-03/P3-04 — the node stack: nesting, `mark`, `handled_error`, `capture`, and the thread fallback.

The plan's Done-when for P3-03 is *nesting 50 deep; `mark` lands in the right node*, and for P3-04 *sync/async/
threadpool/multiprocessing tests*. Both are about the same failure: a frame that lands in the wrong node is worse
than a frame that is missing, because the reader draws a tree from it and the tree is then a lie. So every test here
checks *where* a frame went (its `node_slot`), not merely that it was written.

The thread case is the one that decides the design. `contextvars` do not inherit into a new thread, so a worker gets
a detached root rather than silence — and the test proves the frame says `detached: True`, because "why does this
have no parent" is the question the reader will ask.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import multiprocessing
import pathlib

import pytest

from blackbox import api
from blackbox import constants as C
from blackbox.codec import cbor
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
    uuid = sorted((pathlib.Path(str(tmp_path)) / "tape").iterdir())[0].name
    return scanner.scan(str(tmp_path), uuid).frames


def _decoded(frame):
    return cbor.decode(frame["payload"])[0]


def test_mark_lands_in_the_node_it_was_called_inside(tmp_path):
    api.install(data_dir=str(tmp_path))
    with api.run("ingest"):
        outer = api.current()
        api.mark("phase", phase="parse")
        with api.step("inner"):
            inner = api.current()
            api.mark("phase", phase="write")
    api.shutdown()

    frames = _frames(tmp_path)
    marks = [_decoded(f) for f in frames if f["kind"] == C.KIND_MARK]
    assert [(m["name"], m["node_slot"]) for m in marks] == [("phase", outer.slot), ("phase", inner.slot)]
    assert outer.slot != inner.slot


def test_fifty_nested_steps_stay_balanced_and_keep_their_parents(tmp_path):
    api.install(data_dir=str(tmp_path))

    def nest(level: int) -> None:
        if level == 50:
            return
        with api.step(f"level-{level}"):
            assert node_ctx.depth() == level + 2, (level, node_ctx.depth())
            nest(level + 1)

    with api.run("deep"):
        nest(0)
    assert api.current() is None, "the stack must unwind to empty"
    api.shutdown()

    frames = _frames(tmp_path)
    opens = [_decoded(f) for f in frames if f["kind"] == C.KIND_NODE_OPEN]
    closes = [f for f in frames if f["kind"] == C.KIND_NODE_CLOSE]
    assert len(opens) == len(closes) == 51
    slots = [f["node_slot"] for f in frames if f["kind"] == C.KIND_NODE_OPEN]
    assert opens[0]["parent"] is None, "the run is the root"
    for index in range(1, len(opens)):
        assert opens[index]["parent"] == slots[index - 1], f"level {index} hangs off the wrong node"


def test_handled_error_is_recorded_where_it_was_handled(tmp_path):
    api.install(data_dir=str(tmp_path))
    with api.step("retry-loop"):
        node = api.current()
        try:
            raise TimeoutError("upstream took 30 s")
        except TimeoutError as exc:
            api.handled_error(exc, attempt=2)
    api.shutdown()

    frames = _frames(tmp_path)
    errors = [_decoded(f) for f in frames if f["kind"] == C.KIND_EXCEPTION]
    assert len(errors) == 1
    assert errors[0]["type"] == "TimeoutError"
    assert errors[0]["handled"] is True
    assert errors[0]["attempt"] == 2
    assert [f["node_slot"] for f in frames if f["kind"] == C.KIND_EXCEPTION] == [node.slot]


def test_capture_carries_its_fields_into_the_node_open(tmp_path):
    api.install(data_dir=str(tmp_path))
    with api.capture("prompt", model="mock-1", tokens=42):
        pass
    api.shutdown()
    opens = [_decoded(f) for f in _frames(tmp_path) if f["kind"] == C.KIND_NODE_OPEN]
    assert len(opens) == 1
    assert opens[0]["kind"] == "capture"
    assert opens[0]["model"] == "mock-1" and opens[0]["tokens"] == 42


def test_an_event_on_a_plain_thread_gets_a_detached_root_not_an_orphan(tmp_path):
    api.install(data_dir=str(tmp_path))

    def worker() -> int:
        with api.step("in-thread"):
            return api.current().slot

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(worker).result()
    api.shutdown()

    frames = _frames(tmp_path)
    opens = [_decoded(f) for f in frames if f["kind"] == C.KIND_NODE_OPEN]
    detached = [o for o in opens if o["kind"] == "detached"]
    assert len(detached) == 1, [o["kind"] for o in opens]
    assert detached[0]["name"].startswith("detached:")
    assert detached[0]["parent"] is None
    inner = [o for o in opens if o["kind"] == "step"]
    assert inner and inner[0]["parent"] is not None, "the step must hang off the detached root"


def test_async_tasks_keep_their_own_nodes_under_one_run(tmp_path):
    api.install(data_dir=str(tmp_path))

    async def child(name: str) -> int:
        with api.step(name):
            await asyncio.sleep(0)
            return api.current().slot

    async def parent() -> tuple[int, int]:
        with api.run("gather"):
            return tuple(await asyncio.gather(child("a"), child("b")))  # type: ignore[return-value]

    first, second = asyncio.run(parent())
    assert first != second, "two concurrent tasks shared one slot"
    api.shutdown()
    opens = [_decoded(f) for f in _frames(tmp_path) if f["kind"] == C.KIND_NODE_OPEN]
    assert sorted(o["kind"] for o in opens) == ["run", "step", "step"]


def _child_body(child_dir: str) -> None:
    """Runs in the child process (module level, so `spawn` can pickle it)."""
    api.shutdown()                    # a forked child inherits the parent's writer; drop it before opening its own
    api.install(data_dir=child_dir)
    api.mark("from-child")
    api.shutdown()


def test_a_child_process_records_into_its_own_tape(tmp_path):
    """Multiprocessing: the child must not inherit a file descriptor into the parent's tape and write there.

    Uses `fork` where it exists and `spawn` where it does not (Windows), which is why the child body is a module
    level function: `spawn` pickles it, and a closure would fail on the Windows cells with
    `AttributeError: Can't pickle local object` instead of testing anything.
    """
    method = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    ctx = multiprocessing.get_context(method)
    api.install(data_dir=str(tmp_path))
    parent_tape = pathlib.Path(str(tmp_path)) / "tape"
    before = {p.name for p in parent_tape.iterdir()}

    child_dir = str(tmp_path / "child")
    proc = ctx.Process(target=_child_body, args=(child_dir,))
    proc.start()
    proc.join(60)
    api.shutdown()
    assert proc.exitcode == 0, proc.exitcode
    assert (pathlib.Path(child_dir) / "tape").exists(), "the child wrote nothing"
    after = {p.name for p in parent_tape.iterdir()}
    assert after == before, "the child wrote into its parent's tape"
