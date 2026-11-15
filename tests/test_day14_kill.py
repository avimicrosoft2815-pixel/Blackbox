"""The day-14 rule (G-Capture): an unmodified `openai` script keeps its in-flight call across `kill -9`.

The plan's sentence is the one thing in this repository that is allowed to stop the build: *if day 14 does not work,
the honest move is to stop.* So this test is written to be hard to fool:

* **The script is unmodified.** `tests/_day14_child.py` builds `openai.OpenAI(...)` and iterates
  `chat.completions.create(stream=True)` exactly as the SDK documents; the recorder is installed around it, not
  woven into it. Nothing in the child knows what a seam is.
* **The death is real.** `SIGKILL` to its own pid: no atexit, no flush, no close, no chance to say goodbye.
* **The oracle is the server, not the recorder.** The mock provider counts the chunks and bytes it actually pushed
  down the socket. The tape may hold *less* than that (a frame in flight, a page not yet published) and must never
  hold *more* — a recorder that over-reports is worse than one that loses a chunk.
* **The evidence is what a recovery would read.** The parent runs `recovery.recover()` on the data directory and
  looks at the classified node, not at any in-memory counter of the child's.

Windows has no `SIGKILL`; there the suite skips and `docs/compatibility.md` says so.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from blackbox import constants as C
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.rec import recovery
from blackbox.tape import scanner

pytest.importorskip("openai")            # the script under test is the real SDK, not a stand-in
pytest.importorskip("anthropic")

ROOT = Path(__file__).resolve().parents[1]
CHILD = ROOT / "tests" / "_day14_child.py"
CHILD_ANTHROPIC = ROOT / "tests" / "_day14_child_anthropic.py"

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGKILL"),
                                reason="SIGKILL does not exist on Windows; the gate is POSIX-only by nature")


def _run_child(data_dir: Path, base_url: str, die_after: int, provider: MockProvider,
               child: Path = CHILD, content: str | None = "full") -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "BLACKBOX_HOME": str(data_dir)}
    env.pop("BLACKBOX_DISABLED", None)
    # The child passes no content mode, so the process policy decides: `full` for the byte-for-byte controls, and
    # unset for the test that runs the same kill under the documented default.
    if content is None:
        env.pop("BLACKBOX_CONTENT", None)
    else:
        env["BLACKBOX_CONTENT"] = content
    proc = subprocess.Popen([sys.executable, str(child), str(data_dir), base_url, str(die_after)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    # Wait until the server has actually pushed enough chunks that a kill lands mid-stream, then kill.
    deadline = time.monotonic() + 30
    while provider.sent_chunks < die_after and time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.005)
    if proc.poll() is None:
        proc.kill()
    out, _ = proc.communicate(timeout=30)
    return proc.returncode or 0, out or ""


def test_an_unmodified_openai_stream_keeps_its_in_flight_call_across_sigkill(tmp_path):
    scenario = Scenario(chunks=40, delay_ms=40, chunk_text="token ")
    with MockProvider(scenario) as provider:
        code, out = _run_child(tmp_path, provider.openai_base_url(), die_after=5, provider=provider)
        sent_chunks, sent_bytes = provider.sent_chunks, provider.sent_bytes

    assert code == -signal.SIGKILL, f"the child was supposed to be killed, got {code}: {out[-800:]}"
    assert "SEAM=" in out, out[-800:]
    assert sent_chunks >= 5 and sent_bytes > 0

    result = recovery.recover(str(tmp_path))
    assert result.node_statuses, "recovery found no nodes at all"
    node = next(n for n in result.node_statuses if n["status"] == recovery.INTERRUPTED_MID_STREAM)
    assert node["model_requests"] == 1

    # The numbers a reader of the capsule sees, against the server's own tally.
    assert node["chunks"] >= 1, "no chunk reached the tape; the sink is not writing during the stream"
    assert node["partial_stream_bytes"] > 0
    assert node["partial_stream_bytes"] <= sent_bytes, "the tape claims more bytes than the provider ever sent"
    assert node["chunks"] <= sent_chunks, "the tape claims more chunk frames than the provider sent chunks"
    assert "chunk" in node["detail"] and "of an unknown total" in node["detail"]


def test_the_stream_that_is_not_killed_is_recorded_whole(tmp_path):
    """The control for the test above: same script, no kill. If this one did not record the whole stream, the
    interrupted case would prove nothing about the seam — only about the child dying early."""
    scenario = Scenario(chunks=6, delay_ms=1)
    with MockProvider(scenario) as provider:
        code, out = _run_child(tmp_path, provider.openai_base_url(), die_after=10_000, provider=provider)
        sent_bytes = provider.sent_bytes

    assert code == 0, out[-800:]
    assert "SURVIVED" in out

    candidates = list((tmp_path / "tape").iterdir())
    assert len(candidates) == 1
    scan = scanner.scan(str(tmp_path), candidates[0].name)
    model_frames = [f for f in scan.frames if f["kind"] == C.KIND_MODEL_CHUNK]
    response = [f for f in scan.frames if f["kind"] == C.KIND_MODEL_RESPONSE]
    assert response, "a completed stream must end with MODEL_RESPONSE"
    import json

    summary = json.loads(response[0]["payload"])
    assert summary["bytes_received"] == sum(len(f["payload"]) for f in model_frames)
    assert summary["bytes_received"] <= sent_bytes
    assert summary["status"] == 200 and summary["error"] is None


def test_the_capsule_says_which_seam_captured_it(tmp_path):
    """`capture_seam` has to survive the process, or a capsule cannot be read honestly after the fact."""
    import json

    scenario = Scenario(chunks=4, delay_ms=1)
    with MockProvider(scenario) as provider:
        code, out = _run_child(tmp_path, provider.openai_base_url(), die_after=10_000, provider=provider)
    assert code == 0, out[-800:]

    uuid = next((tmp_path / "tape").iterdir()).name
    scan = scanner.scan(str(tmp_path), uuid)
    request = json.loads(next(f["payload"] for f in scan.frames if f["kind"] == C.KIND_MODEL_REQUEST))
    assert request["capture_seam"] in {"httpx-hooks", "httpx-transport", "sdk-methods"}
    assert request["coalesce_bytes"] == 8 * 1024
    assert request["model"] == "mock-1"


# ── the same experiment against `anthropic`, because "it works for openai" is a fact about openai ───────────


def test_an_unmodified_anthropic_stream_keeps_its_in_flight_call_across_sigkill(tmp_path):
    """G-Capture's zero-code-change row names **both** SDKs. This is the second half of it.

    The child is an unmodified `anthropic` script — `messages.create(stream=True)`, iterated as the SDK documents —
    killed with `SIGKILL` five events in. The oracle is again the server's own tally, not the recorder's.
    """
    scenario = Scenario(chunks=40, delay_ms=40, chunk_text="token ")
    with MockProvider(scenario) as provider:
        code, out = _run_child(tmp_path, provider.anthropic_base_url(), die_after=5, provider=provider,
                               child=CHILD_ANTHROPIC)
        sent_chunks, sent_bytes = provider.sent_chunks, provider.sent_bytes

    assert code == -signal.SIGKILL, f"the child was supposed to be killed, got {code}: {out[-800:]}"
    assert "SEAM=" in out, out[-800:]
    assert sent_chunks >= 5 and sent_bytes > 0

    result = recovery.recover(str(tmp_path))
    assert result.node_statuses, "recovery found no nodes at all"
    node = next(n for n in result.node_statuses if n["status"] == recovery.INTERRUPTED_MID_STREAM)
    assert node["model_requests"] == 1
    assert node["chunks"] >= 1, "no chunk reached the tape; the sink is not writing during the stream"
    assert 0 < node["partial_stream_bytes"] <= sent_bytes
    assert node["chunks"] <= sent_chunks


def test_the_anthropic_stream_that_is_not_killed_is_recorded_whole(tmp_path):
    """The control: without it, the interrupted case proves only that the child died."""
    import json

    with MockProvider(Scenario(chunks=6, delay_ms=1)) as provider:
        code, out = _run_child(tmp_path, provider.anthropic_base_url(), die_after=10_000, provider=provider,
                               child=CHILD_ANTHROPIC)
        sent_bytes = provider.sent_bytes

    assert code == 0, out[-800:]
    assert "SURVIVED events=" in out

    uuid = next((tmp_path / "tape").iterdir()).name
    scan = scanner.scan(str(tmp_path), uuid)
    request = json.loads(next(f["payload"] for f in scan.frames if f["kind"] == C.KIND_MODEL_REQUEST))
    assert request["path"] == "/v1/messages", "the anthropic call was recorded as something else"
    assert request["stream"] is True and request["model"] == "mock-1"
    assert request["capture_seam"] in {"httpx-hooks", "httpx-transport", "sdk-methods"}

    summary = json.loads(next(f["payload"] for f in scan.frames if f["kind"] == C.KIND_MODEL_RESPONSE))
    chunks = [f for f in scan.frames if f["kind"] == C.KIND_MODEL_CHUNK]
    assert summary["bytes_received"] == sum(len(f["payload"]) for f in chunks) <= sent_bytes
    assert summary["status"] == 200 and summary["error"] is None
    assert summary["redaction"]["counts"] == {}, "the mock streams no secrets; a redaction count would be a false positive"


def test_the_two_sdks_take_the_same_seam_and_the_same_shape(tmp_path):
    """The row says "zero code changes" for both SDKs; this is what makes the two halves comparable."""
    with MockProvider(Scenario(chunks=4, delay_ms=1)) as provider:
        openai_code, openai_out = _run_child(tmp_path / "openai", provider.openai_base_url(), die_after=10_000,
                                             provider=provider)
        anthropic_code, anthropic_out = _run_child(tmp_path / "anthropic", provider.anthropic_base_url(),
                                                  die_after=10_000, provider=provider, child=CHILD_ANTHROPIC)

    assert openai_code == 0 and anthropic_code == 0, (openai_out[-400:], anthropic_out[-400:])
    seams = {line for out in (openai_out, anthropic_out) for line in out.splitlines() if line.startswith("SEAM=")}
    assert len(seams) == 1, f"the two SDKs did not take the same seam: {seams}"
