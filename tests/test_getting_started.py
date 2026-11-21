"""The beginner walkthrough (`docs/getting-started.md`) checked against the product, not against itself.

A page written for somebody who knows nothing is the easiest page in a repository to let rot, because its reader cannot
tell a stale command from a missing one. These tests are the reader's substitute for knowing:

* the eight steps exist and keep the order the mandate names — install, attach, confirm, open, findings, brief, approve,
  run the authorized checks;
* every `blackbox <cmd>` the page tells a beginner to type is a command the CLI actually has (a page naming a command
  that was renamed is worse than no page);
* every `BBX-E…` code it names is in the register the code refuses with, and every `BBX-E…` it names in `docs/errors.md`
  agrees with the registry;
* every file it points at — a test, a bench result, a tool — exists;
* every step ends with an Evidence sentence, so "we proved it" cannot quietly become "we said it";
* the page says what has *not* been done by a person, in the words the rest of the repository uses.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "getting-started.md"

#: The eight steps, in the order the mandate lists them, as the page's own headings spell them.
STEPS = [
    "Step 1 — Install it",
    "Step 2 — Attach an app you already use",
    "Step 3 — Confirm the recording happened",
    "Step 4 — Open the incident",
    "Step 5 — Understand the findings",
    "Step 6 — Prepare a brief for a coding assistant",
    "Step 7 — Review and approve the proposal",
    "Step 8 — Run the authorized checks, and read the result honestly",
]


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_page_exists_and_is_reachable_from_the_docs_index() -> None:
    assert PAGE.is_file(), "docs/getting-started.md is gone"
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "(getting-started.md)" in index, "the docs index does not link the walkthrough a beginner needs first"


def test_the_eight_steps_are_present_in_order() -> None:
    text = _page()
    positions = []
    for step in STEPS:
        assert step in text, f"the walkthrough lost its heading: {step}"
        positions.append(text.index(step))
    assert positions == sorted(positions), "the steps are out of order"
    # A step heading that is not one of the eight would be a step nobody checks.
    headings = re.findall(r"^## (Step .*)$", text, re.M)
    assert headings == STEPS, headings


def test_every_command_the_page_tells_a_beginner_to_type_exists() -> None:
    from blackbox.__main__ import KNOWN

    text = _page()
    named = set(re.findall(r"(?:^|\s)blackbox ([a-z][a-z-]*)", text))
    unknown = sorted(name for name in named if name not in KNOWN)
    assert unknown == [], f"the walkthrough names commands the CLI does not have: {unknown}"


def test_every_refusal_code_it_names_is_in_the_register() -> None:
    from blackbox.errors import REGISTRY

    codes = set(re.findall(r"BBX-E\d{3}", _page()))
    assert codes, "the walkthrough names no refusal code, so 'if it goes wrong' became a shrug"
    unknown = sorted(code for code in codes if code not in REGISTRY)
    assert unknown == [], f"the walkthrough names codes the code never raises: {unknown}"


def test_the_error_page_agrees_with_the_register_for_every_code_it_quotes() -> None:
    from blackbox.errors import REGISTRY

    errors_doc = (ROOT / "docs" / "errors.md").read_text(encoding="utf-8")
    for code in sorted(set(re.findall(r"BBX-E\d{3}", _page()))):
        assert code in errors_doc, f"{code} is quoted to a beginner but is not on the error page"
        # The register's first element is the message; the page has to carry the same words.
        message = REGISTRY[code][0]
        assert message.split(":")[0][:24].lower() in errors_doc.lower(), (code, message)


def test_every_file_it_points_at_exists() -> None:
    text = _page()
    missing = []
    for pattern in (r"`(tests/[A-Za-z0-9_./-]+\.py)`", r"`(bench/results/[A-Za-z0-9_./-]+)`",
                    r"`(src/blackbox/[A-Za-z0-9_./-]+\.py)`", r"`(docs/[A-Za-z0-9_./-]+\.md)`",
                    r"`(tools/[A-Za-z0-9_./-]+)`", r"`(\.github/workflows/[A-Za-z0-9_.-]+)`"):
        for match in re.findall(pattern, text):
            if not (ROOT / match).exists():
                missing.append(match)
    assert missing == [], f"the walkthrough points at files that are not there: {sorted(set(missing))}"


def test_every_step_states_its_evidence() -> None:
    text = _page()
    sections = re.split(r"^## ", text, flags=re.M)
    for step in STEPS:
        body = next((section for section in sections if section.startswith(step)), None)
        assert body is not None, step
        assert "**Evidence.**" in body, f"{step} claims something with no evidence sentence"
        evidence = body.split("**Evidence.**", 1)[1]
        assert re.search(r"(tests/|bench/results/|\.github/workflows/|run `\d{6,}`)", evidence), (
            f"{step}'s evidence sentence names nothing a reader can open or run"
        )


def test_it_says_what_has_not_been_done_by_a_person() -> None:
    text = _page()
    lower = text.lower()
    assert "not been" in lower or "has not" in lower, "the page never says what is unrun"
    assert "unsigned" in lower, "the page recommends a Windows installer without saying it is unsigned"
    assert "unverified" in lower, "the page never uses the word the rest of the repo uses for unrun clients"
    # The two surfaces a runner cannot exercise are named, not glossed.
    assert "wizard" in lower and "window" in lower


@pytest.mark.parametrize("phrase", ["no account", "cannot send them", "network call"])
def test_the_privacy_promise_is_stated_in_the_page(phrase: str) -> None:
    # Prose wraps, so match across line breaks rather than forcing the sentence onto one line.
    flat = re.sub(r"\s+", " ", _page()).lower()
    assert phrase in flat, f"the walkthrough lost its privacy sentence ({phrase!r})"
