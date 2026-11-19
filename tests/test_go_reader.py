"""P11-15 — the Go reader: what can be checked about it on a machine with no Go toolchain.

The row asks for a reader written from `docs/format.md` alone that decodes the conformance vectors. The reader exists
(`tools/readers/go/main.go`), and the machine the rest of this branch was built on has **no Go compiler**, so it has never
been executed here. This file does not pretend otherwise: it checks the things that can be checked without a compiler,
and it makes the missing measurement impossible to forget.

What it checks:

* the reader is Go, uses only the standard library, and says in its own header that it was written from the document;
* every refusal code it can emit is a code the registry declares and the Python reader also uses — a second reader that
  invents a code is not reproducing the first one's answers;
* the selftest script parses as POSIX shell and its expectations are the ones `docs/format.md` implies;
* CI compiles and runs it: a `go-reader` job exists and calls that script. **The measurement lives there, not here.**

What it does not check: whether the Go compiles, and whether it reads a capsule correctly. Those are the CI cell's job,
and until that cell is green the row is *written and unmeasured here*.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GO_DIR = ROOT / "tools" / "readers" / "go"
MAIN = GO_DIR / "main.go"
SELFTEST = GO_DIR / "selftest.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_the_reader_exists_and_is_go() -> None:
    assert MAIN.exists(), "the Go reader is the row's deliverable and is missing"
    text = MAIN.read_text(encoding="utf-8")
    assert text.startswith("// bbx-read is an independent reader"), "the header says what it is and why it exists"
    assert "docs/format.md" in text, "a reader that does not name the document it was written from cannot answer the row"
    assert "package main" in text
    assert (GO_DIR / "go.mod").exists()


def test_it_imports_only_the_standard_library() -> None:
    imports = re.findall(r'^\t"([^"]+)"', MAIN.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert imports, "no imports found: the parse is wrong, not the file"
    outside = [name for name in imports if "." in name.split("/")[0]]
    assert outside == [], f"non-stdlib import(s): {outside}"


def test_every_code_it_refuses_with_is_a_declared_code() -> None:
    """The point of a second reader is the same answers. A new code would be a different answer."""
    sys_path = ROOT / "src"
    import sys

    sys.path.insert(0, str(sys_path))
    from blackbox.errors import REGISTRY

    codes = {code.strip('"') for code in re.findall(r'"BBX-E\d{3}"', MAIN.read_text(encoding="utf-8"))}
    assert {"BBX-E402", "BBX-E403", "BBX-E404", "BBX-E405", "BBX-E406", "BBX-E407", "BBX-E409"} <= codes, sorted(codes)
    undeclared = sorted(code for code in codes if code not in REGISTRY)
    assert undeclared == [], f"refusal codes that are not in the registry: {undeclared}"


def test_the_exit_codes_match_the_python_verifier() -> None:
    text = MAIN.read_text(encoding="utf-8")
    assert "os.Exit(0)" in text and "os.Exit(3)" in text and "os.Exit(4)" in text and "os.Exit(2)" in text
    assert "0  every check this reader implements passed" in text


def test_the_selftest_parses_and_names_its_expectations() -> None:
    if shutil.which("sh") is None:                     # pragma: no cover - every platform in CI has sh except Windows
        pytest.skip("no POSIX shell on this machine")
    completed = subprocess.run(["sh", "-n", str(SELFTEST)], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    text = SELFTEST.read_text(encoding="utf-8")
    for expectation in ("conformance vectors", "a real capsule", "exit 4", "exit 3", "traversal"):
        assert expectation in text, f"the selftest does not exercise: {expectation}"
    assert SELFTEST.stat().st_mode & 0o111 if __import__("os").name == "posix" else True


def test_ci_compiles_and_runs_it() -> None:
    """The only place this file is executed. If this job is removed, the row is unmeasured and this test fails."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "go-reader:" in workflow
    assert "actions/setup-go@v5" in workflow
    assert "tools/readers/go/selftest.sh" in workflow, "CI must run the same script a person runs"


def test_the_reader_says_where_the_document_was_silent() -> None:
    """A reader written from a document records the places the document did not answer — that is its finding."""
    text = MAIN.read_text(encoding="utf-8")
    assert text.count("// DOC:") >= 4, "the DOC: markers are how a gap in docs/format.md becomes visible"
