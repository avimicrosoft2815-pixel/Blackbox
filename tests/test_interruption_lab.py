"""P4-19 — the interruption lab: kill a real process at each interruption class, recover it, diagnose it.

`tests/test_recovery.py` proves the *classifier* maps frames to the §14.4 rows, using hand-built frame lists. This file
proves the whole path with nothing hand-built: a child writes real frames through a real `TapeWriter`, is killed with a
real signal, leaves a real tape, and the parent runs the real `recovery.recover()` and the real `diagnose`.

The distinction matters because the two layers fail differently. A classifier can be right about a frame list that no
seam ever produces (defect 44 was exactly that); a recovery can produce a capsule that the diagnosis then misreads; and a
diagnosis can be right about a clean run and wrong about a torn one. Only an end-to-end kill exercises all three.

What this lab deliberately does **not** assert: the child's exit code. A `SIGKILL` on POSIX reports `-9`/`137`, a signal
on Windows reports the signal number, and an `os._exit` fallback reports 1. The property under test is what the *tape*
says afterwards, so the assertion is on frames and findings, not on a number that means something different per platform.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.diagnose import build, load
from blackbox.rec import recovery

CHILD = ROOT / "tests" / "_interruption_child.py"
CAUSE_PREFIXES = ("interrupted-stream-", "no-first-byte-", "provider-error-", "tool-outcome-unknown-")
CAUSE_IDS = ("no-run-end", "torn-tail", "integrity-root", "empty-capsule", "bounded-read")


def _run_lab(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess, object, object]:
    """Kill a child in this scenario, recover what it left, and diagnose it. Nothing is mocked."""
    data = tmp_path / scenario / "data"
    data.mkdir(parents=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUNBUFFERED": "1"}
    completed = subprocess.run([sys.executable, str(CHILD), scenario, str(data)],
                               capture_output=True, text=True, env=env, timeout=120)
    recovered = recovery.recover(str(data))
    assert recovered.capsule_path, f"{scenario}: a killed child left no recoverable capsule ({completed.stderr[-400:]})"
    evidence = load(recovered.capsule_path, max_events=500)
    return completed, evidence, build(evidence)


def _statuses(evidence: object) -> dict[int, str]:
    return {node.slot: node.status for node in evidence.nodes}     # type: ignore[attr-defined]


def _ids(report: object) -> list[str]:
    return [finding.id for finding in report.findings]             # type: ignore[attr-defined]


def _causes(report: object) -> list[str]:
    return [i for i in _ids(report) if i.startswith(CAUSE_PREFIXES) or i in CAUSE_IDS]


# ── one row per §14.4 class, plus the two terminal cases ────────────────────────────────────────────────────────────
def test_mid_stream_is_recovered_as_a_cut_stream_and_diagnosed_as_one(tmp_path):
    _, evidence, report = _run_lab(tmp_path, "mid-stream")
    assert _statuses(evidence)[2] == recovery.INTERRUPTED_MID_STREAM
    finding = next(f for f in report.findings if f.id == "interrupted-stream-2")
    assert finding.verdict == "likely", "the frames say the stream was cut; they do not say by whom"
    assert any("MODEL_CHUNK" in ref for ref in finding.evidence)
    assert len(finding.alternatives) >= 3
    assert evidence.root_ok, "a SIGKILL must not cost the capsule its integrity root"


def test_before_first_byte_is_not_dressed_up_as_a_cut_stream(tmp_path):
    _, evidence, report = _run_lab(tmp_path, "before-first-byte")
    assert _statuses(evidence)[2] == recovery.INTERRUPTED_BEFORE_FIRST_BYTE
    assert "no-first-byte-2" in _ids(report)
    assert not any(i.startswith("interrupted-stream-") for i in _ids(report)), \
        "no chunk was ever recorded, so nothing was cut mid-stream"


def test_a_killed_tool_call_is_unknown_and_never_reported_as_not_having_run(tmp_path):
    _, evidence, report = _run_lab(tmp_path, "in-tool")
    assert _statuses(evidence)[3] == recovery.INTERRUPTED_IN_TOOL
    finding = next(f for f in report.findings if f.id == "tool-outcome-unknown-3")
    assert finding.verdict == "cannot-tell"
    text = report.markdown().lower()
    assert "may have completed" in text
    assert "not a failed tool" in text
    assert "did not run" not in text, "the product must never say this about an unclosed tool"


def test_a_response_recorded_before_the_kill_is_not_an_interrupted_stream(tmp_path):
    _, evidence, report = _run_lab(tmp_path, "after-response")
    assert _statuses(evidence)[2] == recovery.INTERRUPTED_AFTER_RESPONSE
    assert not any(i.startswith("interrupted-stream-") for i in _ids(report))
    assert "no-run-end" in _ids(report), "the process still never closed its run"


def test_a_provider_error_is_reported_as_observed_with_the_type_the_tape_carries(tmp_path):
    _, _, report = _run_lab(tmp_path, "provider-error")
    finding = next(f for f in report.findings if f.id == "provider-error-2")
    assert finding.verdict == "observed" and finding.confidence == "high"
    assert "RateLimitError" in finding.basis


def test_a_clean_run_through_the_same_path_has_no_interruption_finding(tmp_path):
    completed, evidence, report = _run_lab(tmp_path, "clean")
    assert completed.returncode == 0
    assert evidence.has_run_end
    assert _causes(report) == [], f"a run that closed every node and reached RUN_END: {_causes(report)}"


# ── the properties that hold for every class ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", ["mid-stream", "before-first-byte", "in-tool", "after-response", "provider-error"])
def test_every_killed_scenario_leaves_evidence_that_verifies(tmp_path, scenario):
    completed, evidence, report = _run_lab(tmp_path, scenario)
    assert completed.returncode != 0, "these children do not exit cleanly, by construction"
    assert evidence.root_ok and not evidence.problems
    assert evidence.frames, "a killed run still has the frames it wrote before the kill"
    assert report.headline(), "every scenario produces a headline, even the unremarkable ones"


@pytest.mark.parametrize("scenario", ["mid-stream", "in-tool", "before-first-byte", "after-response", "provider-error"])
def test_no_scenario_claims_a_cause_it_only_suspects(tmp_path, scenario):
    _, _, report = _run_lab(tmp_path, scenario)
    for finding in report.findings:
        if finding.id.startswith(("interrupted-stream-", "no-first-byte-", "no-run-end", "tool-outcome-unknown-")):
            assert finding.verdict in ("likely", "cannot-tell"), f"{finding.id} claims {finding.verdict}"
            assert finding.alternatives or finding.verdict == "cannot-tell"


def test_the_report_and_the_brief_render_for_every_scenario_without_inventing_a_location(tmp_path):
    for scenario in ("mid-stream", "in-tool", "clean"):
        _, _, report = _run_lab(tmp_path, scenario)
        markdown, brief = report.markdown(), report.brief()
        assert "No file or line is named here" in brief
        assert ".py:" not in brief
        assert "Untrusted input" in brief
        assert markdown.startswith("# Incident report")


def test_the_lab_json_is_a_row_per_scenario_with_the_findings_that_were_actually_produced(tmp_path):
    """The bench script writes this shape; the test builds one row the same way and checks it is derived, not asserted."""
    _, evidence, report = _run_lab(tmp_path, "mid-stream")
    row = {
        "scenario": "mid-stream",
        "node_statuses": {str(node.slot): node.status for node in evidence.nodes},
        "findings": [{"id": f.id, "verdict": f.verdict, "confidence": f.confidence} for f in report.findings],
        "run_end": evidence.has_run_end,
        "frames": evidence.events_seen,
        "root_ok": evidence.root_ok,
    }
    assert json.loads(json.dumps(row))["findings"][0]["verdict"] == "likely"
    assert row["node_statuses"]["2"] == "INTERRUPTED_MID_STREAM"
    assert row["run_end"] is False
