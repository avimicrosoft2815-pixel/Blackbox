"""§24a's attach row, the half the readiness index could not find: `run -- curl …` with a `kill -9` mid-stream.

The row reads *"`run -- node app.js`, `-- python app.py`, `-- curl …`, `-- claude` each produce a run with the in-flight
call preserved after `kill -9`, zero changes"*. Node and both SDKs were measured (`bench/accept_p3b03.py`); `curl` was
recorded as **not found** by the review. This file is that run.

`curl` is the case with no seam at all: no SDK, no hook, no instrumentation of the client — the proxy is the only
vantage, so what survives a `SIGKILL` is exactly what the proxy forwarded before the child died. The oracle is the mock
provider's own tally of chunks and bytes it pushed, the same rule the SDK tests use: the tape may hold less than the
server sent and must never hold more.

POSIX only, for the same reason as the SDK tests: Windows has no `SIGKILL`.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from blackbox.cap import format as cap
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.rec import recovery

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGKILL"),
                                reason="SIGKILL does not exist on Windows; the row is POSIX-only by nature")

ROOT = Path(__file__).resolve().parents[1]

# The child is a shell, because `curl` is not a Python program and the point of the row is that nothing is written into
# the app: the launcher exports the base URL, curl reads it, and the shell kills its own process group mid-stream. The
# `kill -9 -$$` reaches curl because the shell is the group leader the launcher started, and it does not reach the
# launcher, which is why the launcher can still print the recovery summary.
#
# When it fires is a *fact about the stream*, not about the clock: the watcher waits for a file the test touches once the
# mock provider's own tally says chunks are moving. The first version slept a fixed 0.6 s, and on a loaded py3.12 Ubuntu
# runner the kill landed before `curl` had sent anything, so the tape held no call at all and the test failed with
# `no in-flight call survived: []` — a race in the test, not a hole in the product.
CHILD = """\
set -eu
( while [ ! -f "$BBX_KILL_WHEN" ]; do sleep 0.05; done; kill -9 -$$ 2>/dev/null || kill -9 $$ ) &
curl -sS -N -H 'content-type: application/json' -d '{"model":"gpt-4","stream":true,"messages":[{"role":"user","content":"hi"}]}' \\
  "$OPENAI_BASE_URL/chat/completions" >"$BBX_OUT"
"""


def _run_and_kill_mid_stream(data_dir: Path, provider: MockProvider, *, min_chunks: int = 3,
                             deadline_s: float = 60.0) -> subprocess.CompletedProcess:
    """Start the child, wait until the origin has pushed real chunks AND curl has received bytes from the proxy,
    then let the child SIGKILL its own group."""
    flag = data_dir / "kill-mid-stream.flag"
    flag.unlink(missing_ok=True)
    out_file = data_dir / "curl-out.bin"
    out_file.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "BLACKBOX_HOME": str(data_dir),
           "BBX_KILL_WHEN": str(flag), "BBX_OUT": str(out_file)}
    env.pop("BLACKBOX_DISABLED", None)
    env["BLACKBOX_CONTENT"] = "full"
    proc = subprocess.Popen(
        [sys.executable, "-m", "blackbox", "run", "--data-dir", str(data_dir),
         "--upstream", f"openai={provider.openai_base_url()}@/openai", "--json", "--", "sh", "-c", CHILD],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    give_up = time.monotonic() + deadline_s
    try:
        while ((provider.sent_chunks < min_chunks or not out_file.exists() or out_file.stat().st_size == 0)
               and time.monotonic() < give_up and proc.poll() is None):
            time.sleep(0.01)
    finally:
        # Touched either way: if the stream never started the test has to fail with the launcher's own output rather than
        # with a child that hangs for ninety seconds.
        flag.write_text("go", encoding="utf-8")
    out, err = proc.communicate(timeout=90)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _frame_kinds(data_dir: Path) -> dict[str, int]:
    """What the capsules in `data_dir` actually contain, for a failure message that has to be self-explanatory.

    A capsule's entries are named `<vantage>/<kind>/…`; counting the kinds is enough to tell "the proxy wrote a request and
    three chunks" from "the tape is empty", which is the difference the assertion above cannot show on its own.
    """
    counts: dict[str, int] = {}
    for capsule in sorted((Path(data_dir) / "capsules").glob("*.bbx")):
        parsed = cap.read_capsule(str(capsule))
        for name in parsed.entries:
            kind = name.rsplit("/", 1)[-1]
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def test_a_curl_stream_killed_mid_flight_keeps_what_the_proxy_forwarded(tmp_path):
    scenario = Scenario(chunks=40, delay_ms=60, chunk_text="token ")
    with MockProvider(scenario) as provider:
        completed = _run_and_kill_mid_stream(tmp_path, provider)
        sent_chunks, sent_bytes = provider.sent_chunks, provider.sent_bytes

    assert sent_chunks >= 3 and sent_bytes > 0, f"the origin pushed nothing to forward: {sent_chunks} chunks"
    # `blackbox run` hands the child's exit code back, so a killed child is 137 here and the launcher itself is fine:
    # the line below it in stderr is the recovery summary, and the capsule it names exists.
    assert completed.returncode in (137, -signal.SIGKILL), (
        f"the launcher must pass the child's death through, not fail its own way:\n{completed.stderr[-1200:]}")
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    assert summary["recovered"]["capsules"] >= 1, summary
    assert summary["exit_code"] in (137, -signal.SIGKILL), (
        f"the child was killed by SIGKILL, so the run records it, not {summary['exit_code']}")

    result = recovery.recover(str(tmp_path))
    interrupted = [n for n in result.node_statuses if n["status"] == recovery.INTERRUPTED_MID_STREAM]
    if not interrupted:
        # Defect 119: `py3.11 / macos-latest` failed here with an *empty* list, on a run whose origin had pushed chunks and
        # whose launcher reported a capsule. An empty list cannot tell "the tape held no model frames" from "the archive
        # reported none", so the message below carries both, plus how much the proxy actually forwarded.
        raise AssertionError(
            f"no in-flight call survived: statuses={[n['status'] for n in result.node_statuses]}, "
            f"origin pushed {sent_chunks} chunks / {sent_bytes} bytes, "
            f"launcher summary={completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else '<none>'}, "
            f"capsules={_frame_kinds(tmp_path)}, launcher stderr tail={completed.stderr[-400:]!r}"
        )
    node = interrupted[0]
    assert node["model_requests"] == 1, node
    assert node["chunks"] >= 1, "the proxy forwarded chunks and the tape recorded none of them"
    assert node["partial_stream_bytes"] > 0
    assert node["partial_stream_bytes"] <= sent_bytes, "the tape claims more bytes than the origin ever pushed"
    assert node["chunks"] <= sent_chunks, "the tape claims more chunks than the origin sent"


def test_the_capsule_from_a_killed_curl_run_verifies(tmp_path):
    """A recovered capsule is only evidence if it passes its own integrity check — the reader's first question."""
    scenario = Scenario(chunks=40, delay_ms=60, chunk_text="token ")
    with MockProvider(scenario) as provider:
        completed = _run_and_kill_mid_stream(tmp_path, provider, min_chunks=2)
        assert completed.returncode in (137, -signal.SIGKILL), completed.stderr[-1200:]
        capsules = sorted((tmp_path / "capsules").glob("*.bbx"))
        assert capsules, "no capsule after a killed curl run"
        verified = subprocess.run(
            [sys.executable, "-m", "blackbox", "verify", *[str(c) for c in capsules]],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "BLACKBOX_HOME": str(tmp_path)},
        )
    assert verified.returncode == 0, f"verify exited {verified.returncode}:\n{verified.stdout[-1500:]}\n{verified.stderr[-800:]}"
    assert "verified" in (verified.stdout + verified.stderr).lower()
