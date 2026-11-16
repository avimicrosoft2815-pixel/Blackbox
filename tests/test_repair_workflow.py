"""P4-18 — the approved-fix workflow: preview, approval token, apply, verify.

The dangerous version of this feature is the obvious one: take a suggestion from a trace and write it. These tests are
written against that version, one refusal at a time. Every assertion below is something a patch-applier without a gate
would fail:

* **nothing is written by `preview`**, and nothing is written by `apply` without a token that matches *now*;
* **a file that changed after the preview aborts the apply** — the token is bound to each target's hash, so "the tree
  moved underneath the reviewer" is detected before the first byte, and the tree is byte-identical afterwards;
* **a proposal cannot escape the repository** — `..`, absolute paths, a symlinked file and a symlinked directory are
  four separate refusals;
* **the proposal's `tests` are never executed**, and a command that *would* prove it (a sentinel file) is left uncreated;
  the only commands that run are the ones passed to `verify --test`, and an empty list is reported as *unverified*, not
  as success;
* **a failure mid-apply restores everything it touched**, including deleting a file it created.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import blackbox.repair.apply as apply_mod
from blackbox.repair.apply import apply as apply_proposal
from blackbox.repair.apply import verify
from blackbox.repair.cli import main as repair_main
from blackbox.repair.proposal import ProposalError, assessed_risk, load

SCHEMA = "blackbox.repair-proposal/1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def handler():\n    raise RuntimeError('boom')\n", encoding="utf-8")
    return repo


def _proposal(tmp_path: Path, repo: Path, changes: list[dict], **extra) -> Path:
    document = {"schema": SCHEMA, "title": "catch the error instead of raising", "rationale": "the tape shows a raise",
                "tests": ["pytest tests/test_app.py -q"], "risk": ["changes control flow"], "changes": changes, **extra}
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _change(repo: Path, path: str, content: str, *, new: bool = False) -> dict:
    expected = None if new else _sha((repo / path).read_text(encoding="utf-8"))
    return {"path": path, "expected_sha256": expected, "content": content}


def _snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): (p.read_text(encoding="utf-8") if p.is_file() else "<dir>")
            for p in sorted(root.rglob("*")) if ".blackbox-repair" not in str(p)}


# ── preview: read-only, and it says what it will do ─────────────────────────────────────────────────────────────────
def test_preview_writes_nothing_and_shows_the_diff_the_paths_and_the_risk(tmp_path, capsys):
    repo = _repo(tmp_path)
    fixed = "def handler():\n    return None\n"
    proposal = _proposal(tmp_path, repo, [_change(repo, "src/app.py", fixed)])
    before = _snapshot(repo)
    assert repair_main(["preview", str(proposal), "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert _snapshot(repo) == before, "a preview must not touch the tree"
    assert "- replace `src/app.py`" in out
    assert "```diff" in out and "-    raise RuntimeError('boom')" in out and "+    return None" in out
    assert "## Risk" in out and "changes control flow" in out
    assert "**not run, and not runnable by this command**" in out
    assert "blackbox repair apply" in out and "--approve" in out


def test_preview_json_is_machine_readable_and_names_a_token(tmp_path, capsys):
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo, [_change(repo, "src/app.py", "def handler():\n    return 1\n")])
    assert repair_main(["preview", str(proposal), "--repo", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["token"] and len(payload["token"]) == 32
    assert payload["scope"]["files"] == 1 and payload["scope"]["replaces"] == ["src/app.py"]
    assert payload["changes"][0]["create"] is False


# ── the gate ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_apply_without_a_token_writes_nothing_and_says_the_token(tmp_path, capsys):
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo, [_change(repo, "src/app.py", "def handler():\n    return 1\n")])
    before = _snapshot(repo)
    assert repair_main(["apply", str(proposal), "--repo", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "refusing to apply without --approve" in err
    assert _snapshot(repo) == before


def test_apply_with_the_wrong_token_writes_nothing(tmp_path, capsys):
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo, [_change(repo, "src/app.py", "def handler():\n    return 1\n")])
    before = _snapshot(repo)
    assert repair_main(["apply", str(proposal), "--repo", str(repo), "--approve", "0" * 32]) == 2
    assert "does not match" in capsys.readouterr().err
    assert _snapshot(repo) == before


def test_the_right_token_applies_exactly_the_approved_bytes_and_keeps_a_backup(tmp_path, capsys):
    repo = _repo(tmp_path)
    fixed = "def handler():\n    return None\n"
    proposal_path = _proposal(tmp_path, repo, [_change(repo, "src/app.py", fixed)])
    token = load(str(proposal_path), repo=str(repo)).token()
    assert repair_main(["apply", str(proposal_path), "--repo", str(repo), "--approve", token]) == 0
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == fixed
    backups = list((tmp_path / ".blackbox-repair").rglob("00-app.py"))
    assert backups and "RuntimeError" in backups[0].read_text(encoding="utf-8")
    assert "unverified" in capsys.readouterr().out


def test_a_file_that_changed_after_the_preview_aborts_the_apply(tmp_path, capsys):
    repo = _repo(tmp_path)
    proposal_path = _proposal(tmp_path, repo, [_change(repo, "src/app.py", "def handler():\n    return 1\n")])
    proposal = load(str(proposal_path), repo=str(repo))
    token = proposal.token()
    (repo / "src" / "app.py").write_text("def handler():\n    return 'a human edited this'\n", encoding="utf-8")
    before = _snapshot(repo)
    assert repair_main(["apply", str(proposal_path), "--repo", str(repo), "--approve", token]) == 2
    err = capsys.readouterr().err
    assert "changed since preview" in err
    assert _snapshot(repo) == before, "the human's edit survives: that is the whole point of the token"


def test_a_proposal_edited_after_approval_is_refused(tmp_path, capsys):
    repo = _repo(tmp_path)
    proposal_path = _proposal(tmp_path, repo, [_change(repo, "src/app.py", "def handler():\n    return 1\n")])
    token = load(str(proposal_path), repo=str(repo)).token()
    document = json.loads(proposal_path.read_text(encoding="utf-8"))
    document["changes"][0]["content"] = "def handler():\n    import socket\n    return 2\n"
    proposal_path.write_text(json.dumps(document), encoding="utf-8")
    before = _snapshot(repo)
    assert repair_main(["apply", str(proposal_path), "--repo", str(repo), "--approve", token]) == 2
    assert _snapshot(repo) == before


# ── a proposal cannot reach outside the repository ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["../outside.txt", "../../etc/passwd", "/etc/passwd", "src/../../outside.txt"])
def test_a_path_that_escapes_the_repository_is_refused(tmp_path, path):
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo, [{"path": path, "expected_sha256": None, "content": "x"}])
    with pytest.raises(ProposalError) as caught:
        load(str(proposal), repo=str(repo))
    assert "absolute" in str(caught.value) or ".." in str(caught.value) or "outside" in str(caught.value)


def test_a_symlinked_file_is_refused(tmp_path):
    repo = _repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("secret\n", encoding="utf-8")
    os.symlink(outside, repo / "src" / "linked.py")
    proposal = _proposal(tmp_path, repo, [{"path": "src/linked.py", "expected_sha256": _sha("secret\n"),
                                           "content": "replaced\n"}])
    with pytest.raises(ProposalError) as caught:
        load(str(proposal), repo=str(repo))
    assert "symlink" in str(caught.value)


def test_a_symlinked_directory_is_refused(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", repo / "src" / "sub")
    proposal = _proposal(tmp_path, repo, [{"path": "src/sub/new.py", "expected_sha256": None, "content": "x\n"}])
    with pytest.raises(ProposalError) as caught:
        load(str(proposal), repo=str(repo))
    assert "symlink" in str(caught.value)


def test_a_proposal_may_create_a_file_but_must_be_right_about_it(tmp_path):
    repo = _repo(tmp_path)
    good = _proposal(tmp_path, repo, [{"path": "src/extra.py", "expected_sha256": None, "content": "x = 1\n"}])
    assert load(str(good), repo=str(repo)).changes[0].is_new()
    lying = tmp_path / "lying.json"
    lying.write_text(json.dumps({"schema": SCHEMA, "changes": [
        {"path": "src/app.py", "expected_sha256": None, "content": "x\n"}]}), encoding="utf-8")
    with pytest.raises(ProposalError) as caught:
        load(str(lying), repo=str(repo))
    assert "already exists" in str(caught.value)


def test_deletion_is_not_supported(tmp_path):
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo, [{"path": "src/app.py", "expected_sha256": _sha("x"), "content": None}])
    with pytest.raises(ProposalError) as caught:
        load(str(proposal), repo=str(repo))
    assert "deletion is not supported" in str(caught.value)


def test_a_wrong_schema_is_refused(tmp_path):
    repo = _repo(tmp_path)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema": "something-else", "changes": []}), encoding="utf-8")
    with pytest.raises(ProposalError) as caught:
        load(str(path), repo=str(repo))
    assert "schema must be" in str(caught.value)


# ── rollback ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_failure_part_way_restores_every_file_and_removes_what_it_created(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    changes = [_change(repo, "src/app.py", "def handler():\n    return 1\n"),
               {"path": "src/created.py", "expected_sha256": None, "content": "x = 1\n"}]
    proposal = load(str(_proposal(tmp_path, repo, changes)), repo=str(repo))
    token = proposal.token()
    before = _snapshot(repo)
    real_write = apply_mod._atomic_write
    calls = {"n": 0}

    def fail_on_second(path, data, mode):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_write(path, data, mode)

    monkeypatch.setattr(apply_mod, "_atomic_write", fail_on_second)
    with pytest.raises(ProposalError) as caught:
        apply_proposal(proposal, approval=token)
    assert "restored" in str(caught.value)
    assert _snapshot(repo) == before, "a half-applied repair is worse than no repair"
    assert not (repo / "src" / "created.py").exists()


# ── never execute what the proposal says ──────────────────────────────────────────────────────────────────────────
def test_the_proposals_suggested_tests_are_never_executed(tmp_path, capsys):
    repo = _repo(tmp_path)
    marker = tmp_path / "should-not-exist.txt"
    suggestion = f"touch {marker}"
    proposal = _proposal(tmp_path, repo, [_change(repo, "src/app.py", "def handler():\n    return 1\n")],
                         tests=[suggestion])
    assert repair_main(["preview", str(proposal), "--repo", str(repo)]) == 0
    token = load(str(proposal), repo=str(repo)).token()
    assert repair_main(["apply", str(proposal), "--repo", str(repo), "--approve", token]) == 0
    capsys.readouterr()
    assert not marker.exists(), "a command found in a proposal is data, not an instruction"


def test_verify_without_a_command_says_unverified_and_exits_nonzero(tmp_path, capsys):
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo, [_change(repo, "src/app.py", "def handler():\n    return 1\n")])
    assert repair_main(["verify", str(proposal), "--repo", str(repo)]) == 3
    out = capsys.readouterr().out
    assert "unverified" in out
    assert "PASS" not in out and "FAIL" not in out


def test_verify_runs_the_command_a_human_gave_and_reports_the_exit_code(tmp_path, capsys):
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo, [_change(repo, "src/app.py", "def handler():\n    return 1\n")])
    python = sys.executable
    assert repair_main(["verify", str(proposal), "--repo", str(repo),
                        "--test", f'"{python}" -c "print(42)"']) == 0
    out = capsys.readouterr().out
    assert "PASS  exit   0" in out and "42" in out
    assert "these are the commands that ran" in out
    assert repair_main(["verify", str(proposal), "--repo", str(repo),
                        "--test", f'"{python}" -c "import sys; sys.exit(7)"']) == 3
    assert "FAIL  exit   7" in capsys.readouterr().out


def test_verify_reports_a_failure_honestly_and_keeps_the_output_tail(tmp_path, capsys):
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo, [_change(repo, "src/app.py", "def handler():\n    return 1\n")])
    python = sys.executable
    code = repair_main(["verify", str(proposal), "--repo", str(repo),
                        "--test", f'"{python}" -c "print(\'assertion failed: 1 != 2\'); raise SystemExit(1)"'])
    out = capsys.readouterr().out
    assert code == 3
    assert "assertion failed: 1 != 2" in out
    assert verify([], cwd=str(repo))["verified"] is False


# ── risk is stated, not scored ───────────────────────────────────────────────────────────────────────────────────
def test_risk_notes_are_named_for_the_things_a_reviewer_should_notice(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    proposal_path = _proposal(tmp_path, repo, [
        _change(repo, ".github/workflows/ci.yml", "on: push\njobs: {}\n"),
        {"path": "src/new.py", "expected_sha256": None, "content": "import subprocess\n"}])
    proposal = load(str(proposal_path), repo=str(repo))
    notes = assessed_risk(proposal)
    assert any("CI configuration" in note for note in notes)
    assert any("creates a file" in note for note in notes)
    assert any("process or network module" in note for note in notes)
