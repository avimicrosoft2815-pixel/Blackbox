"""P0-12: documentation must not point at files that do not exist."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
BACKTICK_DOC = re.compile(r"`(docs/[A-Za-z0-9_./-]+\.md)`")


def _doc_files():
    return sorted(DOCS.glob("*.md"))


def test_docs_exist():
    assert _doc_files(), "no documents found"


def test_relative_links_resolve():
    broken = []
    for doc in _doc_files():
        for target in LINK.findall(doc.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = (doc.parent / target.split("#")[0]).resolve()
            if not path.exists():
                broken.append(f"{doc.name} -> {target}")
    assert not broken, "broken relative link(s):\n" + "\n".join(broken)


PLAN_DOCS = {"BLACKBOX_FINAL_ARCHITECTURE_v1.1.md"}   # a plan may name files a later phase creates


def test_referenced_docs_exist():
    missing = []
    for doc in _doc_files():
        if doc.name in PLAN_DOCS or doc.name.startswith("stage-"):
            continue
        for rel in set(BACKTICK_DOC.findall(doc.read_text(encoding="utf-8"))):
            if not (ROOT / rel).exists():
                missing.append(f"{doc.name} mentions {rel}")
    # the v1.1 plan is allowed to name files that a later phase creates; nothing is pending as of P3B-09
    allowed_pending: set[str] = set()
    missing = [m for m in missing if not any(a in m for a in allowed_pending)]
    assert not missing, "references to missing docs:\n" + "\n".join(missing)
