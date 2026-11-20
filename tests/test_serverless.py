"""P8-11 — the serverless wrapper: one run per invocation, sealed at the boundary, nothing left open (§11.11).

What a serverless platform does to a recorder is freeze the process, then destroy it. So the properties that matter are
not "does it record" but:

* **after every invocation there is no open run** — a capsule or a recorded failure, never a tape that nobody sealed;
* **an invocation that was killed is recovered by the next one**, and says so (`completeness="unknown_serverless"`),
  rather than vanishing into a gap that a reader would read as "nothing happened";
* **a handler that raises still seals**, and re-raises: the recorder does not eat the application's exception;
* **a sandbox that was torn down is a fact the next cold start records**, because a marker file is the only thing that
  can survive it — and the wrapper does not pretend otherwise.

The breadth test here runs 250 invocations with random exceptions and simulated timeouts, which is 500 s of measured
`tests/bench` time saved: the full 1 000-invocation run is `bench/serverless_1000.py` and its numbers are committed in
`bench/results/2026-11-16-serverless-1000.json`. Same code, same assertions, more invocations.
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import api, serverless
from blackbox.cap import format as cap
from blackbox.tape.writer import TapeWriter


def _dirs(tmp_path: Path) -> str:
    return str(tmp_path / "data")


def test_an_invocation_is_sealed_at_the_boundary(tmp_path: Path) -> None:
    documents: list[dict] = []

    @serverless.wrap(on_capsule=documents.append, data_dir=_dirs(tmp_path))
    def handler(event, context):
        return f"ok:{event}"

    assert handler("a", None) == "ok:a"
    (document,) = documents
    assert document["completeness"] == "sealed" and document["error"] is None
    assert document["capsule"] and Path(document["capsule"]).exists()
    capsule = cap.read_capsule(document["capsule"])
    assert capsule.root_ok, capsule.problems
    assert not list(Path(tmp_path, "data", "tape").glob("*")), "the tape is gone once it is a capsule"
    assert not Path(tmp_path, "data", serverless.MARKER).exists(), "a sealed invocation leaves no marker"


def test_an_exception_still_seals_and_is_re_raised(tmp_path: Path) -> None:
    documents: list[dict] = []

    @serverless.wrap(on_capsule=documents.append, data_dir=_dirs(tmp_path))
    def handler(event, context):
        raise RuntimeError("the platform timed out")

    with pytest.raises(RuntimeError, match="timed out"):
        handler("a", None)
    (document,) = documents
    assert document["completeness"] == "sealed"
    assert document["invocation_error"] == "RuntimeError: the platform timed out"
    assert document["re_raised"] is True
    assert cap.read_capsule(document["capsule"]).root_ok


def test_a_killed_invocation_is_recovered_by_the_next_one_and_says_it_was(tmp_path: Path) -> None:
    """A timeout kills the process mid-handler. The bytes are in `/tmp`; the next cold start must seal them."""
    data = _dirs(tmp_path)
    os.makedirs(data, mode=0o700, exist_ok=True)
    # Exactly what a killed invocation leaves: a tape, no capsule, and the marker written at invoke time.
    writer = TapeWriter(data, mode="serverless").open()
    writer.emit(0x50, b"\x00")                                        # RUN_START, payload is not the point here
    writer.flush()
    writer.close()
    Path(data, serverless.MARKER).write_text(json.dumps({"schema": "blackbox.serverless/1", "pid": 999_999}) + "\n")
    assert serverless.lost_invocations(data) == 1

    documents: list[dict] = []

    @serverless.wrap(on_capsule=documents.append, data_dir=data)
    def handler(event, context):
        return "warm again"

    assert handler("b", None) == "warm again"
    assert len(documents) == 2, documents
    recovered, current = documents
    assert recovered["recovered"] is True
    assert recovered["capsule"] and Path(recovered["capsule"]).exists()
    assert recovered["completeness"] in ("unknown_serverless", "recovered"), recovered
    assert current["completeness"] == "sealed"
    assert serverless.lost_invocations(data) == 0, "the marker is removed once the invocation sealed"
    assert not list(Path(data, "tape").glob("*"))


def test_the_defaults_are_the_published_ones() -> None:
    from blackbox import constants as C

    assert C.SERVERLESS_SEGMENT_BYTES == 2 * 1024 * 1024
    assert "serverless" in C.MODES
    assert serverless.default_data_dir().endswith(os.path.join("tmp", "blackbox")) or \
        serverless.default_data_dir().endswith(os.path.join("tmp", "blackbox"))
    seen = {}

    @serverless.wrap(on_capsule=lambda document: seen.update(document))
    def handler(event, context):                       # no data_dir: the default
        return "ok"

    assert handler(1, None) == "ok"
    assert handler.data_dir == os.path.abspath(serverless.default_data_dir())
    assert seen["completeness"] == "sealed"


def test_a_recorded_model_call_inside_the_handler_lands_in_the_invocation_capsule(tmp_path: Path) -> None:
    """The seams are installed for the invocation, so a call the handler makes is *this* invocation's evidence."""
    documents: list[dict] = []

    @serverless.wrap(on_capsule=documents.append, data_dir=_dirs(tmp_path))
    def handler(event, context):
        status = api.capture_status()
        api.mark("inside-handler", note="a mark from the handler")
        return status

    status = handler(1, None)
    assert status["attempts"], "the wrapper attempts a seam on every invocation"
    capsule = cap.read_capsule(documents[0]["capsule"])
    assert any(name.startswith("events/seg-") for name in capsule.entries)


@pytest.mark.slow_budget(300)     # 250 seals is ~40 s here and ~3 minutes on a Windows runner: a measurement, not a hang
def test_250_invocations_with_random_failures_leave_no_open_run(tmp_path: Path) -> None:
    documents: list[dict] = []
    rng = random.Random(20261116)  # noqa: S311 - reproducible failures in a test, not a security decision
    outcomes = {"ok": 0, "raised": 0}

    @serverless.wrap(on_capsule=documents.append, data_dir=_dirs(tmp_path))
    def handler(event, context):
        roll = rng.random()
        if roll < 0.20:
            raise TimeoutError(f"invocation {event} exceeded its 3 s limit")     # a timeout, from inside
        if roll < 0.40:
            raise ValueError(f"invocation {event} got a bad payload")
        if roll < 0.50:
            return None                                                     # a function that returns nothing
        return {"echo": event}

    for invocation in range(250):
        try:
            handler(invocation, None)
            outcomes["ok"] += 1
        except (TimeoutError, ValueError):
            outcomes["raised"] += 1

    assert outcomes["ok"] + outcomes["raised"] == 250
    assert len(documents) == 250, f"one document per invocation, got {len(documents)}"
    assert all(document["completeness"] == "sealed" for document in documents), \
        [d["completeness"] for d in documents if d["completeness"] != "sealed"][:5]
    assert all(document["capsule"] and Path(document["capsule"]).exists() for document in documents)
    data = Path(tmp_path, "data")
    assert not list(Path(data, "tape").glob("*")), "no tape is left open after any invocation"
    assert not Path(data, serverless.MARKER).exists()
    assert len(list(Path(data, "capsules").glob("*.bbx"))) == 250
    # The evidence is readable: a sample of the capsules verifies against their own roots.
    for document in documents[::25]:
        assert cap.read_capsule(document["capsule"]).root_ok
    recovered = [document for document in documents if document.get("recovered")]
    assert recovered == [], "with every invocation sealing, nothing is left for the next one to recover"


def test_a_failing_callback_does_not_fail_the_invocation(tmp_path: Path) -> None:
    def explode(document: dict) -> None:
        raise RuntimeError("the shipper is down")

    @serverless.wrap(on_capsule=explode, data_dir=_dirs(tmp_path))
    def handler(event, context):
        return "still fine"

    assert handler(1, None) == "still fine"


def test_the_wrapper_is_fail_open_when_the_data_directory_cannot_be_used(tmp_path: Path) -> None:
    """Recording is never allowed to be the reason a request fails: the handler runs, the wrapper says why."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this is a file\n")
    documents: list[dict] = []

    @serverless.wrap(on_capsule=documents.append, data_dir=str(blocker))
    def handler(event, context):
        return "ran anyway"

    assert handler(1, None) == "ran anyway"
    (document,) = documents
    assert document["completeness"] == "not_recorded"
