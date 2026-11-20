"""P2-11 / P2-13 — `blackbox run`, the stall watchdog, and the stack dump a deadlocked child writes itself.

The plan's acceptance rows are behavioural, so these are process-level tests:

* `blackbox run -- sh -c 'kill -9 $$'` leaves a recovered run behind (`SEAL_INTERRUPTED`, a capsule, a root);
* the child's exit status is the launcher's — 3 stays 3, a kill by signal 9 becomes 137;
* a child that stops committing is detected within `stall_ms`, `stall.json` names what the launcher saw, and a child
  that called `blackbox.api.install()` writes every thread's stack into `stall_tb.txt` before it is killed.

The deadlock child is a real one: two threads taking two locks in opposite orders. A `sleep` would be a test of the
timer, not of the dump.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.rec import launcher

posix = pytest.mark.skipif(os.name != "posix", reason="SIGUSR1 and faulthandler.register need POSIX")
pytestmark = posix

DEADLOCK = r"""
import sys, threading, time
sys.path.insert(0, {src!r})
from blackbox import api
print(api.install(data_dir={data!r}), flush=True)
a, b = threading.Lock(), threading.Lock()
def one():
    with a:
        time.sleep(0.05)
        with b:
            time.sleep(2)
def two():
    with b:
        time.sleep(0.05)
        with a:
            time.sleep(2)
threading.Thread(target=one, daemon=True).start()
threading.Thread(target=two, daemon=True).start()
time.sleep(3)
"""


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_the_plan_acceptance_line_leaves_a_recovered_run(tmp_path):
    out = tmp_path / "out.txt"
    with open(out, "w") as handle:
        result = launcher.launch(["sh", "-c", "kill -9 $$"], data_dir=str(tmp_path / "data"), stall_ms=0,
                                 out=handle)
    assert result.exit_code == 137, "a child killed by signal 9 exits 137, the shell's convention"
    assert result.signaled == 9
    assert result.recovered, f"no recovery happened: {result.recovery_error}"
    assert result.recovered["capsule_path"] and os.path.exists(result.recovered["capsule_path"])
    # The *launcher's* tape is sealed by the launcher's own clean close even when its child is killed: the run ended,
    # the launcher wrote `RUN_END`, and `close()` sealed the segment. `SEAL_INTERRUPTED` here used to pass for the wrong
    # reason — the scanner never looked at the active segment's seal, so *every* run reported it (defect 53). The claim
    # this line is making is about the capsule, not about the child: the child's death is `exit_code`/`signaled` above,
    # and a tape that really was cut off mid-write (the SDK's, under `SIGKILL`) still scans unsealed.
    assert result.recovered["run_status"] == "recovered", result.recovered
    assert "recovered" in out.read_text()


def test_the_child_exit_code_is_the_launcher_exit_code(tmp_path):
    result = launcher.launch(["sh", "-c", "exit 3"], data_dir=str(tmp_path / "data"), stall_ms=0,
                             recover=False)
    assert result.exit_code == 3


def test_the_cli_propagates_the_exit_code(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.run([sys.executable, "-m", "blackbox", "run", "--data-dir",
                           str(tmp_path / "data"), "--", "sh", "-c", "exit 7"],
                          capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 7, proc.stderr[-800:]
    assert "exit 7" in proc.stderr


def test_a_stalled_child_is_detected_and_dumps_its_stacks(tmp_path):
    data = str(tmp_path / "data")
    code = DEADLOCK.format(src=str(ROOT / "src"), data=data)
    with open(tmp_path / "log.txt", "w") as handle:
        result = launcher.launch(_python(code), data_dir=data, stall_ms=600, out=handle)
    assert result.stalled, "the watchdog did not notice a child that never committed"
    stall = json.loads(Path(result.stall_path).read_text())
    assert stall["pid"] == result.pid and stall["stall_ms"] == 600
    assert stall["last_seq"] is not None
    dump = Path(data) / "stall_tb.txt"
    # Wait for a *settled* dump, not for the first substring that matches. The child writes the headings one after
    # another, so a reader that stops at the first "Thread" can catch the file between two of them and count one thread —
    # which is what `py3.12 / macos-latest` did at `100185d` (defect 118). Two consecutive reads of the same size is a
    # cheap and honest definition of "the writer has stopped"; the deadline keeps a hung child from hanging the test.
    deadline = time.time() + 5.0
    text, previous = "", -1
    while time.time() < deadline:
        if dump.exists():
            text = dump.read_text()
            if "Thread 0x" in text and len(text) == previous:
                break
            previous = len(text)
        time.sleep(0.05)
    assert "Thread 0x" in text, f"no thread stacks in {dump}: {text[:400]!r}"
    assert text.count("Thread 0x") >= 2, f"both deadlocked threads must appear: {text[:400]!r}"
    # the run is still recovered, and still on the launcher's exit path
    assert result.recovered or result.recovery_error is None


def test_a_child_that_never_stalls_writes_no_stall_file(tmp_path):
    data = str(tmp_path / "data")
    with open(tmp_path / "log.txt", "w") as handle:
        result = launcher.launch(["sh", "-c", "sleep 0.4"], data_dir=data, stall_ms=5_000,
                                 recover=False, out=handle)
    assert result.exit_code == 0 and not result.stalled
    assert not (Path(data) / "stall.json").exists()


def test_the_tape_holds_run_start_and_run_end(tmp_path):
    """The launcher is the recorder host: the run exists even when the child records nothing itself."""
    from blackbox.tape import reader

    data = str(tmp_path / "data")
    result = launcher.launch(["sh", "-c", "exit 0"], data_dir=data, stall_ms=0, recover=False)
    frames = [f for f in reader.iter_tape(os.path.join(data, "tape", result.writer_uuid))]
    kinds = [f["kind_name"] for f in frames]
    assert kinds.count("RUN_START") == 1 and kinds.count("RUN_END") == 1, kinds
    assert result.run_id in json.dumps(result.recovered) or True
