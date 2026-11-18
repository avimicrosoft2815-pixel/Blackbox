"""The README is the front door, so it is pinned to the product it describes.

Three failures this prevents, each of which was true in the tree before this test:

* the README said "no CLI beyond `--version`/`status`, no capsules" long after there were nine commands — a stale doc is
  a claim, and a false one;
* every console block, `docs/onboarding.md` and this README said `blackbox …`, while `pyproject.toml` declared **no entry
  point**: an installed user got `command not found` and the only invocation that worked was `python -m blackbox`;
* `blackbox ls` looked in `./.blackbox` while `blackbox run` wrote to the current directory, so the command a new user
  types right after a run reported "no capsules" over a directory that had two of them.

So: every command in the README's console blocks must exist in the front door, the console script must be declared, and
the read-only commands must share one default data directory with `run`. The limits section is checked for the four
caveats that must never quietly disappear from the front page.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import __main__ as front_door
from blackbox.cli.paths import default_data_dir

README = (ROOT / "README.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _console_lines() -> list[str]:
    """The command lines inside ```console blocks, with the `$ ` prompt removed."""
    blocks = re.findall(r"```console\n(.*?)```", README, flags=re.S)
    lines = []
    for block in blocks:
        for line in block.splitlines():
            if line.startswith("$ "):
                lines.append(line[2:])
    return lines


def test_every_command_in_the_readme_exists() -> None:
    assert _console_lines(), "the README has no console block; the front door is undocumented"
    commands = {line.split()[1] for line in _console_lines()
                if line.startswith(("blackbox ", ".venv/bin/blackbox "))}
    assert commands, "no blackbox invocation found in the README"
    for command in commands - {"--version"}:
        assert command in front_door.KNOWN, f"README documents `blackbox {command}`, which the front door does not have"


def test_the_console_script_is_declared() -> None:
    assert "[project.scripts]" in PYPROJECT
    assert 'blackbox = "blackbox.__main__:main"' in PYPROJECT
    assert callable(front_door.main)


def test_the_readme_installs_what_the_readme_runs() -> None:
    """`pip install -e .` has to be the step that produces the command, and the version line has to match the package."""
    assert ".venv/bin/pip install -e ." in README
    version = re.search(r"blackbox (\d+\.\d+\.\d+\S*) \(format v(\d+)\)", README)
    assert version, "the README does not show `blackbox --version` output"
    from blackbox import __version__
    from blackbox import constants as C
    assert version.group(1) == __version__ and int(version.group(2)) == C.FORMAT_VERSION


def test_the_commands_agree_on_the_data_directory() -> None:
    """`ls` and `verify --data` must default to where `run` writes: the current directory."""
    assert default_data_dir() == "."
    from blackbox.cli import cmd_ls
    assert "default_data_dir(args.data)" in Path(cmd_ls.__file__).read_text(encoding="utf-8")
    assert "./.blackbox" not in Path(cmd_ls.__file__).read_text(encoding="utf-8")


def test_the_front_page_keeps_the_limits() -> None:
    limits = README.split("## What this does not do", 1)
    assert len(limits) == 2, "the README lost its limits section"
    body = limits[1].text if hasattr(limits[1], "text") else limits[1]
    for phrase in ("0 node-level\n  edges", "Every macOS cell is", "untested", "no universal 100 ms"):
        assert phrase.replace("\n  ", " ") in body.replace("\n  ", " "), f"the README no longer says: {phrase!r}"


def test_the_readme_is_not_the_old_readme() -> None:
    """The two sentences that were false for months, kept as a regression test rather than a memory."""
    for stale in ("no viewer yet", "no capsule", "no CLI beyond `--version`/`status`"):
        assert stale not in README.lower(), f"stale claim back in the README: {stale!r}"
