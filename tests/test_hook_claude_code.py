"""The Claude Code adapter (P3B-05): somebody else's settings file, and our one block inside it.

The acceptance in the plan is one sentence — *install/remove round-trip on a settings file with unrelated keys leaves
them untouched* — and "untouched" has a weak reading (it still parses) and a strong one (**the same bytes**). These
tests hold the strong one, because a user's `settings.json` is where their permissions and their own hooks live, and a
recorder that reorders it has already made a change they did not ask for.

The rest is about refusing: an unknown schema, a `hooks` value that is not an object, a file that does not parse. Each
raises `BBX-E151` and writes nothing — a settings file is not a place to be creative.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.cli import hook as hook_cli
from blackbox.errors import BlackBoxError
from blackbox.hooks import claude_code, events

SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"], "deny": []},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-own-hook"}]}]},
    "env": {"FOO": "bar"},
}


def _settings_file(tmp_path: Path, body: dict | None = None, *, text: str | None = None) -> Path:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else json.dumps(body or SETTINGS, indent=2) + "\n", encoding="utf-8")
    return path


def test_install_then_remove_returns_the_file_byte_identical(tmp_path: Path) -> None:
    path = _settings_file(tmp_path)
    original = path.read_bytes()
    installed = claude_code.install(str(path), now_ns=1_700_000_000_000_000_000)
    assert installed.installed and installed.backup and Path(installed.backup).exists()
    after_install = path.read_bytes()
    assert after_install != original

    body = json.loads(after_install)
    assert body["model"] == "opus" and body["env"] == {"FOO": "bar"}
    assert body["permissions"] == SETTINGS["permissions"]
    # The user's own hook is still there, and ours sits beside it.
    own = [entry for entry in body["hooks"]["PreToolUse"]
           if not any(h.get(claude_code.MARKER) for h in entry.get("hooks") or [])]
    assert own == SETTINGS["hooks"]["PreToolUse"]
    assert set(claude_code.installed_events(body)) == set(events.CLAUDE_EVENTS)

    removed = claude_code.remove(str(path))
    assert removed.changed and not removed.installed
    assert path.read_bytes() == original, "remove must give the user their file back, byte for byte"
    assert not Path(installed.backup).exists(), "the backup is consumed by the restore it enabled"


def test_install_on_a_file_that_does_not_exist_yet_and_remove_leaves_no_hooks_key(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    result = claude_code.install(str(path))
    assert result.backup is None, "nothing to back up"
    assert json.loads(path.read_text())["hooks"], "the block was written"
    claude_code.remove(str(path))
    body = json.loads(path.read_text())
    assert "hooks" not in body, "an empty hooks key is our residue, not the user's"


def test_install_is_idempotent_in_effect_and_remove_never_duplicates_a_hook(tmp_path: Path) -> None:
    path = _settings_file(tmp_path)
    first = claude_code.install(str(path), now_ns=1)
    once = json.loads(path.read_text())
    second = claude_code.install(str(path), now_ns=2)
    twice = json.loads(path.read_text())
    ours_once = sum(1 for entry in once["hooks"]["PreToolUse"]
                    if any(h.get(claude_code.MARKER) for h in entry.get("hooks") or []))
    ours_twice = sum(1 for entry in twice["hooks"]["PreToolUse"]
                     if any(h.get(claude_code.MARKER) for h in entry.get("hooks") or []))
    assert (ours_once, ours_twice) == (1, 1), (
        "a second install must not leave a second entry: a hook that fires twice per prompt is a bug the user pays for")
    assert first.changed and not second.changed, "the second install says it changed nothing"
    assert claude_code.remove(str(path)).changed


def test_an_edit_made_between_install_and_remove_survives(tmp_path: Path) -> None:
    """The user changed their model while we were installed. Their edit wins; only our block goes."""
    path = _settings_file(tmp_path)
    claude_code.install(str(path), now_ns=1)
    body = json.loads(path.read_text())
    body["model"] = "sonnet"
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    removed = claude_code.remove(str(path))
    after = json.loads(path.read_text())
    assert removed.changed and removed.backup, "the backup is kept because the file is no longer ours to restore"
    assert after["model"] == "sonnet", "the user's edit survived"
    assert not claude_code.installed_events(after)
    assert after["permissions"] == SETTINGS["permissions"]


@pytest.mark.parametrize("text", ["not json", "[]", '{"hooks": "nope"}', '{"hooks": {"Stop": 3}}'])
def test_an_unknown_schema_is_refused_and_nothing_is_written(tmp_path: Path, text: str) -> None:
    path = _settings_file(tmp_path, text=text)
    before = path.read_bytes()
    with pytest.raises(BlackBoxError) as caught:
        claude_code.install(str(path))
    assert caught.value.code == "BBX-E151"
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.bak")) == []


def test_status_reads_without_writing(tmp_path: Path) -> None:
    path = _settings_file(tmp_path)
    before_bytes = path.read_bytes()
    plain = claude_code.status(str(path))
    assert plain == {**plain, "installed": False, "events": [], "missing": list(events.CLAUDE_EVENTS),
                     "exists": True, "backups": []}
    claude_code.install(str(path), now_ns=1)
    installed = claude_code.status(str(path))
    assert installed["installed"] and installed["missing"] == []
    assert installed["backups"] and installed["backups"][0].endswith(".bak")
    assert json.loads(before_bytes)["model"] == "opus", "status never rewrites what it reads"


def test_the_hook_command_reports_and_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _settings_file(tmp_path)
    assert hook_cli.main(["install", "--claude-code", "--settings", str(path), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["installed"] and body["path"] == str(path) and len(body["events"]) == 10
    assert hook_cli.main(["status", "--claude-code", "--settings", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["installed"] is True
    assert hook_cli.main(["remove", "--claude-code", "--settings", str(path)]) == 0
    assert "removed" in capsys.readouterr().out


def test_the_hook_command_refuses_a_schema_it_does_not_know(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _settings_file(tmp_path, text='{"hooks": "nope"}')
    assert hook_cli.main(["install", "--claude-code", "--settings", str(path)]) == 2
    assert "BBX-E151" in capsys.readouterr().err


def test_the_hook_command_needs_an_adapter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert hook_cli.main(["install", "--settings", str(tmp_path / "x.json")]) == 2
    assert "choose an adapter" in capsys.readouterr().err


def test_the_user_and_project_scopes_resolve_to_the_documented_paths(tmp_path: Path) -> None:
    assert claude_code.default_path("user", home=str(tmp_path)) == str(tmp_path / ".claude" / "settings.json")
    assert claude_code.default_path("project").endswith(str(Path(".claude") / "settings.json"))
    with pytest.raises(ValueError):
        claude_code.default_path("global")
