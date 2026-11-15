"""P3B-09's acceptance, enforced: **every row has either a verified version or the word "unverified"**.

A table like this is the easiest document in a repository to let rot, because nothing about a row *looks* wrong: a
version number copied from a blog post reads exactly like a version number that was run. So the checks are mechanical
and narrow. They apply to the tables whose last column is headed **verdict** — the ones that make a claim about
verification — and they are:

* every row's verdict cell either contains the word **unverified** or names a version **and** an ISO date, in that row;
* a row that says *verified* carries both, so "verified on some version of something" cannot pass;
* a row labelled unverified says **what is doubted**, because a bare "unverified" is a shrug nobody can ever act on;
* every test file the document points at exists, and the loopback test it tells the reader to run really collects;
* the document is its own document (no shared title) and does not smuggle in a reassuring phrase.
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
DOC = ROOT / "docs" / "attach.md"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_forbidden_words import scan as scan_banned  # one list of banned phrases, not two

DATE = re.compile(r"\b20\d\d-\d\d-\d\d\b")
VERSION = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _tables() -> list[tuple[list[str], list[tuple[int, list[str]]]]]:
    """Every markdown table as `(header cells, [(line number, cells), …])`, separator rows skipped."""
    tables: list[tuple[list[str], list[tuple[int, list[str]]]]] = []
    lines = DOC.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("|") and index + 1 < len(lines) and \
                set(lines[index + 1].strip()) <= set("|-: ") and "-" in lines[index + 1]:
            header = _cells(lines[index])
            rows: list[tuple[int, list[str]]] = []
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append((index + 1, _cells(lines[index])))
                index += 1
            tables.append((header, rows))
            continue
        index += 1
    return tables


def _verdict_tables() -> list[tuple[list[str], list[tuple[int, list[str]]]]]:
    chosen = [table for table in _tables() if table[0] and table[0][-1].strip().lower() == "verdict"]
    return chosen


def test_the_document_exists_and_is_its_own_document() -> None:
    assert DOC.exists()
    titles: dict[str, str] = {}
    for path in sorted((ROOT / "docs").glob("*.md")):
        first = path.read_text(encoding="utf-8").splitlines()[0].strip()
        assert titles.get(first) is None, f"{path.name} and {titles[first]} share a title"
        titles[first] = path.name
    assert DOC.read_text(encoding="utf-8").startswith("# Attach")


def test_the_verdict_tables_are_filled_in() -> None:
    tables = _verdict_tables()
    assert len(tables) >= 2, f"expected a client matrix and an agent matrix, found {len(tables)} verdict table(s)"
    total = sum(len(rows) for _header, rows in tables)
    assert total >= 18, f"only {total} verdict rows: the matrix is not filled in"


def test_every_verdict_row_has_a_version_and_a_date_or_the_word_unverified() -> None:
    bad: list[str] = []
    for header, rows in _verdict_tables():
        for number, cells in rows:
            last = cells[-1]
            if "unverified" in last.lower():
                continue
            joined = " | ".join(cells)
            if not (DATE.search(joined) and VERSION.search(joined)):
                bad.append(f"line {number} ({cells[0]}): {last!r} — no version+date and no 'unverified'")
            assert header[-1].lower() == "verdict"
    assert not bad, "rows without a verifiable verdict:\n" + "\n".join(bad)


def test_a_verified_row_carries_a_versions_column_too() -> None:
    """The verdict cell says *verified*; the row must also name what was run, not only that something was."""
    for _header, rows in _verdict_tables():
        for number, cells in rows:
            if "verified" not in cells[-1].lower() or "unverified" in cells[-1].lower():
                continue
            body = " | ".join(cells[:-1])
            assert VERSION.search(body), f"line {number} ({cells[0]}) claims verification without a version"
            assert DATE.search(body), f"line {number} ({cells[0]}) claims verification without a date"


def test_an_unverified_row_says_what_is_doubted() -> None:
    """A labelled row with no reason cannot be fixed by the next reader; the Codex and Ollama rows are the models."""
    for _header, rows in _verdict_tables():
        for number, cells in rows:
            if "unverified" not in cells[-1].lower():
                continue
            explanation = " | ".join(cells[:-1])
            assert len(explanation) > 20, f"line {number} ({cells[0]}) is labelled but not explained"


def test_the_unverified_variables_in_the_command_table_say_why() -> None:
    text = DOC.read_text(encoding="utf-8")
    for name in ("OLLAMA_HOST", "GOOGLE_GEMINI_BASE_URL"):
        line = next(line for line in text.splitlines() if name in line and line.strip().startswith("|"))
        assert "unverified" in line.lower(), line
        assert "unverified" in line.lower() and ":" in line.split("unverified", 1)[1], \
            f"{name} is labelled but the doubt is not written down: {line[:120]}"


def test_the_tests_it_names_exist_and_the_one_it_tells_you_to_run_collects() -> None:
    text = DOC.read_text(encoding="utf-8")
    named = set(re.findall(r"(tests/[A-Za-z0-9_./-]+\.py)", text))
    assert named, "the document names no test, so no row can be checked"
    for relative in named:
        assert (ROOT / relative).exists(), f"the document points at {relative}, which does not exist"
    done = subprocess.run([sys.executable, "-m", "pytest", "tests/test_attach_sdks.py", "--collect-only", "-q"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300,
                          env={**os.environ, "PYTHONPATH": "src"})
    assert done.returncode == 0, done.stdout[-800:] + done.stderr[-800:]
    collected = int(re.search(r": (\d+)$", done.stdout.strip(), re.M).group(1))
    defined = len(re.findall(r"^def test_", (ROOT / "tests" / "test_attach_sdks.py").read_text(), re.M))
    assert collected >= defined, f"collected {collected} of {defined} tests, so a row has no test behind it"


def test_the_document_does_not_smuggle_in_a_reassuring_phrase() -> None:
    """The banned list lives in one place (`tests/test_forbidden_words.py`); this file only asks it."""
    found = scan_banned(DOC.read_text(encoding="utf-8"))
    assert found == [], f"banned phrase(s) in docs/attach.md: {found}"


@pytest.mark.parametrize("heading", ["## 1. The one command", "## 2. Clients that take a base URL",
                                     "## 3. Coding agents through their own hooks", "## 4. What attach cannot see",
                                     "## 5. Checking a row yourself"])
def test_the_document_answers_the_questions_the_ticket_lists(heading: str) -> None:
    assert heading in DOC.read_text(encoding="utf-8")


def test_state_and_the_document_agree_that_this_ticket_exists() -> None:
    buckets = json.loads((ROOT / "docs" / "STATE.json").read_text(encoding="utf-8"))["lane_status"]["L3"]
    listed = buckets["done"] + buckets["wip"] + buckets["todo"]
    assert "P3B-09" in listed
    if "P3B-09" in buckets["done"]:
        assert DOC.exists()
