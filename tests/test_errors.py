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
