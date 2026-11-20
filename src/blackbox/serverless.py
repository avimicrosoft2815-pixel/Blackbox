"""`blackbox.serverless.wrap` (§11.11, P8-11): one run per invocation, sealed at the boundary, never left open.

A serverless platform freezes or destroys the process between invocations. A recorder built for a long-lived process
assumes the opposite: a tape that stays open, a keeper thread that ticks, a recovery that runs when someone remembers. In
a Lambda that assumption is the bug — an open tape that nobody sealed is a run that never becomes a capsule.

So this wrapper changes the boundary and nothing else:

* **open at invoke** — `api.install(data_dir=…, mode="serverless")` starts a tape whose segments are 2 MiB and whose
  syncs happen per invocation, not per batch;
* **seal at return or exception** — `api.shutdown()` writes `RUN_END`, closes the tape, and `recovery.recover` builds the
  capsule; `on_capsule` is called with what was sealed, or with why it was not;
* **recover at the next invoke** — before opening, any tape in the directory that has no capsule is a previous
  invocation that was killed (a timeout, a freeze, an OOM). It is recovered and emitted through `on_capsule`, so the
  evidence that survived a timeout is not lost by the *next* invocation starting fresh;
* **say when it is gone** — a marker file written at invoke and removed at seal is the only thing that can survive a
  sandbox teardown. A marker with no tape and no capsule means the invocation was destroyed outright; the next invoke
  records `completeness="unknown_serverless"` on its own run rather than claiming the gap was empty.

What this wrapper does **not** do, and what `docs/serverless.md` says in the same words: it cannot recover anything from a
sandbox that was torn down (there is nothing left to read), and it does not make a capsule durable beyond `/tmp`. The
capsule has to be shipped by the user's own code — `on_capsule` is the hook for that.
"""
from __future__ import annotations

import contextlib
import functools
import glob
import json
import os
import shutil
import time
from collections.abc import Callable
from typing import Any

from blackbox import api
from blackbox import constants as C
from blackbox.errors import bbx
from blackbox.rec import recovery

MARKER = "invocation.json"


def default_data_dir() -> str:
    """§11.11: `/tmp/blackbox` — the one directory a serverless runtime is expected to keep between warm invocations."""
    # `/tmp` is the serverless convention (§11.11) and is what a Lambda/Cloud Run runtime keeps between warm invocations;
    # it is not a temporary file this process creates and forgets. A caller who wants somewhere else passes `data_dir`.
    return os.path.join(os.environ.get("BLACKBOX_SERVERLESS_HOME") or "/tmp", "blackbox")   # noqa: S108


def wrap(handler: Callable[..., Any] | None = None, *, data_dir: str | None = None,
         on_capsule: Callable[[dict[str, Any]], None] | None = None,
         content: str = "hash", segment_bytes: int = C.SERVERLESS_SEGMENT_BYTES) -> Any:
    """Wrap a handler (as a decorator, or by calling it with one). Each invocation is its own sealed run."""
    if handler is not None:
        return _wrap(handler, data_dir=data_dir, on_capsule=on_capsule, content=content,
                     segment_bytes=segment_bytes)

    def decorator(function: Callable[..., Any]) -> Any:
        return _wrap(function, data_dir=data_dir, on_capsule=on_capsule, content=content,
                     segment_bytes=segment_bytes)

    return decorator


def _wrap(handler: Callable[..., Any], *, data_dir: str | None, on_capsule: Callable[[dict[str, Any]], None] | None,
          content: str, segment_bytes: int) -> Any:
    root = os.path.abspath(data_dir or default_data_dir())

    @functools.wraps(handler)
    def invoke(event: Any = None, context: Any = None, *args: Any, **kwargs: Any) -> Any:
        # Fail-open: a data directory that cannot be used (a file where the directory should be, a read-only /tmp) must
        # not fail the request. The handler runs unrecorded and the wrapper says so — `install` would swallow the rest.
        if os.path.exists(root) and not os.path.isdir(root):
            document = {"run": None, "capsule": None, "root": None, "completeness": "not_recorded",
                        "error": f"{root} exists and is not a directory", "invocation_error": None}
            _emit(on_capsule, document)
            return handler(event, context, *args, **kwargs)
        with contextlib.suppress(OSError):
            os.makedirs(root, mode=0o700, exist_ok=True)
        # 1. anything a previous invocation left behind is evidence, not litter.
        recovered = recover_leftovers(root, on_capsule)
        marker = _write_marker(root, recovered)
        # 2. open this invocation's run.
        # `content` is a capture-policy decision the seams make, not a writer argument: passing it to the writer would be
        # a `TypeError` swallowed by `install`'s fail-open path, and every invocation would silently record nothing.
        status = api.install(data_dir=root, mode="serverless", segment_bytes=segment_bytes)
        writer_uuid = status.get("writer_uuid")
        if not status.get("enabled"):                 # fail-open: the app runs, unrecorded and told why
            _remove_marker(marker)
            return handler(event, context, *args, **kwargs)
        try:
            result = handler(event, context, *args, **kwargs)
        except BaseException as exc:                  # a timeout looks exactly like this from inside the process
            sealed = _seal(root, writer_uuid, on_capsule, error=exc)
            _remove_marker(marker)
            sealed["re_raised"] = True
            raise
        sealed = _seal(root, writer_uuid, on_capsule)
        _remove_marker(marker)
        return result

    # Introspection for tests and for a user who wants to know what the wrapper will do before deploying it.
    invoke.data_dir = root                                              # type: ignore[attr-defined]
    invoke.completeness = "unknown_serverless"                          # type: ignore[attr-defined]
    invoke.content = content                                            # type: ignore[attr-defined]
    return invoke


# ── sealing one invocation ─────────────────────────────────────────────────


def _seal(root: str, writer_uuid: str | None, on_capsule: Callable[[dict[str, Any]], None] | None,
          *, error: BaseException | None = None) -> dict[str, Any]:
    """Close the tape and build the capsule. Never raises: a seal that fails must not replace the handler's exception."""
    api.shutdown()
    document: dict[str, Any] = {"run": None, "capsule": None, "root": None, "error": None,
                               "completeness": "sealed", "invocation_error": None if error is None else
                               f"{type(error).__name__}: {error}"}
    if not writer_uuid:
        document["error"] = "no tape was opened for this invocation"
        document["completeness"] = "not_recorded"
    else:
        try:
            result = recovery.recover(root, writer_uuid)
            document.update({"run": result.run_id, "capsule": result.capsule_path,
                             "root": result.capsule_root, "state": result.state})
            if document["capsule"] is not None:
                _prune_tape(root, writer_uuid)
        except Exception as exc:
            document["error"] = f"{type(exc).__name__}: {exc}"
            document["completeness"] = "sealed_unrecovered"
    _emit(on_capsule, document)
    return document


def recover_leftovers(root: str, on_capsule: Callable[[dict[str, Any]], None] | None,
                      on_note: Callable[[dict[str, Any]], None] | None = None,
                      *, prune: bool = True) -> list[dict[str, Any]]:
    """Seal every tape in `root` that has no capsule yet — a previous invocation that was killed before it sealed."""
    sealed: list[dict[str, Any]] = []
    if not os.path.isdir(root):
        return sealed
    capsule_runs = set()
    for path in glob.glob(os.path.join(root, "capsules", "*.bbx")):
        capsule_runs.add(os.path.basename(path))
    for tape in sorted(glob.glob(os.path.join(root, "tape", "*"))):
        if not os.path.isdir(tape):
            continue
        writer_uuid = os.path.basename(tape)
        try:
            result = recovery.recover(root, writer_uuid)
        except Exception as exc:
            document = {"run": None, "capsule": None, "error": f"{type(exc).__name__}: {exc}",
                        "completeness": "recovery_failed", "recovered": True}
            _emit(on_capsule, document)
            sealed.append(document)
            continue
        document = {"run": result.run_id, "capsule": result.capsule_path, "root": result.capsule_root,
                    "state": result.state, "recovered": True,
                    "completeness": "unknown_serverless" if result.run_status == "recovered" else result.run_status}
        if document["capsule"] is not None and os.path.basename(str(document["capsule"])) in capsule_runs:
            document["completeness"] = "already_sealed"
        _emit(on_capsule, document)
        sealed.append(document)
        if prune and document["capsule"] is not None:
            _prune_tape(root, writer_uuid)
    return sealed


def _prune_tape(root: str, writer_uuid: str) -> None:
    """Drop a tape that has become a capsule.

    The capsule holds the same segment bytes and an integrity root over them, so the tape is a second copy of evidence
    that has already been sealed and shipped. Keeping it is what made the tenth invocation re-emit nine capsules: every
    leftover was reconsidered on every cold start, and a warm container's `/tmp` became a directory that grows with the
    number of invocations forever. Dry-run of that bug: 100 invocations → 5 050 callbacks and 28 s.
    """
    with contextlib.suppress(OSError):
        shutil.rmtree(os.path.join(root, "tape", writer_uuid), ignore_errors=True)


# ── the marker that survives a teardown ────────────────────────────────────


def _write_marker(root: str, recovered: list[dict[str, Any]], now_ns: int | None = None) -> str:
    """Write `invocation.json` before anything else. If a later cold start finds it, this invocation was destroyed."""
    marker = os.path.join(root, MARKER)
    document = {"schema": "blackbox.serverless/1", "pid": os.getpid(), "started_ns": now_ns or time.time_ns(),
                "recovered_from_previous": len(recovered)}
    temporary = marker + ".tmp"
    with contextlib.suppress(OSError):           # a marker that cannot be written is a weaker record, not a failed call
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(document, fh, sort_keys=True)
            fh.write("\n")
        os.replace(temporary, marker)
    return marker


def _remove_marker(marker: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(marker)


def lost_invocations(root: str) -> int:
    """How many invocations left a marker behind that nothing recovered — the `unknown_serverless` count, as a fact."""
    marker = os.path.join(root, MARKER)
    if not os.path.exists(marker):
        return 0
    return 1


def _emit(callback: Callable[[dict[str, Any]], None] | None, document: dict[str, Any]) -> None:
    if callback is None:
        return
    with contextlib.suppress(Exception):    # a callback that raises must not turn a seal into a failure
        callback(document)


def validate(result: Any) -> dict[str, Any]:
    """A tiny helper for tests and for a user's own asserts: the last sealed document, or a coded refusal."""
    if not isinstance(result, dict):
        raise bbx("BBX-E100", f"expected a seal document, got {type(result).__name__}")
    return result
