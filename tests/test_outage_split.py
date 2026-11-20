"""Defect 108, read from the recorded sample rather than from the failure message.

The cell that failed on 2026-11-16 printed eight outages and eight rebinds and left the reader to decide which half of a
686.2 ms outage was the product's. `tools/outage_split.py` answers that question as a rule, and this file holds the rule
to the numbers that were actually recorded — including the one that matters: the worst sample put **none** of its 686.2 ms
in the rebind, so the budget was spent before the code that rebinds ran.

What these tests do not do is claim a cause. `not-ours:tick-or-loop` is a statement that the time was spent outside the
rebind; whether the Windows runner was starved, the proactor loop was parked, or the machine swapped is not decided by a
clock in this process, and the artefact says so in its own notes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from outage_split import split, split_one  # noqa: E402

ARTEFACT = ROOT / "bench" / "results" / "2026-11-20-defect-108-split.json"

# The sample as it was recorded in docs/defects.md (defect 108), transcribed rather than re-measured: it is the only
# surviving record of that cell, and the point of the tool is that it can be read after the log is gone.
RECORDED = {
    "outages_ms": [20.6, 18.5, 19.3, 15.0, 12.3, 9.1, 686.2, 20.1],
    "rebinds_ms": [0.0, 0.0, 0.0, 16.0, 0.0, 0.0, 0.0, 15.0],
}


def test_the_recorded_windows_sample_puts_none_of_its_outage_in_the_rebind():
    result = split(RECORDED["outages_ms"], RECORDED["rebinds_ms"])
    assert result["over_budget"] == 1, "one sample of the eight was over 100 ms, not seven and not none"
    worst = result["worst"]
    assert worst["outage_ms"] == 686.2
    assert worst["rebind_ms"] == 0.0, (
        "the rebind column of that sample is 0 ms: charging the outlier to 'a slow rebind' would contradict the record")
    assert worst["unexplained_ms"] == 686.2
    assert worst["label"] == "not-ours:tick-or-loop", (
        "the label is the rule's reading of the numbers: the time was spent outside the code that rebinds")


def test_a_sample_whose_rebind_did_the_damage_is_labelled_ours():
    """The rule has to be able to say `ours:rebind` too, or it is a conclusion looking for evidence."""
    sample = split_one(140.0, rebind_ms=120.0, dispatch_ms=4.0, wake_late_ms=2.0)
    assert sample["label"] == "ours:rebind"
    assert sample["rebind_share_of_outage"] == round(120.0 / 140.0, 4)


def test_a_sample_inside_the_budget_says_so_and_is_not_dressed_up_as_a_pass():
    sample = split_one(38.0, rebind_ms=6.0, dispatch_ms=1.0, wake_late_ms=3.0)
    assert sample["label"] == "inside-budget"
    assert sample["over_budget"] is False
    assert sample["unexplained_ms"] == 28.0, "what no clock explains is kept as its own number, not folded into a share"


def test_a_clock_that_over_reports_is_clamped_rather_than_credited():
    """Sample 3 of the recorded run: a 16 ms rebind inside a 15 ms outage. Two clocks, one of them generous."""
    sample = split_one(15.0, rebind_ms=16.0)
    assert sample["unexplained_ms"] == 0.0
    assert sample["rebind_ms"] == 16.0, "the measurement is kept as measured; only the derived share is clamped"


def test_tick_lateness_is_attributed_to_the_tick_not_to_the_rebind():
    result = split([180.0], [2.0], [1.0], [150.0])
    sample = result["samples"][0]
    assert sample["wake_late_ms"] == 150.0
    assert sample["unexplained_ms"] == 27.0
    assert sample["label"] == "not-ours:tick-or-loop"
    assert "1 of 1 sample(s) over 100 ms" in result["verdict"]


def test_the_committed_artefact_is_what_the_tool_produces_today():
    """An artefact that cannot be regenerated is a screenshot; this one is compared, field by field."""
    assert ARTEFACT.exists(), f"{ARTEFACT} is the evidence defect 108 cites; regenerate it with tools/outage_split.py"
    committed = json.loads(ARTEFACT.read_text())
    regenerated = split(RECORDED["outages_ms"], RECORDED["rebinds_ms"])
    assert committed["schema"] == "blackbox.outage-split/1"
    assert committed["samples"] == regenerated["samples"]
    assert committed["verdict"] == regenerated["verdict"]
    assert committed["worst"]["label"] == "not-ours:tick-or-loop"


def test_the_tool_runs_from_the_command_line_with_the_numbers_in_the_document():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "outage_split.py"),
         "--outages", ",".join(str(v) for v in RECORDED["outages_ms"]),
         "--rebinds", ",".join(str(v) for v in RECORDED["rebinds_ms"]), "--json"],
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["worst"]["outage_ms"] == 686.2
    assert payload["n"] == 8
