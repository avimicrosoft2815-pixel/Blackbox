"""P3B-03's remainder: the Python auto-import, and the four flags that decide what a child is given.

§11.9 step 4 asks for *"an auto-import (`.pth` in a temp site dir / `PYTHONPATH`, the `opentelemetry-instrument`
technique) so `install()` runs without code changes; `--no-sdk` disables."* Two words in that sentence carry all the
risk, and each has a test that would fail without it:

* **temp** — the directory is created per run and deleted when the child exits. `test_nothing_of_the_auto_import_
  survives_the_run` fails if anything is written outside the temp dir, and fails if the dir outlives the child, which
  is what "temporary" means when the alternative is editing a user's interpreter.
* **for Python commands** — `test_a_shell_child_is_given_no_pythonpath_at_all` fails if the launcher hands a `.pth`
  to a child that will never read it.

The rest of the file is the flag surface the plan names (`--no-sdk`, `--force-proxy`, `--no-hooks`, `--no-proxy`), and
each test asserts the *effect the flag claims*, not that the parser accepted the word: a `--force-proxy` test that only
checked the flag was parsed would have passed on the code before this ticket, where the flag did not exist.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from blackbox.cap import format as cap
from blackbox.rec import launcher, recovery

# The child reports what it can see into a file: its stdout belongs to it, and a test that reads the parent's stdout
# would be measuring pytest's capture rather than the child's environment.
REPORT = """
import json, os, sys
report = {
    "pythonpath": os.environ.get("PYTHONPATH", ""),
    "blackbox_sdk": os.environ.get("BLACKBOX_SDK"),
    "inbox": os.environ.get("BLACKBOX_INBOX"),
    "openai_base_url": os.environ.get("OPENAI_BASE_URL"),
    "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL"),
    "blackbox_proxy": os.environ.get("BLACKBOX_PROXY"),
    "autoimport_ran": "blackbox.cap_sdk.autoimport" in sys.modules,
}
try:
    import blackbox.api as api
    report["sdk_enabled"] = api.enabled()
except Exception as exc:
    report["sdk_enabled"] = f"import failed: {exc}"
json.dump(report, open(sys.argv[1], "w"))
"""


def _child(tmp_path: Path, script: str = REPORT) -> list[str]:
    return [sys.executable, "-c", script, str(tmp_path / "report.json")]


def _report(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "SYSTEMROOT", "TMPDIR", "TEMP")}
    env.update(extra)
    return env


def _sdk_dirs() -> set[str]:
    root = tempfile.gettempdir()
    return {str(p) for p in Path(root).glob("blackbox-sdk-*")}


# A child that dies as hard as the platform allows. Windows has no `SIGKILL` (accessing it is an `AttributeError`,
# which is what turned this test into a failure with exit 1 on all three Windows cells), so the signal is chosen by the
# platform and the *expectation* follows it: POSIX reports 128+n, Windows has no such convention and the launcher
# reports the status the platform gave it.
KILL_SELF = """
import os, signal

os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))
"""


def _kill_exit_code() -> int:
    """The status a self-killed child leaves, as the platform reports it.

    POSIX has the 128+n convention, so `SIGKILL` is 137. Windows has neither `SIGKILL` nor the convention: `os.kill`
    there is `TerminateProcess(handle, sig)`, so the exit code *is* the signal number — CI reported `exit 15` for the
    `SIGTERM` fallback on all three Windows cells (run 43885473662), and that is the number used here rather than a
    guess. The first version of this helper said `1` and was wrong in exactly the way this file exists to prevent: a
    number that looked like a measurement.
    """
    return 137 if os.name == "posix" else int(getattr(signal, "SIGTERM", 15))


def _shell_child(tmp_path: Path) -> tuple[list[str], Path | None]:
    """A non-Python command for the "no PYTHONPATH for a shell" test, and where it reports from, if it can.

    On POSIX the child genuinely reads its own environment and writes it to a file — the strongest form of the check.
    On Windows that is not worth the contortions: a quoted redirect path inside a `cmd /c` command line goes through
    `list2cmdline`, whose backslash-escaped quotes are *not* what `cmd.exe` parses, and the file lands somewhere else
    (that is the `FileNotFoundError` this test produced on all three Windows cells in run 43885473662). So on Windows the
    command is a plain `cmd /c exit 0` and the assertion is made against the environment the launcher actually passes to
    `Popen` — which is the property under test, observed directly instead of through a shell.
    """
    out = tmp_path / "pp.txt"
    if os.name == "posix":
        script = 'printf "%s" "${PYTHONPATH-unset}" > "$1"'
        return ["/bin/sh", "-c", script, "sh", str(out)], out
    return ["cmd", "/c", "exit 0"], None


# ── the auto-import ────────────────────────────────────────────────────────────────────────────────────────────────
def test_sitecustomize_installs_the_capture_layer_without_the_application_importing_anything(tmp_path):
    """The whole point: the child's code says nothing about blackbox and is recorded anyway."""
    data_dir = tmp_path / "data"
    before = _sdk_dirs()
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch(_child(tmp_path), data_dir=str(data_dir), out=out, proxy=False)
    report = _report(tmp_path)
    assert result.exit_code == 0
    assert report["autoimport_ran"] is True, "the sitecustomize did not run before __main__"
    assert report["sdk_enabled"] is True, "autoimport ran but the recorder is not installed"
    assert report["blackbox_sdk"] == "1"
    assert result.sdk["enabled"] is True
    assert result.sdk["site_dir"] and "blackbox-sdk-" in result.sdk["site_dir"]
    assert result.sdk["file"] == "sitecustomize.py" and "sitecustomize" in result.sdk["mechanism"]
    # ... and the child left a tape of its own, which is the observable consequence on disk
    entries = recovery.discover.discover(str(data_dir))
    assert len(entries) >= 2, "expected the launcher's tape and the child's, got " + repr([e.writer_uuid for e in entries])
    assert _sdk_dirs() == before, "a temporary site directory was left behind"


def test_nothing_of_the_auto_import_survives_the_run(tmp_path):
    """Temporary means gone: the directory the child was told about no longer exists, and no new one does."""
    data_dir = tmp_path / "data"
    before = _sdk_dirs()
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch(_child(tmp_path), data_dir=str(data_dir), out=out, proxy=False)
    site_dir = result.sdk["site_dir"]
    assert site_dir is not None and not Path(site_dir).exists(), f"{site_dir} outlived the child"
    assert result.sdk.get("removed") is True
    assert _sdk_dirs() == before
    # Nothing was written into the interpreter that ran the child: the package root is untouched and read-only to us.
    package_root = Path(result.sdk["package_root"])
    assert not list(package_root.glob("*.pth")), "the launcher must never write a .pth beside the package"
    assert not list((package_root / "blackbox").glob("*.pth"))


def test_the_temp_dir_is_removed_even_when_the_child_is_killed(tmp_path):
    """A child killed with no chance to run a handler is the case that leaves litter in a naive implementation.

    The exit code is asserted as the platform defines it — 137 on POSIX (`128 + SIGKILL`), the platform's own status
    on Windows, which has no such convention. Asserting 137 everywhere is what made this test fail on all three
    Windows cells; the property under test (the directory is gone) is the same on both.
    """
    data_dir = tmp_path / "data"
    before = _sdk_dirs()
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch([sys.executable, "-c", KILL_SELF], data_dir=str(data_dir), out=out, proxy=False)
    assert result.exit_code == _kill_exit_code(), f"exit {result.exit_code} (os.name={os.name})"
    assert result.exit_code != 0, "a killed child must not report success"
    assert result.sdk["removed"] is True, result.sdk.get("cleanup_error")
    assert not Path(result.sdk["site_dir"]).exists()
    assert _sdk_dirs() == before


def test_an_existing_sitecustomize_is_chained_not_replaced(tmp_path):
    """`site` imports one module by that name; a launcher that wins the name silently disables whoever had it.

    A distro hook, a virtualenv, another agent's instrumentation — all of them are a `sitecustomize.py` somewhere on
    `sys.path`. The temp directory goes first, so ours is the one `site` imports; `delegate()` therefore runs the next
    one it finds before installing, and both effects are visible in this child.
    """
    other = tmp_path / "other"
    other.mkdir()
    marker = tmp_path / "other-ran.txt"
    (other / "sitecustomize.py").write_text(
        "import os\nfrom pathlib import Path\nPath(os.environ['OTHER_MARKER']).write_text('ran')\n",
        encoding="utf-8")
    env = _env(PYTHONPATH=str(other), OTHER_MARKER=str(marker))
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch(_child(tmp_path), data_dir=str(tmp_path / "data"), out=out, proxy=False, env=env)
    report = _report(tmp_path)
    assert marker.exists(), "the sitecustomize that was already on PYTHONPATH never ran"
    assert result.sdk["enabled"] is True
    assert report["autoimport_ran"] is True and report["sdk_enabled"] is True


def test_no_sdk_leaves_the_child_exactly_as_it_found_it(tmp_path):
    data_dir = tmp_path / "data"
    before = _sdk_dirs()
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch(_child(tmp_path), data_dir=str(data_dir), out=out, proxy=False, sdk=False)
    report = _report(tmp_path)
    assert report["autoimport_ran"] is False
    assert report["blackbox_sdk"] is None
    assert "blackbox-sdk-" not in report["pythonpath"]
    assert result.sdk["enabled"] is False and result.sdk["reason"] == "--no-sdk"
    assert _sdk_dirs() == before


def test_a_shell_child_is_given_no_pythonpath_at_all(tmp_path, monkeypatch):
    """A `sitecustomize` is a file only Python reads; exporting it to a shell is noise and a lie about what happened.

    The environment handed to `Popen` is captured and checked on both platforms, and on POSIX the child's own view is
    checked as well. Neither `/bin/sh` nor a `cmd.exe` redirect is portable, so the portable assertion is the one that
    observes what the launcher passes, not what a shell made of it.
    """
    data_dir = tmp_path / "data"
    command, report = _shell_child(tmp_path)
    seen: dict = {}
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(launcher.subprocess, "Popen", spy)
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch(command, data_dir=str(data_dir), out=out, proxy=False, env=_env())
    assert result.sdk["enabled"] is False
    assert result.sdk["is_python"] is False
    assert result.sdk["reason"] == "not a Python command"
    child_env = seen["env"]
    assert "blackbox-sdk-" not in child_env.get("PYTHONPATH", ""), "a shell child must not be handed the SDK path"
    assert not any("blackbox-sdk-" in str(value) for value in child_env.values())
    if report is not None:
        assert "blackbox-sdk-" not in report.read_text(), "and the child itself must not see it either"


def test_the_python_command_test_itself_is_platform_neutral():
    """`_is_python_command` is the decision behind both branches above, so it is tested without a child at all."""
    for python_like in (sys.executable, "python", "python3", "python3.13", "pythonw", "PYTHON.EXE"):
        assert launcher._is_python_command([python_like])[0] is True, python_like
    for other in ("node", "bash", "sh", "cmd", "blackbox", "pythonista", "python-runner", "/usr/bin/env"):
        assert launcher._is_python_command([other])[0] is False, other
    # `py.exe` is Windows' launcher and is a Python command *only* where it resolves; this asserts the rule without
    # pretending a POSIX box has one.
    assert launcher._is_python_command(["py"])[0] is (os.name == "nt" and bool(shutil.which("py")))


# ── one run, several tapes, linked ─────────────────────────────────────────────────────────────────────────────────
CHILD_WITH_A_NODE = """
from blackbox import api

with api.run("child-work"):
    api.mark("from-the-child")
"""


def _capsules(result) -> dict[str, dict]:
    """role → {manifest, links} for every capsule this run left behind, read back through the public reader."""
    out = {}
    for record in result.capsules:
        assert "error" not in record, record
        capsule = cap.read_capsule(record["capsule_path"])
        assert capsule.root_ok, f"{record['capsule_path']} failed its own integrity root"
        out[record["role"]] = {"manifest": capsule.manifest,
                               "links": json.loads(capsule.entry("links.json")),
                               "writer": record["writer_uuid"]}
    return out


def test_one_run_is_one_set_of_linked_tapes_not_one_tape(tmp_path):
    """P3B-03's acceptance line: `blackbox run -- python app.py` yields **one run with linked nodes**.

    Before this, the launcher recovered only its own tape, so the child's frames (the ones with the work in them) were
    in no capsule at all, and the child's tape carried no `RUN_START`, so `correlate` read its run id as `None` and
    nothing could link. Both halves are asserted here: every tape of the run is recovered, and the capsules name the
    same run with an `exact` link between them.
    """
    data_dir = tmp_path / "data"
    child = [sys.executable, "-c", CHILD_WITH_A_NODE]
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch(child, data_dir=str(data_dir), out=out, proxy=False)
    assert result.exit_code == 0
    capsules = _capsules(result)
    assert set(capsules) == {"launcher", "sdk"}, f"expected the launcher's tape and the child's, got {set(capsules)}"

    launcher_cap, sdk_cap = capsules["launcher"], capsules["sdk"]
    # defect 47: the capsules must agree with each other *and* with the link, which is resolved from the tapes'
    # `RUN_START`. A child tape that attached declares the launcher's run, so its capsule names that run too.
    assert {entry["manifest"]["run"]["run_id"] for entry in capsules.values()} == {result.run_id}
    assert launcher_cap["manifest"]["capsule_id"] != sdk_cap["manifest"]["capsule_id"], "one run, two tapes, two capsules"
    # the child's tape is where the nodes are, which is why recovering only the launcher's tape lost the work
    assert sdk_cap["manifest"]["counts"]["nodes"] == 1
    assert sdk_cap["manifest"]["counts"]["events"] > launcher_cap["manifest"]["counts"]["events"]

    for role, entry in capsules.items():
        links = entry["links"]["links"]
        assert len(links) == 1, f"{role} should carry exactly one link, got {links}"
        assert links[0]["class"] == "exact"
        assert {links[0]["left_writer"], links[0]["right_writer"]} == {launcher_cap["writer"], sdk_cap["writer"]}
        assert links[0]["left_run"] == links[0]["right_run"] == result.run_id
        # A link describes a relation; it does not fold two recordings into one. The capsules stay separate files.
        assert links[0].get("merges_runs", False) is False
    paths = {c["capsule_path"] for c in result.capsules}
    assert len(paths) == 2, "a linked run is still two capsules, one per writer"


def test_the_childs_capsule_names_the_run_the_launcher_started(tmp_path):
    """The link is only `exact` because the child's own `RUN_START` names the launcher's run id."""
    data_dir = tmp_path / "data"
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch([sys.executable, "-c", CHILD_WITH_A_NODE], data_dir=str(data_dir),
                                 out=out, proxy=False)
    named = {c["linked_run_id"] for c in result.capsules}
    assert named == {result.run_id}, f"tapes named {named}, the run is {result.run_id}"


def test_no_sdk_leaves_one_tape_and_one_capsule(tmp_path):
    """`--no-sdk` has to be visible on disk, not just in a flag: one tape, one capsule, no link to invent."""
    data_dir = tmp_path / "data"
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch([sys.executable, "-c", CHILD_WITH_A_NODE], data_dir=str(data_dir),
                                 out=out, proxy=False, sdk=False)
    assert len(result.capsules) == 1
    assert _capsules(result)["launcher"]["links"]["summary"] == {"declared": 0, "exact": 0, "inferred": 0}


def test_a_tape_from_another_run_in_the_same_data_dir_is_not_swept_in(tmp_path):
    """Two runs in one data directory: the second must recover its own tapes and leave the first one's alone."""
    data_dir = tmp_path / "data"
    with open(tmp_path / "out1.txt", "w", encoding="utf-8") as out:
        first = launcher.launch([sys.executable, "-c", "pass"], data_dir=str(data_dir), out=out, proxy=False)
    with open(tmp_path / "out2.txt", "w", encoding="utf-8") as out:
        second = launcher.launch([sys.executable, "-c", "pass"], data_dir=str(data_dir), out=out, proxy=False)
    assert len(second.capsules) == 2, [c["role"] for c in second.capsules]
    assert {c["linked_run_id"] for c in second.capsules} == {second.run_id}
    assert second.run_id != first.run_id


# ── the flags ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_without_force_the_childs_own_base_url_is_adopted_not_overwritten(tmp_path):
    """The default the design has always had, restated here so `--force-proxy`'s meaning is unambiguous."""
    data_dir = tmp_path / "data"
    own = "https://gateway.internal.example/v1"
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch(_child(tmp_path), data_dir=str(data_dir), out=out, proxy=True,
                                 env=_env(OPENAI_BASE_URL=own, ANTHROPIC_BASE_URL=""))
    report = _report(tmp_path)
    assert report["openai_base_url"] == own, "a value the user set must never be moved silently"
    assert result.proxy["adopted"] == {"OPENAI_BASE_URL": own}
    assert result.proxy["overridden"] == {}
    assert report["anthropic_base_url"].startswith(result.proxy["url"])


def test_force_proxy_points_every_variable_at_the_listener_and_names_what_it_displaced(tmp_path):
    data_dir = tmp_path / "data"
    own = "https://gateway.internal.example/v1"
    buffer = []
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch(_child(tmp_path), data_dir=str(data_dir),
                                 out=_Tee(out, buffer), proxy=True, proxy_force=True,
                                 env=_env(OPENAI_BASE_URL=own, ANTHROPIC_BASE_URL=""))
    report = _report(tmp_path)
    proxy_url = result.proxy["url"]
    assert report["openai_base_url"].startswith(proxy_url), "--force-proxy must move the variable it found"
    assert result.proxy["overridden"] == {"OPENAI_BASE_URL": own}
    assert result.proxy["adopted"] == {}
    said = "".join(buffer)
    assert "--force-proxy" in said and "OPENAI_BASE_URL" in said and own in said, \
        f"the displaced value must be named on stderr, got {said!r}"


def test_no_hooks_removes_the_inbox_variable_rather_than_ignoring_the_files(tmp_path):
    data_dir = tmp_path / "data"
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch(_child(tmp_path), data_dir=str(data_dir), out=out, proxy=False, hooks=False)
    report = _report(tmp_path)
    assert report["inbox"] is None, "a hook that writes an inbox nobody folds is a directory that grows"
    assert result.hooks["enabled"] is False and result.hooks["reason"] == "--no-hooks"
    assert not (data_dir / "inbox").exists()


def test_hooks_on_by_default_still_exports_the_inbox(tmp_path):
    """The paired test: `--no-hooks` has to be a difference, not a description of the default."""
    data_dir = tmp_path / "data"
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        launcher.launch(_child(tmp_path), data_dir=str(data_dir), out=out, proxy=False)
    assert _report(tmp_path)["inbox"] == str(data_dir / "inbox")


def test_no_proxy_starts_no_listener(tmp_path):
    data_dir = tmp_path / "data"
    with open(tmp_path / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch(_child(tmp_path), data_dir=str(data_dir), out=out, proxy=False)
    report = _report(tmp_path)
    assert result.proxy == {}
    assert report["blackbox_proxy"] is None
    assert report["openai_base_url"] is None


# ── the flag surface is reachable from the CLI, with the exit codes it already had ───────────────────────────────
@pytest.mark.parametrize("flag", ["--no-sdk", "--no-hooks", "--force-proxy", "--no-proxy"])
def test_every_documented_flag_is_accepted_by_the_cli(tmp_path, flag):
    argv = ["--data-dir", str(tmp_path / "data"), flag, "--", sys.executable, "-c", "raise SystemExit(7)"]
    if flag == "--no-proxy":
        argv.insert(3, "--force-proxy" if "--force-proxy" not in argv else "--no-proxy")
    assert launcher.main(argv) == 7, "the child's exit code is the launcher's, whatever the flags say"


class _Tee:
    """A writer that keeps what was written, so a test can assert on what the user was told."""

    def __init__(self, stream, sink: list[str]):
        self.stream = stream
        self.sink = sink

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.sink.append(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
