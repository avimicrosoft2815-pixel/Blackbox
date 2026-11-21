"""The workflow that would build and run the Windows installer, checked here so it cannot rot unnoticed.

B9 says no Windows installer has been produced or run. The build environment that writes `tools/package/windows/` has no
Windows, no `wine`, no Inno Setup and no PowerShell, so the definition was reviewed and never compiled
(`tests/test_windows_definition.py` checks that it would compile). This file checks the next step exists in the tree: a
GitHub workflow that runs on a **real Windows machine**, stages the tree, compiles the installer with Inno Setup, installs
it silently, runs the installed copy, uninstalls it, and checks that the recordings are still there.

Two things this file is careful about:

* it does **not** claim the workflow passed — a test can read a file, not a runner. The evidence is the run itself, and
  `docs/desktop.md` stays unverified until somebody links one;
* it checks the workflow refuses to imply a window was opened. A runner has no interactive desktop, so a workflow whose
  summary said "desktop verified" would be the kind of overstatement that makes every other row harder to believe.

Text-level checks rather than a YAML parse, because `pyyaml` is not a dependency of this project and adding one for a
single test is a worse trade than reading the file the way a reviewer would.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "package-windows.yml"
ISS = ROOT / "tools" / "package" / "windows" / "blackbox.iss"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_the_workflow_exists_and_can_be_dispatched_by_hand() -> None:
    assert WORKFLOW.is_file(), "the Windows packaging workflow is missing"
    text = _text()
    assert "workflow_dispatch" in text, "it has to be runnable without a commit, so a person can ask for an installer"
    assert "runs-on: windows-latest" in text, "a Linux runner cannot compile an Inno Setup script"
    assert "fetch-depth: 0" in text, (
        "a depth-1 clone breaks the suite's ancestry and commit-message checks (C35), which this job runs")
    assert "tools/package/windows/**" in text, "a change to the packaging should rebuild it"


def test_it_builds_with_the_committed_definition_rather_than_a_second_one() -> None:
    text = _text()
    assert ISS.is_file(), "blackbox.iss is the definition; the workflow must compile that file"
    assert "blackbox.iss" in text
    assert re.search(r"ISCC\.exe", text), "Inno Setup's compiler is `ISCC.exe`"
    assert "choco install innosetup" in text, "the runner needs the Inno Setup 6 package"
    assert "/DAppVersion=" in text, "the definition takes the version as a define, so it is not hard-coded twice"


def test_it_runs_the_staged_tree_and_the_installed_one_on_windows() -> None:
    """The row B9 asks for is *on Windows*: a staged tree that records, and an installed tree that answers."""
    text = _text()
    assert "--version" in text and "doctor --json" in text, "the staged and the installed trees both have to answer"
    assert "run --data-dir" in text and " verify " in text, (
        "a version string is not a recording: the job records a real run and verifies its capsule")
    assert text.count("::error::") >= 3, (
        "job logs are unreachable from the environment that wrote this, so every check has to announce its own failure "
        "through a workflow command; the first attempt failed with only 'exit code 1'")
    assert "/VERYSILENT" in text, "the install has to be unattended on a runner"
    assert re.search(r"unins\*?\.exe", text), "the uninstaller has to be the installed one, not a guess"
    assert "recordings" in text.lower(), "the uninstaller's promise about recordings is the row worth checking"


def test_it_says_what_it_does_not_prove() -> None:
    text = _text().lower()
    assert "does not open the window" in text or "not open the window" in text, (
        "a runner has no interactive desktop; the job must say so rather than let a green cell imply a window")
    assert "unverified" in text, "docs/desktop.md keeps the window row unverified, and the job should say so"
