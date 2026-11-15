"""The node stack (P3-03/P3-04): what a recorded call is *inside*, carried on `contextvars`.

A node is one unit of work the reader will see as a row: a workflow, an agent, a tool call, a retrieval, a step. The
stack is a `ContextVar`, which is the only mechanism that follows a call across `await` without the recorder holding
a reference to the caller's frame — and the reason it is not a `threading.local` is the same: an `async` task that
awaits a model call must still be inside the node it started in when it resumes.

Three consequences the plan asks for by name, and this module implements them explicitly:

* **Nesting is cheap and deep.** 50 levels is a test, not a limit: a slot is an int, and the frame that names it is
  emitted only when the node opens.
* **A thread that was not given a context gets a detached run, not silence.** `threading.Thread` starts with an
  empty context, so an event recorded there would otherwise have no parent. Instead the first event on such a thread
  opens a root node named after the thread, marked `detached: True`, so the reader sees *why* it has no parent
  rather than an orphan frame.
* **Inheritance is opt-in.** `inherit()` copies the current context into a new one for a worker thread that should
  belong to the caller's node; without it the worker is detached. Guessing either way would be wrong: inheriting a
  node into a pool thread that outlives the request produces a tree that is a fiction.

Nothing here raises into user code, and nothing is emitted when recording is off: `open_node` returns a Node whose
`slot` is 0 and `emit` is skipped, so a disabled process pays two attribute reads per decorated call.
"""
from __future__ import annotations

import contextlib
import contextvars
import itertools
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from blackbox import api
from blackbox import constants as C
from blackbox.codec import cbor


@dataclass(frozen=True)
class Node:
    """One open unit of work. `slot` is what the frames carry; 0 means "not recorded"."""

    slot: int
    name: str
    kind: str
    parent: int | None
    opened_ns: int
    detached: bool = False


_CURRENT: contextvars.ContextVar[Node | None] = contextvars.ContextVar("blackbox_node", default=None)
_slots = itertools.count(1)
_slot_lock = threading.Lock()
_depth = contextvars.ContextVar("blackbox_depth", default=0)
_threads = threading.local()          # has this thread ever opened a node? (P3-04's detached-root rule)


def _next_slot() -> int:
    with _slot_lock:
        return next(_slots)


def current() -> Node | None:
    """The node this call is inside, or None at the top of a run."""
    return _CURRENT.get()


def depth() -> int:
    return _depth.get()


def open_node(name: str, kind: str = "node", **fields: Any) -> Node:
    """Open a node and make it current. Never raises.

    On a thread that has never opened a node and is not the main thread, the node hangs off a detached root
    (`kind="detached"`), so a worker's frames are attached to something that says what it is (P3-04).
    """
    parent = _CURRENT.get()
    if parent is None and kind != "detached" and not getattr(_threads, "seen", False) \
            and threading.current_thread() is not threading.main_thread():
        parent = _open_root(name=f"detached:{threading.current_thread().name}")
    _threads.seen = True
    if not api.enabled():
        node = Node(slot=0, name=name, kind=kind, parent=None, opened_ns=0, detached=parent is None)
        _CURRENT.set(node)
        _depth.set(_depth.get() + 1)
        return node
    node = Node(slot=_next_slot(), name=name, kind=kind, parent=None if parent is None else parent.slot,
                opened_ns=time.time_ns())
    payload = {"name": name, "kind": kind, "parent": node.parent, "opened_ns": node.opened_ns}
    payload.update(fields)
    api.emit(C.KIND_NODE_OPEN, cbor.encode(payload), node_slot=node.slot)
    _CURRENT.set(node)
    _depth.set(_depth.get() + 1)
    return node


def close_node(node: Node | None, outcome: str = "ok", error: BaseException | None = None) -> None:
    """Close a node, restoring the parent. Never raises."""
    if node is None:
        return
    _CURRENT.set(None if node.parent is None else _parent_of(node))
    _depth.set(max(0, _depth.get() - 1))
    if not node.slot:
        return
    payload: dict[str, Any] = {"name": node.name, "kind": node.kind, "outcome": outcome,
                               "duration_us": max(0, (time.time_ns() - node.opened_ns) // 1000)}
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["error"] = str(error)[:500]
    api.emit(C.KIND_NODE_CLOSE, cbor.encode(payload), node_slot=node.slot)


def _parent_of(node: Node) -> Node | None:
    """Rebuild the parent handle from the slot alone: the stack is a link, not a list of objects."""
    parent = _CURRENT.get()
    if parent is not None and parent.slot == node.parent:
        return parent
    return Node(slot=int(node.parent or 0), name="", kind="node", parent=None, opened_ns=0)


def _open_root(name: str) -> Node:
    """Emit the detached root itself (no parent, no recursion)."""
    if not api.enabled():
        return Node(slot=0, name=name, kind="detached", parent=None, opened_ns=0, detached=True)
    from blackbox.codec import cbor as _cbor

    node = Node(slot=_next_slot(), name=name, kind="detached", parent=None, opened_ns=time.time_ns())
    api.emit(C.KIND_NODE_OPEN, _cbor.encode({"name": name, "kind": "detached", "parent": None,
                                            "opened_ns": node.opened_ns, "detached": True}), node_slot=node.slot)
    return node


def detached_root(thread_name: str | None = None) -> Node:
    """Explicitly start a detached run on the current thread (P3-04)."""
    node = _open_root(f"detached:{thread_name or threading.current_thread().name}")
    _CURRENT.set(node)
    return node


def ensure_current() -> Node:
    """Return the current node, opening a detached root for a thread that has none."""
    node = _CURRENT.get()
    if node is not None:
        return node
    return detached_root()


@contextlib.contextmanager
def inherit() -> Iterator[contextvars.Context]:
    """A context a worker thread can run in so that it belongs to the caller's node (opt-in)."""
    ctx = contextvars.copy_context()
    yield ctx


def mark(name: str, **fields: Any) -> int:
    """Emit a `MARK` inside the current node. Returns the seq, 0 when recording is off."""
    if not api.enabled():
        return 0
    node = _CURRENT.get()
    payload = {"name": name, "node_slot": 0 if node is None else node.slot}
    payload.update(fields)
    return api.emit(C.KIND_MARK, cbor.encode(payload), node_slot=0 if node is None else node.slot)


def handled_error(error: BaseException, **fields: Any) -> int:
    """Record an exception the application *handled* — the interesting ones, and the ones that vanish from logs."""
    if not api.enabled():
        return 0
    node = _CURRENT.get()
    payload = {"type": type(error).__name__, "message": str(error)[:500], "handled": True}
    payload.update(fields)
    return api.emit(C.KIND_EXCEPTION, cbor.encode(payload), node_slot=0 if node is None else node.slot)
