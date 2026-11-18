"""STATUS.md is in the plan's §10 shape, and every number in it can be recomputed from the file it cites.

The failures this prevents are on record. §6 carried *"eight of nine cells green"*, *"two candidate causes"* and
*"that is the next ticket"* for a week after the cause was known (`O_BINARY`) and the cells were green. §2 carried
`53.9 ms/10k` for a recovery row whose JSON said `52.1`. And the gate table printed `1.231× (4,718,976 /
2,560,000)` — arithmetic that divides out to 1.843, because the numerator belonged to the other configuration.
Each of those is a sentence a reader could not check without diffing the file against the bench output.

So: one trunk sha, the six §10 sections, one label per labelled row, every measurement line citing its JSON, every
quoted ratio recomputed here (0.1 % tolerance), and no document may masquerade as another.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import git_history

STATUS = ROOT / "docs" / "STATUS.md"
STATE = ROOT / "docs" / "STATE.json"
LABELS = ("requires humans", "requires time", "not testable here", "blocked", "not measured")
SECTIONS = ("## Fulfilled", "## Labelled", "## Measurements",
            "## Deviations applied", "## Decisions taken", "## What is next")


def _text() -> str:
    return STATUS.read_text(encoding="utf-8")


def _section(heading: str) -> str:
    text = _text()
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start:] if nxt == -1 else text[start:nxt]


def _rows(section: str) -> list[list[str]]:
    out = []
    for line in section.splitlines():
        if line.startswith("|") and line.count("|") >= 3:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue                              # the header rule
            out.append(cells)
    return out


def test_status_names_exactly_one_trunk_and_it_is_in_the_past() -> None:
    git_history.require_history()
    header = _text().splitlines()[0]
    shas = re.findall(r"trunk `([0-9a-f]{7,40})`", header)
    assert len(shas) == 1, f"the header must name one trunk, found {shas}"
    assert git_history.is_ancestor(shas[0]), f"STATUS points at {shas[0]}, which is not an ancestor of HEAD"


def test_status_has_the_six_section_10_sections_in_order() -> None:
    text = _text()
    positions = []
    for heading in SECTIONS:
        assert heading in text, f"STATUS is missing {heading!r}"
        positions.append(text.index(heading))
    assert positions == sorted(positions), "the §10 sections are out of order"


def test_no_claim_in_status_is_hedged_with_two_answers() -> None:
    text = _text()
    # "53.9" narrowed to the stale recovery row it was written for (`53.9 ms/10k` where the row's own JSON said 52.1):
    # the proxy now reports a legitimate 53.980 ms p99 on the request-level rebind row, and a guard that fires on a
    # correct number is a guard that gets deleted instead of read.
    for stale in ("eight of nine", "candidate cause", "that is the next ticket", "53.9 ms/10k", "C1–C24", "C1-C24"):
        assert stale not in text, f"STATUS still carries the stale text {stale!r}"


def test_every_fulfilled_row_cites_something_a_reader_can_open() -> None:
    rows = [r for r in _rows(_section("## Fulfilled")) if r[0] != "Gate or budget row"]
    assert len(rows) >= 20, f"the Fulfilled table is too thin for L1: {len(rows)} rows"
    offenders = []
    for cells in rows:
        evidence = cells[-1]
        if not (re.search(r"(tests/|bench/results/|docs/)\S+", evidence) or re.search(r"run `\d{6,}`", evidence)):
            offenders.append(cells[0])
    assert offenders == [], "Fulfilled rows with no openable evidence: " + "; ".join(offenders)


def test_every_labelled_row_carries_exactly_one_allowed_label() -> None:
    rows = [r for r in _rows(_section("## Labelled")) if r[0] != "Item"]
    assert rows, "the Labelled table is empty; every unfinished item belongs here"
    offenders = []
    for cells in rows:
        label = cells[1]
        hits = [word for word in LABELS if word in label]
        if len(hits) != 1:
            offenders.append(f"{cells[0]}: {label!r}")
    assert offenders == [], "labels must be exactly one allowed string: " + "; ".join(offenders)


def test_every_measurement_line_cites_its_json() -> None:
    lines = [line for line in _section("## Measurements").splitlines() if line.startswith("- ")]
    assert len(lines) >= 10, f"one line per budget row: found {len(lines)}"
    offenders = [line[:60] for line in lines if ".json" not in line and "kill_matrix" not in line]
    assert offenders == [], "measurement lines with no raw JSON: " + "; ".join(offenders)


def test_every_quoted_ratio_recomputes_from_its_own_bytes() -> None:
    """`1.231× (4,718,976 / 2,560,000)` was printed for weeks: 4 718 976 / 2 560 000 = 1.843 (C38)."""
    offenders = []
    for number, line in enumerate(_text().splitlines(), start=1):
        if "×" not in line:
            continue
        pairs = re.findall(r"([0-9]+\.[0-9]+)× \(([\d\s,]+) / ([\d\s,]+)\)", line)
        if not pairs:
            continue
        for printed, numerator, denominator in pairs:
            printed = float(printed)
            num = float(numerator.replace(",", "").replace(" ", ""))
            den = float(denominator.replace(",", "").replace(" ", ""))
            if den == 0:
                offenders.append(f"line {number}: a ratio over zero")
                continue
            expected = num / den
            if abs(expected - printed) > 0.001 * expected:
                offenders.append(f"line {number}: {printed}× printed for {num:,.0f} / {den:,.0f} = {expected:.3f}")
    assert offenders == [], "STATUS arithmetic does not check out: " + "; ".join(offenders)


def test_every_state_baseline_key_appears_in_status() -> None:
    """A number in STATE that STATUS does not print is a number the reader cannot check (and vice versa)."""
    state = json.loads(STATE.read_text(encoding="utf-8"))["bench_baseline"]["keys"]
    text = _text().replace(",", "").replace(" ", "")
    for key, value in state.items():
        shown = {value, round(float(value), 3)}
        assert any(str(v).replace(" ", "") in text for v in shown), f"{key} = {value} is not printed in STATUS.md"


def test_the_test_count_line_matches_the_suite_size() -> None:
    """The header states a total; it is checked by running the collection, not by trusting the sentence.

    The pinned number belongs to one **dependency environment**: the one CI installs, `.[dev,capture,capture-sdks]`.
    Sixteen tests live behind `anthropic`, so a checkout with only `httpx` and `openai` collects 1146 and would "fail" a
    comparison against a 1162-test sentence — which is what happened on `127d187` for the opposite reason: the sentence
    said 1146 (measured in a lighter venv) and CI collected 1162. A missing extra means the number cannot be checked
    here, so this skips and names the missing package; with every extra present it still asserts equality.
    """
    import importlib.util
    missing = [name for name in ("httpx", "openai", "anthropic") if importlib.util.find_spec(name) is None]
    if missing:
        pytest.skip(f"the pinned count is the CI dependency set; no {', '.join(missing)} in this environment")
    match = re.search(r"\*\*Total: (\d+) tests\*\* — (\d+) pass here", _text())
    assert match, "STATUS must state the suite size"
    total, passed = int(match.group(1)), int(match.group(2))
    proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "--collect-only"],
                          cwd=ROOT, capture_output=True, text=True, timeout=600,
                          env={**os.environ, "PYTHONPATH": "src"})
    counts = re.findall(r"^tests/\S+: (\d+)$", proc.stdout, re.M)
    assert counts, proc.stdout[-500:] + proc.stderr[-500:]
    collected = sum(int(n) for n in counts)
    assert collected == total, f"STATUS says {total} tests, collection found {collected}"
    assert passed == total - 1, "the one skip is named in the same sentence"


def test_the_gate_file_and_state_agree_about_the_open_gates() -> None:
    gates = json.loads(STATE.read_text(encoding="utf-8"))["gates"]
    assert gates.get("G-Promise") == "pass", "G-Promise must be recorded once its gate file exists"
    gate_file = ROOT / "docs" / "gates" / "G-Promise.md"
    assert gate_file.exists(), "a gate with a verdict in STATE needs a signed file"
    text = gate_file.read_text(encoding="utf-8")
    assert re.search(r"verdict: pass", text), "the gate file must state its verdict"
    assert "self" in text.lower(), "the gate file must name who reviewed it, honestly"


def test_no_document_in_docs_is_another_document() -> None:
    """`docs/DECISIONS.md` was overwritten with STATUS.md by a bad write and nothing noticed (this turn)."""
    titles: dict[str, str] = {}
    for path in sorted((ROOT / "docs").glob("*.md")):
        first = path.read_text(encoding="utf-8").splitlines()[0].strip()
        assert first, f"{path.name} starts with a blank line"
        assert titles.get(first) is None, f"{path.name} and {titles[first]} share a title: {first!r}"
        titles[first] = path.name
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    assert decisions.startswith("# Decisions (append-only)"), "DECISIONS.md must be the decision log"
    assert len(re.findall(r"^\| D-\d{3} ", decisions, re.M)) >= 19, "the decision rows are missing"
