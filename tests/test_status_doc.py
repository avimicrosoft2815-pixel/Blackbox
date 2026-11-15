"""C33 — `docs/STATUS.md` says one thing per claim, and the numbers it prints are the numbers in the JSONs.

The failure this prevents is on record: §6 carried *"eight of nine cells green"*, *"two candidate causes"* and
*"that is the next ticket"* for a week after the cause was known (`O_BINARY`) and the cells were green, and §2
carried `53.9 ms/10k` for a recovery row whose JSON said `52.1`. A reader could not tell which sentence was current,
and a reviewer had to diff the file against the bench output to find out.

So: one HEAD, one verdict per row, one verdict per CI cell, every baseline number present and matching
`docs/STATE.json`, and none of the phrases that made the past version unreadable.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import git_history

STATUS = ROOT / "docs" / "STATUS.md"
STATE = ROOT / "docs" / "STATE.json"

VERDICT_WORDS = ("PASS", "FAIL", "reported", "not gated")


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


def test_status_names_exactly_one_head_and_it_is_in_the_past() -> None:
    git_history.require_history()
    header = "\n".join(_text().splitlines()[:6])
    heads = re.findall(r"\*\*HEAD\*\*\s+`([0-9a-f]{7,40})`", header)
    assert len(heads) == 1, f"the header must name one HEAD, found {heads}"
    sha = heads[0]
    assert git_history.is_ancestor(sha), f"STATUS points at {sha}, which is not an ancestor of HEAD"


def test_no_claim_in_status_is_hedged_with_two_answers() -> None:
    text = _text()
    for stale in ("eight of nine", "candidate cause", "that is the next ticket", "53.9"):
        assert stale not in text, f"STATUS still carries the stale text {stale!r}"


def test_every_ci_cell_carries_one_verdict() -> None:
    row = next(r for r in _rows(_section("## 6.")) if r[0].startswith("**C7 CI**"))
    cell = " | ".join(row[1:])
    runs = set(re.findall(r"run `(\d{6,})`", cell))
    assert len(runs) == 1, f"one CI run per cell set, found {sorted(runs)}"
    for name in ("ubuntu", "macOS", "Windows", "docs"):
        hits = re.findall(name + r"\s*×\s*\d+\s+\*\*(pass|fail)\*\*", cell)
        assert len(hits) == 1, f"{name}: expected exactly one verdict in the CI cell, found {hits}"
        assert hits[0] == "pass", f"{name} is labelled {hits[0]}"


def test_every_gate_row_carries_exactly_one_verdict() -> None:
    rows = [r for r in _rows(_section("## 2.")) if r[0] != "Row"]
    assert len(rows) >= 6, "the §15.1 table is missing"
    for cells in rows:
        verdict = cells[-2] if len(cells) >= 2 else ""
        found = [w for w in (*VERDICT_WORDS, "adopt", "bound") if w in verdict]
        assert found, f"row {cells[0]!r} has no verdict in {verdict!r}"
        assert "PASS" not in verdict or "FAIL" not in verdict, \
            f"row {cells[0]!r} says both PASS and FAIL: {verdict!r}"


def test_the_recovery_row_is_labelled_against_its_own_row() -> None:
    rows = _rows(_section("## 2."))
    row = next(r for r in rows if "Recovery" in r[0])
    line = " | ".join(row)
    assert "120 ms" in line, "the row's budget must be printed next to the number"
    assert "ms per 10 k frames" in line and "22 312" in line, "the row needs the ms/10k figure and the N frames"
    assert re.search(r"n \d+ rounds", line), "the row must say how many rounds stand behind the percentiles"
    assert "**PASS**" in line, "the verdict must be stated, not implied"


def test_amplification_verdicts_name_their_configuration() -> None:
    section = _section("## 2.")
    default = next(line for line in section.splitlines() if "16 KiB" in line and "1.231" in line)
    assert "PASS" in default, "the default config's verdict must be stated"
    other = next(line for line in section.splitlines() if "1.843" in line and "per-event" in line)
    assert "reported" in other and "2×" in other, \
        "the per-event figure must be reported, with the hard-fail bound named"


def test_every_state_baseline_key_appears_in_status() -> None:
    """A number in STATE that STATUS does not print is a number the reader cannot check (and vice versa)."""
    state = json.loads(STATE.read_text(encoding="utf-8"))["bench_baseline"]["keys"]
    text = _text().replace(",", "")
    for key, value in state.items():
        shown = {value, round(float(value), 3), int(value) if float(value).is_integer() else value}
        assert any(str(v) in text for v in shown), f"{key} = {value} is not printed in STATUS.md"


def test_the_test_count_line_matches_the_suite_size() -> None:
    """The header states a total; it is checked by running the collection, not by trusting the sentence."""
    match = re.search(r"Total: \*\*(\d+) tests\*\* — (\d+) pass here", _text())
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


def test_every_quoted_ratio_recomputes_from_its_own_bytes() -> None:
    """C38 — `1.231× (4,718,976 / 2,560,000)` was printed for a week; 4 718 976 is the *per-event* numerator.

    1.231 is `results.ext4.amplification_by_batch[batch_bytes=16384]` = 3 150 352 / 2 560 000, and 4 718 976 /
    2 560 000 = 1.843, which is the per-publish figure. Two different "amplification" numbers were published under
    one label with the other number's arithmetic beside it, so a reader who checked the division found 1.843 where
    the row said 1.231. The file cannot be trusted on arithmetic it does not do itself, so this does the division.
    """
    offenders = []
    for number, line in enumerate(_text().splitlines(), start=1):
        if "×" not in line:
            continue
        pairs = re.findall(r"\(([\d\s,]+) / ([\d\s,]+)\)", line)
        if not pairs:
            continue
        quoted = [float(value) for value in re.findall(r"([0-9]+\.[0-9]+)×", line)]
        for numerator, denominator in pairs:
            num = float(numerator.replace(",", "").replace(" ", ""))
            den = float(denominator.replace(",", "").replace(" ", ""))
            if den == 0 or not quoted:
                offenders.append(f"line {number}: a ratio with no multiplier beside it")
                continue
            expected, printed = num / den, quoted.pop(0)
            if abs(expected - printed) > 0.001 * expected:
                offenders.append(f"line {number}: {printed}× printed for {num:,.0f} / {den:,.0f} = {expected:.3f}")
    assert offenders == [], "STATUS.md arithmetic does not check out: " + "; ".join(offenders)


def test_every_numeric_row_in_the_gate_table_cites_its_json() -> None:
    """A row without a source is a claim a reviewer cannot follow back to a file (C38)."""
    rows = [line for line in _section("## 2.").splitlines()
            if line.startswith("|") and re.search(r"\d+(\.\d+)?", line) and "---" not in line]
    bodies = [row for row in rows if not row.lower().startswith("| row |")]
    offenders, carried = [], None
    for row in bodies:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if ".json" in row:
            carried = cells[-1]
            continue
        if cells[-1] == "same" and carried is not None:      # "same" means the row directly above it
            continue
        offenders.append(cells[0])
    assert offenders == [], "gate-table rows with no JSON source: " + "; ".join(offenders)
