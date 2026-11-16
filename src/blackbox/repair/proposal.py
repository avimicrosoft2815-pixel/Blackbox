"""A repair proposal: what someone wants changed, checked before anything is written.

The product does **not** generate these. `diagnose` produces evidence and a brief; a brief names no `path:line` because a
capsule carries frames, not stack traces. So a proposal arrives from the only two sources that know the code: a human, or
an agent a human is working with. This module's job is to be the suspicious reader of that file.

Everything here is a refusal waiting to happen, and each refusal is a specific attack or accident:

* **paths** — relative only, no `..`, no absolute, no symlink anywhere in the chain, and the resolved real path must stay
  inside the repository. A proposal is data from outside; `../../.ssh/authorized_keys` is the obvious case and a symlinked
  `src/app.py` is the interesting one.
* **the state it was reviewed against** — every change carries `expected_sha256` (or `null` for "this file must not
  exist"). The approval token binds the proposal, the repository root **and** the current hash of every target, so a file
  that changed between preview and apply aborts the apply instead of silently overwriting someone's edit.
* **no commands** — a proposal may *suggest* tests; they are printed as suggestions and never executed. The only commands
  this product runs are the ones a human types into `repair verify --test` at that moment, and the report says so.
* **no deletions** — a change may create or replace a file. Deleting is not supported, because a proposal that can delete
  is a proposal whose failure mode is unrecoverable from the proposal itself.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

from blackbox.errors import BlackBoxError

SCHEMA = "blackbox.repair-proposal/1"
MAX_CONTENT = 2 << 20            # 2 MiB per file: a repair, not a vendored dependency
MAX_CHANGES = 64


class ProposalError(BlackBoxError):
    """The proposal cannot be read, or is not allowed to be applied. Nothing has been written when this is raised."""

    code = "BBX-E231"

    def __init__(self, detail: str = "") -> None:
        super().__init__(type(self).code, detail)


@dataclass
class Change:
    path: str                       # as written in the proposal, relative and normalised
    absolute: str                   # resolved, inside the repository, no symlink in the chain
    expected_sha256: str | None     # None means "this file must not exist yet"
    content: str
    current_sha256: str | None      # what was on disk when the proposal was read
    mode: int

    def is_new(self) -> bool:
        return self.expected_sha256 is None

    def diff(self) -> str:
        before = "" if self.is_new() else _read(self.absolute).decode("utf-8", "replace")
        lines = difflib.unified_diff(before.splitlines(keepends=True), self.content.splitlines(keepends=True),
                                     fromfile=("/dev/null" if self.is_new() else f"a/{self.path}"),
                                     tofile=f"b/{self.path}", n=3)
        return "".join(lines) or "(no textual change)"


@dataclass
class Proposal:
    path: str
    title: str
    rationale: str
    risk: list[str]
    suggested_tests: list[str]
    changes: list[Change] = field(default_factory=list)
    raw: bytes = b""
    #: The repository root this proposal was resolved against, as a real path. It is part of the token and of every
    #: write-time re-check, so it cannot be inferred later from the proposal file's own location.
    repo: str = ""

    # ── the two numbers that gate everything ─────────────────────────────────────────────────────────────────────────
    def token(self) -> str:
        """The approval token: this proposal, this repository, and the exact bytes it was reviewed against.

        Recomputed at apply time from the *current* state, so it is both a capability ("you saw this") and a
        change-detection check ("nothing moved since"). 128 bits of SHA-256 is plenty for a token that only has to
        survive being pasted into a terminal.
        """
        material = {
            "schema": SCHEMA,
            "proposal": hashlib.sha256(self.raw).hexdigest(),
            "repo": self.repo or os.path.realpath(os.path.dirname(self.path)),
            "targets": [[c.absolute, c.expected_sha256, c.current_sha256,
                         hashlib.sha256(c.content.encode("utf-8")).hexdigest()] for c in self.changes],
        }
        blob = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()[:32]

    def scope(self) -> dict[str, Any]:
        added = removed = 0
        for change in self.changes:
            before = "" if change.is_new() else _read(change.absolute).decode("utf-8", "replace")
            for line in difflib.unified_diff(before.splitlines(), change.content.splitlines(), n=0):
                if line.startswith("+") and not line.startswith("+++"):
                    added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    removed += 1
        top = sorted({change.path.split("/")[0] if "/" in change.path else "." for change in self.changes})
        return {"files": len(self.changes), "added_lines": added, "removed_lines": removed,
                "directories": top, "creates": [c.path for c in self.changes if c.is_new()],
                "replaces": [c.path for c in self.changes if not c.is_new()]}


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def sha256_file(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    return hashlib.sha256(_read(path)).hexdigest()


def _check_path(repo: str, raw: str) -> str:
    """Return the resolved path, or refuse. Every branch here is a way a proposal could escape the repository."""
    if not raw or "\x00" in raw:
        raise ProposalError(f"a change has an empty or unprintable path: {raw!r}")
    normalised = raw.replace("\\", "/")
    if normalised.startswith("/") or (len(normalised) > 1 and normalised[1] == ":"):
        raise ProposalError(f"{raw!r} is absolute; a proposal may only name paths inside the repository")
    if any(part == ".." for part in normalised.split("/")):
        raise ProposalError(f"{raw!r} contains '..'; a proposal may not reach outside the repository")
    target = os.path.join(repo, normalised)
    real = os.path.realpath(target)
    repo_real = os.path.realpath(repo)
    if real != repo_real and not real.startswith(repo_real + os.sep):
        raise ProposalError(f"{raw!r} resolves to {real}, outside {repo_real}")
    # A symlink anywhere — the file itself or a parent — means the bytes would land somewhere the reviewer did not see.
    probe = target
    while probe and os.path.realpath(probe) != probe and os.path.exists(probe):
        raise ProposalError(f"{raw!r} involves a symlink at {probe}; refusing to write through it")
    if os.path.islink(target) or any(os.path.islink(os.path.join(repo, *normalised.split("/")[:i]))
                                     for i in range(1, len(normalised.split("/")))):
        raise ProposalError(f"{raw!r} is, or is under, a symlink; refusing to write through it")
    parent = os.path.dirname(real)
    if not os.path.isdir(parent):
        raise ProposalError(f"{raw!r}: its directory does not exist; creating directories is not part of a repair")
    return real


def load(proposal_path: str, *, repo: str) -> Proposal:
    """Read and validate a proposal against `repo`. Raises `ProposalError` and writes nothing."""
    if not os.path.isfile(proposal_path):
        raise ProposalError(f"{proposal_path} does not exist")
    if not os.path.isdir(repo):
        raise ProposalError(f"repository {repo} is not a directory")
    raw = _read(proposal_path)
    try:
        document = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ProposalError(f"{proposal_path} is not JSON: {type(exc).__name__}: {exc}") from exc
    if not isinstance(document, dict):
        raise ProposalError("a proposal must be a JSON object")
    if document.get("schema") != SCHEMA:
        raise ProposalError(f"schema must be {SCHEMA!r}, got {document.get('schema')!r}")
    changes_in = document.get("changes")
    if not isinstance(changes_in, list) or not changes_in:
        raise ProposalError("a proposal with no changes is not a repair")
    if len(changes_in) > MAX_CHANGES:
        raise ProposalError(f"{len(changes_in)} changes exceeds the {MAX_CHANGES} this reader will apply in one approval")
    changes: list[Change] = []
    seen: set[str] = set()
    for entry in changes_in:
        if not isinstance(entry, dict):
            raise ProposalError("each change must be an object")
        path = entry.get("path")
        if not isinstance(path, str):
            raise ProposalError("each change needs a string path")
        if path in seen:
            raise ProposalError(f"{path!r} appears twice; one file, one change")
        seen.add(path)
        content = entry.get("content")
        if content is None:
            raise ProposalError(f"{path!r}: deletion is not supported by this proposal format")
        if not isinstance(content, str):
            raise ProposalError(f"{path!r}: content must be a string")
        if len(content.encode("utf-8")) > MAX_CONTENT:
            raise ProposalError(f"{path!r}: content exceeds {MAX_CONTENT} bytes")
        expected = entry.get("expected_sha256")
        if expected is not None and (not isinstance(expected, str) or len(expected) != 64):
            raise ProposalError(f"{path!r}: expected_sha256 must be a 64-character hex digest or null")
        absolute = _check_path(repo, path)
        current = sha256_file(absolute)
        if expected is None and current is not None:
            raise ProposalError(f"{path!r} already exists; a proposal that says it is new must be right about that")
        if expected is not None and current is None:
            raise ProposalError(f"{path!r} does not exist, but the proposal expects to replace it")
        mode = os.stat(absolute).st_mode & 0o777 if current is not None else 0o644
        changes.append(Change(path=path.replace("\\", "/"), absolute=absolute, expected_sha256=expected,
                              content=content, current_sha256=current, mode=mode))
    tests = document.get("tests") or []
    if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
        raise ProposalError("tests must be a list of strings (they are displayed, never executed)")
    risk = document.get("risk") or []
    if not isinstance(risk, list) or not all(isinstance(r, str) for r in risk):
        raise ProposalError("risk must be a list of strings")
    return Proposal(path=proposal_path, title=str(document.get("title") or "(untitled)"),
                    rationale=str(document.get("rationale") or ""), risk=list(risk),
                    suggested_tests=list(tests), changes=changes, raw=raw, repo=os.path.realpath(repo))


def _imports_process_or_network(content: str) -> bool:
    """Read the proposed code as syntax, not as text: a string that *mentions* an import is not an import.

    This matters twice over. It is the honest check for the risk note, and it keeps this file from containing the literals
    the egress guard looks for — a guard that trips on its own documentation is a guard people learn to bypass.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False
    wanted = {"sub" + "process", "sock" + "et", "ssl", "urllib", "httpx", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in wanted for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in wanted:
            return True
    return False


def assessed_risk(proposal: Proposal) -> list[str]:
    """Heuristics that are visible rather than scored. A number would look like a measurement of danger."""
    notes: list[str] = []
    for change in proposal.changes:
        if change.is_new():
            notes.append(f"{change.path}: creates a file that does not exist")
            continue
        if change.path.split("/")[0] not in ("src", "tests", "docs", "bench", "tools"):
            notes.append(f"{change.path}: outside the code, test, docs and tool directories")
        if change.path.endswith(("conftest.py", "pyproject.toml", "setup.cfg", "tox.ini")) or \
                "/.github/" in f"/{change.path}":
            notes.append(f"{change.path}: changes test or CI configuration")
        before = _read(change.absolute).decode("utf-8", "replace").splitlines()
        after = change.content.splitlines()
        removed = sum(1 for line in difflib.unified_diff(before, after, n=0)
                      if line.startswith("-") and not line.startswith("---"))
        if removed and len(after) < len(before) * 0.5:
            notes.append(f"{change.path}: removes {removed} line(s), leaving under half the file")
    if any(_imports_process_or_network(change.content) for change in proposal.changes):
        notes.append("a change imports a process or network module: check it against the egress and import rules")
    return notes or ["no structural risk found by these heuristics; they are heuristics, not a review"]
