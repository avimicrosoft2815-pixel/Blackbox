"""Every command the error table tells you to run has to exist (defect 111).

Six codes in `errors.py` ended their advice with `blackbox doctor …`, and `doctor` was in the plan but not in the binary.
The cost is not the missing feature — it is that the sentence a stuck user reads after one failure produces a second one,
`unknown command 'doctor'`, at the worst moment. The same class of mistake is `blackbox gc --dry-run` in `docs/desktop.md`:
`retention` never deletes by design, so the command it named could not exist.

This test reads the registry, extracts every `blackbox <word>` a remediation names, and asserts the word is a command the
front door dispatches. It reads the user-facing documents for the same shape, so a doc that invents a command fails here
rather than in somebody's terminal.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.__main__ import KNOWN  # noqa: E402
from blackbox.errors import REGISTRY  # noqa: E402

COMMANDS = set(KNOWN) | {"status"}

#: Words that follow `blackbox` in prose without being commands.
NOT_A_COMMAND = {"run", "the", "is", "and", "on", "under", "to", "if", "with"} - COMMANDS

PATTERN = re.compile(r"`?blackbox\s+([a-z][a-z-]+)")


def _commands_in(text: str) -> set[str]:
    found = {match.group(1) for match in PATTERN.finditer(text)}
    return {name for name in found if name not in NOT_A_COMMAND}


def test_every_remediation_names_a_command_this_build_has():
    problems: list[str] = []
    for code, (message, advice) in REGISTRY.items():
        for name in _commands_in(advice):
            if name not in COMMANDS:
                problems.append(f"{code} ({message}) tells the user to run `blackbox {name}`")
    assert not problems, "\n".join(problems)


def test_the_doctor_codes_that_used_to_dangle_now_point_at_probes_it_has():
    """`doctor` exists and the probes the advice names are the probes it implements."""
    from blackbox.cli.cmd_doctor import PROBES

    named = {name for code, (_, advice) in REGISTRY.items() for name in re.findall(r"doctor --([a-z-]+)", advice)}
    assert named, "no remediation names a doctor probe any more; this test would then guard nothing"
    assert named <= set(PROBES), f"advice names probes doctor does not implement: {sorted(named - set(PROBES))}"


#: Documents that record history rather than give advice. They keep the words of the build they describe; rewriting them
#: to today's command set would edit the record, which is the one thing this repository does not do to its own logs.
HISTORY = {
    "ANALYSIS-2026-11-16.md", "REVIEW_v1.1_FINDINGS.md", "STATUS.md", "defects.md", "progress.md",
    "stage-0-report.md", "stage-1-report.md", "stage-2a-report.md", "DECISIONS.md", "DEVIATIONS.md",
    "BLACKBOX_FINAL_ARCHITECTURE_v1.1.md", "BLACKBOX_ROUND1_PRIOR_ART_CRITIQUE.md",
}

#: A line that says the command is deliberately absent is not a dangling reference — it is the documentation doing its job.
ABSENCE = ("not offered", "not implemented", "deliberately", "planned", "does not exist", "from blackbox import",
           "no blackbox", "was never")


def test_the_user_documents_do_not_invent_commands():
    problems: list[str] = []
    for path in sorted((ROOT / "docs").glob("*.md")):
        if path.name in HISTORY:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            lowered = line.lower()
            if any(marker in lowered for marker in ABSENCE):
                continue
            for match in re.finditer(r"`blackbox ([a-z][a-z-]+)", line):
                name = match.group(1)
                if name not in COMMANDS:
                    problems.append(f"{path.name}:{number}: `blackbox {name}`")
    assert not problems, "documents name commands this build does not have:\n" + "\n".join(problems)
