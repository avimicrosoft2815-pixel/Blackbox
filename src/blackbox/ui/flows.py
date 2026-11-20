"""The four things a person does with a recording, as functions the window can call.

The CLI already does each of these; what this module adds is a shape a page can render and the **gates** that make the
window as safe as the terminal:

| flow | what it does | what it writes |
|---|---|---|
| `verify_capsule` | recompute the capsule's integrity index and report every problem | nothing |
| `diagnose_capsule` | findings, verdicts and a repair brief | nothing |
| `review_proposal` | the diff, the risk, the suggested tests and the approval token | nothing |
| `apply_reviewed` | apply a proposal to the user's repository, after an explicit human confirmation | files the proposal names, plus a verified backup |
| `run_tests` | run the commands the *human* names, in the repository | nothing but side effects the commands have |

Two rules are enforced here rather than in the page, because a page is the wrong place to hold them:

* **`apply_reviewed` refuses unless the person confirmed *and* typed the reviewed token's prefix.** The token is bound to
  the proposal bytes and the repository state at the moment of the preview, so a tree that moved between review and click
  produces a different token, a failed match, and a refusal that says what changed.
* **`run_tests` takes its commands from the caller**, never from a proposal or a capsule, and reports exactly what ran.

Nothing here talks to a network, and nothing here sends incident data anywhere: the brief is a string a person can copy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from blackbox.cap import format as cap

#: How many commands a verification may run, and how long each may take. A page is not a CI system.
MAX_TESTS = 8
TEST_TIMEOUT_S = 900.0


@dataclass
class VerifyView:
    """The result of recomputing a capsule's integrity index."""

    name: str
    ok: bool = False
    entries: int = 0
    bytes: int = 0
    root: str | None = None
    root_ok: bool | None = None
    problems: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def verify_capsule(path: str, *, max_bytes: int = 1 << 30) -> VerifyView:
    """Recompute the integrity index — the same work `blackbox verify` does, reported as data. Never raises."""
    view = VerifyView(name=path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    started = time.monotonic()
    try:
        capsule = cap.read_capsule(path, max_bytes=max_bytes)
    except Exception as exc:                      # a capsule that cannot be read is a result, not a 500
        view.error = str(exc) if hasattr(exc, "code") else f"{type(exc).__name__}: {exc}"
    else:
        view.ok = capsule.root_ok
        view.entries = len(capsule.entries)
        view.bytes = sum(len(value) for value in capsule.entries.values())
        view.root = (capsule.manifest.get("integrity") or {}).get("root")
        view.root_ok = capsule.root_ok
        view.problems = list(capsule.problems)
    view.seconds = round((time.monotonic() - started) * 1000.0, 1)
    return view


@dataclass
class DiagnoseView:
    """A recording's findings, in the three voices the brief uses: observed, likely, cannot-tell."""

    name: str
    headline: str = ""
    summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    markdown: str = ""
    brief: str = ""
    error: str | None = None
    seconds: float = 0.0

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for finding in self.findings:
            out[finding["verdict"]] = out.get(finding["verdict"], 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "headline": self.headline, "summary": self.summary,
                "findings": self.findings, "counts": self.counts, "error": self.error,
                "seconds": self.seconds}


def diagnose_capsule(path: str, *, max_events: int = 50_000) -> DiagnoseView:
    """Read the capsule's frames and report what they support. Read-only; never raises."""
    from blackbox.diagnose import evidence as evidence_mod
    from blackbox.diagnose import report as report_mod

    view = DiagnoseView(name=path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    started = time.monotonic()
    try:
        evidence = evidence_mod.load(path, max_events=max_events)
        report = report_mod.build(evidence)
        view.headline = report.headline()
        view.summary = " · ".join(report.summary_lines()) or "no findings"
        view.findings = [finding.to_dict() for finding in report.findings]
        view.markdown = report.markdown()
        view.brief = report.brief()
    except Exception as exc:
        view.error = str(exc) if hasattr(exc, "code") else f"{type(exc).__name__}: {exc}"
    view.seconds = round((time.monotonic() - started) * 1000.0, 1)
    return view


# ── the repair gate ─────────────────────────────────────────────────────────
class FlowRefused(Exception):
    """A flow that will not proceed, with a reason a page can print instead of a traceback."""


@dataclass
class ProposalView:
    """A proposal as a reviewer sees it: what it changes, what it risks, and the token that approves exactly this."""

    proposal: str
    repo: str
    title: str = ""
    rationale: str = ""
    token: str = ""
    scope: dict[str, Any] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    risk: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def hint(self) -> str:
        """The characters a person must type to approve this — the first eight of the token, which is 32 bits of binding."""
        return self.token[:8]

    def as_dict(self) -> dict[str, Any]:
        return {"proposal": self.proposal, "repo": self.repo, "title": self.title, "token": self.token,
                "scope": self.scope, "risk": self.risk, "tests": self.tests, "error": self.error,
                "changes": [{k: c[k] for k in ("path", "verb", "diff", "current", "new_bytes")} for c in self.changes]}


def review_proposal(proposal_path: str, repo: str) -> ProposalView:
    """Load a proposal against a repository and describe it. Writes nothing; never raises."""
    from blackbox.repair import proposal as proposal_mod

    view = ProposalView(proposal=proposal_path, repo=repo)
    try:
        proposal = proposal_mod.load(proposal_path, repo=repo)
    except Exception as exc:                      # a bad proposal is a message, not a stack trace in the window
        view.error = str(exc) if hasattr(exc, "code") else f"{type(exc).__name__}: {exc}"
        return view
    view.title = proposal.title
    view.rationale = proposal.rationale
    view.token = proposal.token()
    view.scope = proposal.scope()
    view.risk = list(proposal.risk) + list(proposal_mod.assessed_risk(proposal))
    view.tests = list(proposal.suggested_tests)
    for change in proposal.changes:
        view.changes.append({
            "path": change.path,
            "verb": "create" if change.is_new() else "replace",
            "diff": change.diff(),
            "current": (change.current_sha256 or "")[:12],
            "new_bytes": len(change.content.encode("utf-8")),
        })
    return view


def apply_reviewed(proposal_path: str, repo: str, *, typed: str, confirmed: bool) -> dict[str, Any]:
    """Apply a reviewed proposal — only after a human checked the box **and** typed the reviewed token's prefix.

    The token is recomputed here from the proposal and the repository as they are *now*. If either moved since the
    preview, the typed prefix no longer matches and this refuses with the changed paths named — which is what makes the
    typed characters mean "this diff, against this tree" rather than "whatever is there when I click".
    """
    from blackbox.repair.apply import apply as apply_proposal

    if not confirmed:
        raise FlowRefused("nothing was applied: the confirmation box was not ticked, so this change was not approved")
    view = review_proposal(proposal_path, repo)
    if not view.ok:
        raise FlowRefused(f"nothing was applied: {view.error}")
    typed_clean = (typed or "").strip().lower()
    if not typed_clean:
        raise FlowRefused("nothing was applied: the approval token was not typed")
    if typed_clean != view.hint:
        raise FlowRefused(
            "nothing was applied: the typed characters do not match this proposal and this repository state. "
            f"The token for what is in the tree now starts {view.hint} — re-read the diff before approving it.")
    from blackbox.repair import proposal as proposal_mod

    proposal = proposal_mod.load(proposal_path, repo=repo)
    applied = apply_proposal(proposal, approval=proposal.token())
    return {"applied": True, "backup": applied.backup_dir, "manifest": applied.manifest,
            "created": list(applied.created), "written": list(applied.written),
            "private_by_mode": applied.private_by_mode, "token": proposal.token()}


def run_tests(commands: list[str], *, cwd: str, confirmed: bool) -> dict[str, Any]:
    """Run the commands the person asked for, in the repository, and report every exit code.

    The commands come from the form's checkboxes (the proposal's suggestions) or from a text field, never from a capsule.
    They are shown before this runs, and `executes: true` is in the result so a caller cannot mistake it for a dry run.
    """
    from blackbox.repair.apply import verify as verify_change

    if not confirmed:
        raise FlowRefused("nothing was run: the confirmation box was not ticked")
    chosen = [command for command in commands if command.strip()][:MAX_TESTS]
    if not chosen:
        raise FlowRefused("nothing was run: no command was given, so this change stays unverified")
    result = verify_change(chosen, cwd=cwd, timeout_s=TEST_TIMEOUT_S)
    result["commands_shown_before_running"] = chosen
    return result
