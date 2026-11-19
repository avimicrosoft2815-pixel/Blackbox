"""`tools/desktop_journey.sh` — the harness a person with a Linux desktop runs, tested for the half that needs no screen.

The journey itself cannot happen here: this machine has no display, no GTK and no WebKit. What can be tested is that the
script
* refuses **before** pretending, on a machine with no display (one sentence, non-zero exit);
* records a real incident, lists it, verifies it and diagnoses it, and writes a journey file that says exactly which steps
  were automated and which it could not attempt;
* never writes a verdict better than what happened — `--automated-only` produces `automated-only`, not `pass`.

A harness that overstates is worse than no harness, because its output is what a release decision would rest on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "desktop_journey.sh"


def _env(tmp_path: Path, *, display: bool = False) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("BLACKBOX")}
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    if display:
        env["DISPLAY"] = ":0"
    # `blackbox` has to be on PATH, and it is this interpreter's console script that the journey must exercise.
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    env["BLACKBOX_HOME"] = str(tmp_path / "data")
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def test_the_script_is_valid_posix_shell() -> None:
    """It parses, it names the shell that runs it, and on a POSIX filesystem it is executable.

    The permission bit is checked only where a permission bit survives a checkout. Git on Windows does not store one, so
    asserting it there tests the checkout rather than the script — which is exactly how this test failed on all three
    Windows cells on 2026-11-16 (defect 92). Every invocation in this file and in `docs/desktop.md` is `sh <script>`, so
    the bit is a convenience, not the thing that makes it runnable.
    """
    completed = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert SCRIPT.read_text(encoding="utf-8").splitlines()[0] == "#!/bin/sh", "the shebang names the interpreter"
    if os.name == "posix":
        assert SCRIPT.stat().st_mode & 0o111, "on a POSIX filesystem the script is meant to be executable"


def test_it_refuses_on_a_machine_with_no_display_and_says_why(tmp_path: Path) -> None:
    completed = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, timeout=300,
                               env=_env(tmp_path), cwd=tmp_path)
    assert completed.returncode != 0
    assert "no display" in completed.stderr
    assert "--automated-only" in completed.stderr, "a refusal has to offer the way to do the rest"
    assert not list(tmp_path.glob("desktop-journey-*.json")), "a refused run must not leave a record"


@pytest.mark.skipif(os.environ.get("CI") == "true" and sys.platform != "linux",
                    reason="the journey is the Linux one; other platforms are recorded as unverified")
def test_automated_only_records_a_real_incident_and_never_claims_a_window(tmp_path: Path) -> None:
    out = tmp_path / "journey.json"
    completed = subprocess.run(["sh", str(SCRIPT), "--automated-only", "--out", str(out)],
                               capture_output=True, text=True, timeout=600, env=_env(tmp_path), cwd=tmp_path)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    record = json.loads(out.read_text())

    assert record["schema"] == "blackbox.desktop-journey/1"
    assert record["verdict"] == "automated-only", record["verdict"]
    assert record["machine"]["system"] == "Linux"
    # The recording really happened: a capsule exists, the sample failed on purpose, and the CLI agreed about it.
    assert record["capsule"] and Path(record["capsule"]).exists(), record["capsule"]
    assert record["automated"]["run_exit"] == 1, "the sample raises on purpose, so its exit code is 1"
    assert record["automated"]["verify_exit"] == "0"
    assert record["automated"]["diagnose_exit"] == "0"
    # Nothing about the window may be claimed: those answers are all "not-attempted".
    answers = {key: value for key, value in record["seen_by_a_person"].items() if key != "note"}
    assert set(answers.values()) == {"not-attempted"}, answers
    assert record["automated"]["window_exit"] == "skipped"
