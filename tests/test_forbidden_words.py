"""C12 — the forbidden-word rule, as a test over the tree *and* the commit messages.

The rule (execution brief §1.4) bans marketing absolutes from code, comments, docs, commits, UI and README:
  zero loss · crash-safe · power-loss safe · secure · forensic-grade · deterministic · first ever ·
  unique · never done before · AI-powered

Two things are excluded on purpose, and both are inputs rather than output:
  * the owner's frozen documents (`docs/BLACKBOX_*.md`, `docs/REVIEW_v1.1_FINDINGS.md`, `docs/archive/`) —
    they *quote* the banned list in order to ban it;
  * the CLI flag `--insecure-upstream`, whose name comes from the plan's frozen CLI table.

`docs/guarantees.md` is excluded for the same reason as the frozen documents: §5 "What is never claimed"
has to name the claims it forbids.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BANNED = (
    "zero loss", "zero-loss", "crash-safe", "crash safe", "power-loss safe", "secure", "forensic-grade",
    "forensic grade", "deterministic", "first ever", "never done before", "ai-powered", "unique",
)
ALLOWED_CONTEXTS = ("--insecure-upstream", "insecure_upstream")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".bench", ".pytest_cache", "node_modules", "dist", "build"}
SKIP_FILES = {
    "docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md",
    "docs/BLACKBOX_ROUND1_PRIOR_ART_CRITIQUE.md",
    "docs/REVIEW_v1.1_FINDINGS.md",
    "docs/guarantees.md",           # §5 exists to enumerate the claims that are never made
    "tests/test_forbidden_words.py",
}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".txt", ".cfg", ".ini", ""}


def files() -> list[Path]:
    out = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in SKIP_FILES or rel.startswith("docs/archive/"):
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        out.append(path)
    return out


def scan(text: str) -> list[str]:
    lowered = text.lower()
    for allowed in ALLOWED_CONTEXTS:
        lowered = lowered.replace(allowed, "")
    return [word for word in BANNED if word in lowered]


def test_no_forbidden_word_in_the_tree():
    offenders: list[str] = []
    for path in files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:                                   # pragma: no cover - unreadable file
            continue
        for word in scan(text):
            offenders.append(f"{path.relative_to(ROOT).as_posix()}: {word}")
    assert offenders == [], offenders


def test_no_forbidden_word_in_commit_messages():
    try:
        log = subprocess.run(["git", "log", "--format=%s%n%b"], cwd=ROOT, capture_output=True,
                             text=True, timeout=30, check=False)      # noqa: S607
    except (OSError, subprocess.SubprocessError):                     # pragma: no cover - no git
        return
    if log.returncode != 0:                                          # pragma: no cover - not a checkout
        return
    assert scan(log.stdout) == [], scan(log.stdout)
