"""One place that answers "can this checkout see history?" (C35).

Both ancestry assertions in the test suite (`STATE.last_green_commit`, `STATUS`'s HEAD token) and the forbidden-word
scan over `git log` depend on the clone being **deep**. A `actions/checkout@v4` default is `fetch-depth: 1`, which
makes `git merge-base --is-ancestor <sha> HEAD` true only for `HEAD` itself: the tests then fail for every real sha
and pass for a self-referential one. Four green local runs and seven red CI runs were the same code.

So the shallow case is a **failure with an instruction**, never a skip: a skipped assertion is a claim nobody checked.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHALLOW_MESSAGE = "CI checkout must use fetch-depth: 0 (a depth-1 clone cannot answer ancestry questions)"


def git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd or ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def is_shallow(cwd: Path | None = None) -> bool:
    try:
        return git("rev-parse", "--is-shallow-repository", cwd=cwd) == "true"
    except RuntimeError:
        return False


def commit_count(cwd: Path | None = None) -> int:
    try:
        return int(git("rev-list", "--count", "HEAD", cwd=cwd))
    except (RuntimeError, ValueError):
        return 0


def require_history(cwd: Path | None = None) -> None:
    """Raise with the instruction when the clone cannot answer `--is-ancestor`. A skip here would hide a red cell."""
    if is_shallow(cwd):
        raise AssertionError(SHALLOW_MESSAGE)
    count = commit_count(cwd)
    if count < 2:
        raise AssertionError(f"this checkout has {count} commit(s): {SHALLOW_MESSAGE}")


def is_ancestor(sha: str, ref: str = "HEAD", cwd: Path | None = None) -> bool:
    proc = subprocess.run(["git", "merge-base", "--is-ancestor", sha, ref], cwd=cwd or ROOT,
                          capture_output=True, text=True)
    return proc.returncode == 0


def head_sha(cwd: Path | None = None) -> str:
    return git("rev-parse", "HEAD", cwd=cwd)
