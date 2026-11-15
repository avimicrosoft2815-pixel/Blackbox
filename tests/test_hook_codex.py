"""The Codex adapter (P3B-06): twelve event names, one honest assumption, and a refusal where the assumption could bite.

The plan's acceptance for this ticket is *"fixture payloads for the 12 events mapped; unknown event ⇒
`MARK{tool_event}`"*. That is what can be verified here, and it is what these tests check — over recorded payloads, not
over a live tool.

What cannot be verified here is Codex's own settings location and the shape of its hook block: no documentation for it
could be read from this sandbox. So the build **labels** it — `codex.SPEC.verified is False`, `status` prints
`schema_verified: false` and the note, and `docs/STATUS.md` carries a labelled row — and the tests below assert the
label exists, because an assumption that nothing prints is an assumption that becomes a claim. Everything the
assumption could cost is refused or made visible instead: an unknown `hooks` shape raises `BBX-E151` and writes nothing,
an unknown event name becomes a `MARK`, and `remove` gives the file back byte-identically.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cli import hook as hook_cli
from blackbox.errors import BlackBoxError
from blackbox.hooks import codex, events, inbox
from blackbox.tape import scanner
from blackbox.tape.writer import TapeWriter

SETTINGS = {
    "model": "gpt-5-codex",
    "approval_policy": "on-request",
    "hooks": {"notify": [{"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}]},
    "mcp_servers": {"fs": {"command": "fs-server"}},
}


def _settings(tmp_path: Path, body: dict | None = None, *, text: str | None = None) -> Path:
    path = tmp_path / ".codex" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else json.dumps(body or SETTINGS, indent=2) + "\n", encoding="utf-8")
    return path


# ── the twelve events ─────────────────────────────────────────────────────────────────────────────────────────────
def test_the_table_has_the_twelve_names_the_ticket_asks_for() -> None:
    assert len(events.CODEX_EVENTS) == 12
    assert len(set(events.CODEX_EVENTS)) == 12


@pytest.mark.parametrize("event", events.CODEX_EVENTS)
def test_every_codex_event_maps_to_its_frames(event: str) -> None:
    body = {"session_id": "c1", "tool_name": "shell", "tool_input": {"cmd": ["ls"]},
            "prompt": "fix the build", "message": "turn done", "error": "boom", "turn": 2}
    records = events.map_event(events.CODEX, event, body)
    assert len(records) == 1
    record = records[0]
    expected = {
        "session.start": (C.KIND_RUN_START, None, None),
        "session.end": (C.KIND_RUN_END, None, None),
        "turn.start": (C.KIND_NODE_OPEN, events.TURN, None),
        "turn.end": (C.KIND_NODE_CLOSE, None, events.TURN),
        "user.prompt": (C.KIND_MARK, None, None),
        "tool.call": (C.KIND_NODE_OPEN, "shell", None),
        "tool.result": (C.KIND_NODE_CLOSE, None, "shell"),
        "tool.error": (C.KIND_NODE_CLOSE, None, "shell"),
        "agent.message": (C.KIND_MARK, None, None),
        "context.compact": (C.KIND_MARK, None, None),
        "notify": (C.KIND_MARK, None, None),
        "error": (C.KIND_MARK, None, None),
    }[event]
    assert (record.kind, record.opens, record.closes) == expected
    assert record.payload, "every mapped event carries a payload a reader can use"


@pytest.mark.parametrize("event", ["pre.tool", "TurnComplete", "some.future.event"])
def test_an_unknown_codex_event_becomes_a_mark_that_names_it(event: str) -> None:
    records = events.map_event(events.CODEX, event, {})
    assert records[0].kind == C.KIND_MARK
    assert records[0].payload["kind"] == "tool_event" and records[0].payload["raw_name"] == event


def test_codex_prompts_and_tool_arguments_are_hashes_not_content() -> None:
    body = {"prompt": "the ssh key is in ~/.ssh/id_rsa", "tool_name": "shell",
            "tool_input": {"cmd": ["cat", "/home/me/.ssh/id_rsa"]}}
    prompt = events.map_event(events.CODEX, "user.prompt", body)[0].payload
    call = events.map_event(events.CODEX, "tool.call", body)[0].payload
    blob = json.dumps([prompt, call])
    assert "id_rsa" not in blob, "neither the prompt nor the arguments may reach the tape"
    assert prompt["content"] == "hash" and len(prompt["hash"]) == 32
    assert call["args_fingerprint"]


def test_a_codex_session_folds_into_a_tape_with_a_matched_turn(tmp_path: Path) -> None:
    """The mapping is only real if the folder can drive it: one turn, one tool, slots that pair."""
    inbox_dir = str(tmp_path / "inbox")
    writer = TapeWriter(str(tmp_path / "data"), segment_bytes=C.MIN_SEGMENT_BYTES)
    writer.batch_bytes = 0
    writer.batch_ms = 0.0
    writer.open()
    for event, body in (("session.start", {"session_id": "c1"}),
                        ("turn.start", {"turn": 1}),
                        ("user.prompt", {"prompt": "fix the build"}),
                        ("tool.call", {"tool_name": "shell", "tool_input": {"cmd": ["ls"]}}),
                        ("tool.result", {"tool_name": "shell"}),
                        ("turn.end", {"turn": 1}),
                        ("session.end", {"session_id": "c1"})):
        inbox.write_event(inbox_dir, tool="codex", event=event, session_id="c1", body=body)
    assert inbox.fold(inbox_dir, writer).files == 7
    writer.publish()
    frames = [f for f in scanner.scan(str(tmp_path / "data"), writer.writer_uuid.hex()).frames
              if f["kind"] != C.KIND_CHAIN]
    writer.close()
    kinds = [f["kind"] for f in frames]
    assert kinds[0] == C.KIND_RUN_START and kinds[-1] == C.KIND_RUN_END
    opens = [f for f in frames if f["kind"] == C.KIND_NODE_OPEN]
    closes = [f for f in frames if f["kind"] == C.KIND_NODE_CLOSE]
    assert len(opens) == 2 and len(closes) == 2
    assert sorted(f["node_slot"] for f in opens) == sorted(f["node_slot"] for f in closes), (
        "every close names a slot its open created")
    closed = [f["node_slot"] for f in closes]
    assert closed == sorted(closed, reverse=True), (
        "nesting closes innermost-first: the tool ends before the turn that contains it")


# ── the settings file, and the assumption it carries ─────────────────────────────────────────────────────────────
def test_the_adapter_says_out_loud_that_its_schema_is_unverified(tmp_path: Path) -> None:
    assert codex.SPEC.verified is False, "if this ever becomes True, it must be because someone checked"
    body = codex.status(str(tmp_path / "missing.json"))
    assert body["schema_verified"] is False
    assert "assumption" in body["note"]
    for record in (codex.install(str(tmp_path / "s.json")),):
        assert record.to_dict()["schema_verified"] is False


def test_install_then_remove_returns_the_file_byte_identical(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    original = path.read_bytes()
    installed = codex.install(str(path), now_ns=1_700_000_000_000_000_000)
    assert installed.installed and installed.backup and Path(installed.backup).exists()
    body = json.loads(path.read_text())
    assert body["model"] == "gpt-5-codex" and body["mcp_servers"] == SETTINGS["mcp_servers"]
    assert body["approval_policy"] == "on-request"
    assert set(codex.installed_events(body)) == set(events.CODEX_EVENTS)
    # The user's own notify hook survives beside ours.
    own = [entry for entry in body["hooks"]["notify"]
           if not any(h.get(codex.MARKER) for h in entry.get("hooks") or [])]
    assert own == SETTINGS["hooks"]["notify"]
    assert codex.remove(str(path)).changed
    assert path.read_bytes() == original


def test_an_edit_between_install_and_remove_wins_and_only_our_block_goes(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    codex.install(str(path), now_ns=1)
    body = json.loads(path.read_text())
    body["model"] = "gpt-5"
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    removed = codex.remove(str(path))
    after = json.loads(path.read_text())
    assert removed.changed and after["model"] == "gpt-5"
    assert not codex.installed_events(after)


@pytest.mark.parametrize("text", ["not json", "[]", '{"hooks": 7}', '{"hooks": {"tool.call": 1}}'])
def test_an_unknown_schema_is_refused_before_a_byte_or_a_backup(tmp_path: Path, text: str) -> None:
    path = _settings(tmp_path, text=text)
    before = path.read_bytes()
    with pytest.raises(BlackBoxError) as caught:
        codex.install(str(path))
    assert caught.value.code == "BBX-E151"
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.bak")) == []


def test_the_command_reports_the_unverified_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / ".codex" / "settings.json"
    assert hook_cli.main(["install", "--codex", "--settings", str(path)]) == 0
    captured = capsys.readouterr()
    assert "assumption" in captured.err, "the installer tells the operator, not just the docs"
    assert hook_cli.main(["status", "--codex", "--settings", str(path)]) == 0
    captured = capsys.readouterr()
    assert "NOT verified" in captured.out
    assert hook_cli.main(["status", "--codex", "--settings", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema_verified"] is False
    assert hook_cli.main(["remove", "--codex", "--settings", str(path)]) == 0
    assert not path.exists() or not codex.installed_events(json.loads(path.read_text()))
