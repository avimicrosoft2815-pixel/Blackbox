"""The review packs are builder evidence, and that is only useful if their references resolve and they carry no verdict.

A pack is what a reviewer opens to check a claim. It is worse than useless if it names a test file that has been renamed,
a bench script that never existed, or a command the binary does not dispatch — the reviewer reads a dead link as evidence
and the builder never finds out. And a pack that says "pass" in a verdict-shaped line is a builder signing a gate, which
`docs/gates/README.md` forbids outright.

So: every path and command a pack names must exist, every pack must say it is not a gate file, and none may carry a
signature or a verdict block.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.__main__ import KNOWN   # the commands this binary dispatches

PACKS = sorted((ROOT / "docs" / "reviews").glob("*.md"))

PATH_PATTERN = re.compile(r"`((?:tests|docs|bench|tools|src)/[A-Za-z0-9_./-]+)`")
COMMAND_PATTERN = re.compile(r"`blackbox ([a-z-]+)")


def _existing(relative: str) -> bool:
    """A path with or without its extension: packs write `tests/test_x.py` and sometimes a directory."""
    candidate = ROOT / relative
    if candidate.exists():
        return True
    return any((ROOT / relative).with_suffix(suffix).exists() for suffix in (".py", ".md", ".json"))


def test_every_pack_exists_and_says_it_is_not_a_gate_file() -> None:
    names = {path.name for path in PACKS}
    assert {"README.md", "capture.md", "privacy.md", "redaction.md", "recovery.md", "malicious-files.md",
            "integrations-exports.md"} <= names, names
    for pack in PACKS:
        if pack.name == "README.md":
            continue
        text = pack.read_text(encoding="utf-8")
        assert "not a gate file" in text, f"{pack.name} has to say what it is not"
        assert "unsigned" in text, f"{pack.name} has to say it is unsigned"


def test_every_path_a_pack_names_is_in_the_tree() -> None:
    offenders: list[str] = []
    for pack in PACKS:
        for relative in PATH_PATTERN.findall(pack.read_text(encoding="utf-8")):
            if not _existing(relative):
                offenders.append(f"{pack.name}: {relative}")
    assert offenders == [], "review packs pointing at files that are not there: " + "; ".join(sorted(set(offenders)))


def test_every_command_a_pack_names_is_one_the_binary_dispatches() -> None:
    offenders: list[str] = []
    for pack in PACKS:
        for command in COMMAND_PATTERN.findall(pack.read_text(encoding="utf-8")):
            if command not in KNOWN:
                offenders.append(f"{pack.name}: blackbox {command}")
    assert offenders == [], "review packs naming commands this build does not have: " + "; ".join(sorted(set(offenders)))


def test_no_pack_carries_a_verdict_or_a_signature() -> None:
    """`Signed:` and `Verdict:` are the two shapes a gate file has (`docs/gates/README.md` §Shape)."""
    offenders: list[str] = []
    for pack in PACKS:
        text = pack.read_text(encoding="utf-8")
        for marker in ("**Signed:**", "## Verdict", "Verdict: pass", "Verdict: fail"):
            if marker in text:
                offenders.append(f"{pack.name}: {marker}")
    assert offenders == [], "a builder may add evidence, never a verdict: " + "; ".join(offenders)


def test_the_readme_lists_every_pack() -> None:
    readme = (ROOT / "docs" / "reviews" / "README.md").read_text(encoding="utf-8")
    for pack in PACKS:
        if pack.name == "README.md":
            continue
        assert f"({pack.name})" in readme, f"{pack.name} is not linked from the packs index"
