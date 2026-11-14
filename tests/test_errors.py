"""P0-05: the error registry is closed — an undeclared code is a bug, not a warning."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blackbox import errors  # noqa: E402


def test_registry_is_populated_and_formatted():
    assert len(errors.REGISTRY) >= 20
    for code, entry in errors.REGISTRY.items():
        assert code.startswith("BBX-E") and len(code) == 8, code
        message, remedy = entry
        assert message, code
        assert remedy, f"{code} needs a remedy string (the operator must be told what to do)"


def test_unknown_code_fails_loudly():
    with pytest.raises(KeyError):
        errors.bbx("BBX-E999", "nope")


def test_error_payload_roundtrip():
    err = errors.bbx("BBX-E120", "tmpfs")
    assert err.code == "BBX-E120"
    assert "tmpfs" in str(err)
    assert err.to_dict()["code"] == "BBX-E120"


def test_every_documented_code_exists_in_docs():
    root = Path(__file__).resolve().parents[1]
    text = (root / "docs" / "format.md").read_text(encoding="utf-8")
    text += (root / "docs" / "guarantees.md").read_text(encoding="utf-8")
    documented = {c for c in errors.REGISTRY if c in text}
    assert documented, "no BBX-E code appears in the docs"


# ── A5: the registry and the plan's appendix must agree in both directions ──
import re  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "your", "when", "does", "not"}


def _plan_codes() -> dict[str, str]:
    """Every `BBX-E###` in the plan with the meaning text of the table row that defines it."""
    text = (ROOT / "docs" / "BLACKBOX_FINAL_ARCHITECTURE_v1.1.md").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.search(r"\|\s*`BBX-E(\d{3})`\s*\|\s*([^|]+)\|", line)
        if m:
            out[f"BBX-E{m.group(1)}"] = m.group(2).strip()
            continue
        # the plan also lists one range row for the capsule codes: `BBX-E401`–`E411`
        r = re.search(r"`BBX-E(\d{3})`\s*[–-]\s*`?E(\d{3})`?\s*\|\s*([^|]+)\|", line)
        if r:
            low, high, meaning = int(r.group(1)), int(r.group(2)), r.group(3).strip()
            for n in range(low, high + 1):
                out[f"BBX-E{n}"] = meaning
    return out


def test_registry_and_plan_appendix_agree():
    plan = _plan_codes()
    registry = set(errors.REGISTRY)
    assert not (registry - set(plan)), f"declared but not in the plan appendix: {sorted(registry - set(plan))}"
    missing = set(plan) - registry
    assert not missing, f"in the plan appendix but not declared: {sorted(missing)}"


def test_code_meaning_matches_plan_meaning():
    """A code may not be repurposed: the registry message and the plan's row must share a real word.

    Cheap, but it is exactly what would have caught `BBX-E140` standing for "segment preallocation failed"
    while the plan defines it as "proxy: upstream not in allowlist".
    """
    plan = _plan_codes()
    problems = []
    for code, (message, _remedy) in errors.REGISTRY.items():
        if code not in plan:
            continue
        ours = {w.lower().strip("(),.") for w in message.split()} - STOP
        theirs = {w.lower().strip("(),.") for w in plan[code].split()} - STOP
        if not (ours & theirs):
            problems.append(f"{code}: registry {message!r} vs plan {plan[code]!r}")
    assert not problems, "code meaning drifted from the plan:\n" + "\n".join(problems)


def test_every_code_used_in_src_is_declared():
    used = set()
    for path in (ROOT / "src").rglob("*.py"):
        used |= set(re.findall(r"BBX-E\d{3}", path.read_text(encoding="utf-8")))
    assert used <= set(errors.REGISTRY), f"used but undeclared: {sorted(used - set(errors.REGISTRY))}"


def test_errors_doc_is_generated_from_the_registry():
    doc = (ROOT / "docs" / "errors.md").read_text(encoding="utf-8")
    for code in errors.REGISTRY:
        assert f"`{code}`" in doc, code
