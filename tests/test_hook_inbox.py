"""The hook inbox (P3B-07) and §11.9's event table (P3B-05's mapping half).

The inbox exists because a hook blocks the agent that fired it, so the claims here are about *durability and speed*,
not about style: one file per event, atomic, folded exactly as often as it should be, and a corrupt entry quarantined
rather than dropped. The mapping claims are about honesty: a prompt leaves a hash and a length, never its text; a tool
call leaves a fingerprint of its arguments, never the arguments; a `NODE_CLOSE` for a node nobody saw open is written
as a `MARK` that says so.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.hooks import events, inbox
from blackbox.tape import scanner
from blackbox.tape.writer import TapeWriter


def _writer(tmp_path: Path) -> TapeWriter:
    writer = TapeWriter(str(tmp_path / "data"), segment_bytes=C.MIN_SEGMENT_BYTES)
    writer.batch_bytes = 0
    writer.batch_ms = 0.0
    writer.open()
    return writer


def _frames(tmp_path: Path, writer: TapeWriter) -> list[dict]:
    writer.publish()
    return [frame for frame in scanner.scan(str(tmp_path / "data"), writer.writer_uuid.hex()).frames
            if frame["kind"] != C.KIND_CHAIN]


def _payloads(frames: list[dict], kind: int) -> list[dict]:
    return [cbor.decode_exact(frame["payload"]) for frame in frames if frame["kind"] == kind]


# ── §11.9's table ────────────────────────────────────────────────────────────────────────────────────────────────
def test_every_claude_code_event_in_the_plan_maps_to_a_frame() -> None:
    """The plan's prose says nine events and its table names ten; the adapter maps all ten and the fixture is each."""
    assert len(events.EVENTS) == 10
    body = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}, "prompt": "hi"}
    mapped = {name: events.map_event(events.CLAUDE_CODE, name, body) for name in events.EVENTS}
    assert all(records for records in mapped.values())
    kinds = {name: [record.kind for record in records] for name, records in mapped.items()}
    assert kinds["SessionStart"] == [C.KIND_RUN_START]
    assert kinds["SessionEnd"] == [C.KIND_RUN_END]
    assert kinds["PreToolUse"] == [C.KIND_NODE_OPEN]
    assert kinds["PostToolUse"] == [C.KIND_NODE_CLOSE]
    assert kinds["PostToolUseFailure"] == [C.KIND_NODE_CLOSE]
    assert kinds["UserPromptSubmit"] == [C.KIND_MARK]
    assert kinds["Notification"] == [C.KIND_MARK]
    assert kinds["PreCompact"] == [C.KIND_MARK]
    assert mapped["PreToolUse"][0].opens == "Bash"
    assert mapped["PostToolUse"][0].closes == "Bash"


def test_a_prompt_leaves_a_hash_and_a_length_and_never_its_text() -> None:
    prompt_text = "my api key is sk-live-abc123 and I live at 12 Elm Street"
    payload = events.map_event(events.CLAUDE_CODE, "UserPromptSubmit", {"prompt": prompt_text})[0].payload
    blob = json.dumps(payload)
    assert "sk-live" not in blob and "Elm Street" not in blob
    assert payload["content"] == "hash" and payload["bytes"] == len(prompt_text.encode())
    assert len(payload["hash"]) == 32


def test_a_tool_call_leaves_a_fingerprint_of_its_arguments_and_not_the_arguments() -> None:
    args = {"command": "cat ~/.ssh/id_rsa", "timeout": 5}
    payload = events.map_event(events.CLAUDE_CODE, "PreToolUse",
                               {"tool_name": "Bash", "tool_input": args})[0].payload
    assert payload["args_fingerprint"] and payload["args_fingerprint"] != ""
    assert "id_rsa" not in json.dumps(payload)
    # Key order and whitespace must not change the fingerprint, or two identical calls look different.
    reordered = events.map_event(events.CLAUDE_CODE, "PreToolUse",
                                 {"tool_name": "Bash", "tool_input": {"timeout": 5, "command": "cat ~/.ssh/id_rsa"}})
    assert reordered[0].payload["args_fingerprint"] == payload["args_fingerprint"]


@pytest.mark.parametrize("tool,event", [("codex", "PreToolUse"), ("claude-code", "SomeNewEvent"), ("nope", "x")])
def test_an_unmapped_event_is_a_mark_that_names_it(tool: str, event: str) -> None:
    records = events.map_event(tool, event, {})
    assert len(records) == 1 and records[0].kind == C.KIND_MARK
    assert records[0].payload["kind"] == "tool_event"
    assert records[0].payload["raw_name"] == event


# ── the inbox ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_one_event_is_one_file_and_no_temporary_survives(tmp_path: Path) -> None:
    path = inbox.write_event(str(tmp_path / "inbox"), tool="claude-code", event="PreToolUse",
                             session_id="s1", body={"tool_name": "Bash"}, now_ns=1_700_000_000_000_000_000)
    assert Path(path).exists()
    assert Path(path).parent.name == "s1"
    assert Path(path).name.startswith("1700000000000000000-")
    assert Path(path).suffix == ".json"
    assert [p.name for p in Path(path).parent.iterdir() if p.name.endswith(".tmp")] == []
    envelope = inbox.read_envelope(path)
    assert envelope["version"] == inbox.ENVELOPE_VERSION
    assert envelope["ingest_id"] == Path(path).stem
    assert envelope["body"] == {"tool_name": "Bash"}


def test_fifty_concurrent_ingests_produce_fifty_files_and_no_corruption(tmp_path: Path) -> None:
    """P3B-07's acceptance, through real processes: a parallel tool batch fires hooks at the same instant."""
    inbox_dir = tmp_path / "inbox"
    script = (f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r});"
              "from blackbox.hooks import inbox;"
              f"inbox.write_event({str(inbox_dir)!r}, tool='claude-code', event='PreToolUse', "
              "session_id='s1', body={'n': 1})")
    procs = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(50)]
    assert all(proc.wait(timeout=120) == 0 for proc in procs)
    files = inbox.pending(str(inbox_dir))
    assert len(files) == 50
    assert len({os.path.basename(f) for f in files}) == 50, "two ingests must never pick the same name"
    for path in files:
        assert inbox.read_envelope(path)["event"] == "PreToolUse"
    assert not list(inbox_dir.rglob("*.tmp"))


def test_a_thread_race_on_the_same_millisecond_still_writes_whole_files(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "inbox"
    errors: list[BaseException] = []

    def fire() -> None:
        try:
            inbox.write_event(str(inbox_dir), tool="claude-code", event="PostToolUse", session_id="s1",
                              body={"tool_name": "Read"}, now_ns=1)
        except BaseException as exc:          # pragma: no cover - a failure here is the assertion
            errors.append(exc)

    threads = [threading.Thread(target=fire) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    files = inbox.pending(str(inbox_dir))
    assert len(files) == 20, "the nonce, not the clock, is what keeps same-millisecond events apart"
    for path in files:
        assert inbox.read_envelope(path)["body"] == {"tool_name": "Read"}


def test_folding_writes_the_frames_and_empties_the_inbox(tmp_path: Path) -> None:
    inbox_dir = str(tmp_path / "inbox")
    writer = _writer(tmp_path)
    for event, body in (("SessionStart", {"session_id": "s1"}),
                        ("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}}),
                        ("PostToolUse", {"tool_name": "Bash"}),
                        ("SessionEnd", {"session_id": "s1"})):
        inbox.write_event(inbox_dir, tool="claude-code", event=event, session_id="s1", body=body)
    result = inbox.fold(inbox_dir, writer)
    assert result.files == 4 and result.ok and result.quarantined == []
    frames = _frames(tmp_path, writer)
    kinds = [frame["kind"] for frame in frames]
    assert kinds == [C.KIND_RUN_START, C.KIND_NODE_OPEN, C.KIND_NODE_CLOSE, C.KIND_RUN_END]
    opened, closed = frames[1], frames[2]
    assert opened["node_slot"] == closed["node_slot"] != 0, "the close must name the node the open created"
    assert _payloads(frames, C.KIND_NODE_CLOSE)[0]["outcome"] == "ok"
    assert inbox.pending(inbox_dir) == [], "a folded entry is gone, so a second fold cannot duplicate it"
    assert inbox.fold(inbox_dir, writer).files == 0


def test_the_slot_survives_the_process_that_opened_it(tmp_path: Path) -> None:
    """`PreToolUse` and `PostToolUse` are two hook invocations, minutes apart, in two processes."""
    inbox_dir = str(tmp_path / "inbox")
    writer = _writer(tmp_path)
    inbox.write_event(inbox_dir, tool="claude-code", event="PreToolUse", session_id="s1",
                      body={"tool_name": "Bash"})
    inbox.fold(inbox_dir, writer)
    inbox.write_event(inbox_dir, tool="claude-code", event="PostToolUse", session_id="s1",
                      body={"tool_name": "Bash"})
    second = inbox.fold(inbox_dir, writer)
    assert second.files == 1
    frames = _frames(tmp_path, writer)
    assert frames[-1]["kind"] == C.KIND_NODE_CLOSE and frames[-1]["node_slot"] == frames[-2]["node_slot"]


def test_a_close_without_an_open_is_a_mark_that_says_so(tmp_path: Path) -> None:
    inbox_dir = str(tmp_path / "inbox")
    writer = _writer(tmp_path)
    inbox.write_event(inbox_dir, tool="claude-code", event="Stop", session_id="s1", body={})
    inbox.fold(inbox_dir, writer)
    frames = _frames(tmp_path, writer)
    assert [frame["kind"] for frame in frames] == [C.KIND_MARK], "no NODE_CLOSE for a node nobody saw open"
    payload = _payloads(frames, C.KIND_MARK)[0]
    assert payload["why"] == "close without an open node"
    assert payload["raw_name"] == "Stop"


def test_a_corrupt_entry_is_quarantined_and_the_rest_still_fold(tmp_path: Path) -> None:
    inbox_dir = str(tmp_path / "inbox")
    writer = _writer(tmp_path)
    inbox.write_event(inbox_dir, tool="claude-code", event="SessionStart", session_id="s1", body={})
    bad = Path(inbox_dir) / "s1" / "0000000000000000001-bad.json"
    bad.write_bytes(b"{not json")
    result = inbox.fold(inbox_dir, writer)
    assert result.quarantined == ["0000000000000000001-bad.json"]
    assert not result.ok
    assert (Path(inbox_dir) / ".bad" / bad.name).exists(), "the entry is kept, not deleted"
    assert _payloads(_frames(tmp_path, writer), C.KIND_RUN_START), "one bad entry does not stop the others"


def test_every_frame_carries_the_ingest_id_so_a_refold_is_detectable(tmp_path: Path) -> None:
    """Folding is at-least-once by design: the file is unlinked *after* its frames are published."""
    inbox_dir = str(tmp_path / "inbox")
    writer = _writer(tmp_path)
    path = inbox.write_event(inbox_dir, tool="claude-code", event="PreToolUse", session_id="s1",
                             body={"tool_name": "Bash"})
    ingest_id = Path(path).stem
    inbox.fold(inbox_dir, writer)
    payload = _payloads(_frames(tmp_path, writer), C.KIND_NODE_OPEN)[0]
    assert payload["ingest_id"] == ingest_id


def test_the_inbox_defaults_follow_the_run_and_never_the_working_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLACKBOX_INBOX", raising=False)
    monkeypatch.setenv("BLACKBOX_DATA_DIR", "/tmp/bbx-data")
    assert inbox.default_dir(None) == "/tmp/bbx-data/inbox"
    monkeypatch.setenv("BLACKBOX_INBOX", "/tmp/bbx-inbox")
    assert inbox.default_dir(None) == "/tmp/bbx-inbox"
    assert inbox.default_dir("/explicit") == "/explicit"
    monkeypatch.delenv("BLACKBOX_INBOX")
    monkeypatch.delenv("BLACKBOX_DATA_DIR")
    assert inbox.default_dir(None).endswith(os.path.join(".blackbox", "inbox"))


def test_the_ingest_command_costs_the_agent_tens_of_milliseconds(tmp_path: Path) -> None:
    """P3B-07's cold-start row, measured the way a hook pays it: a whole interpreter, stdin to file."""
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}}).encode()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    started = time.perf_counter()
    finished = subprocess.run([sys.executable, "-m", "blackbox", "ingest", "--tool", "claude-code",
                               "--event", "PreToolUse", "--inbox", str(tmp_path / "inbox")],
                              input=payload, capture_output=True, env=env, timeout=60)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert finished.returncode == 0, finished.stderr
    assert len(inbox.pending(str(tmp_path / "inbox"))) == 1
    # The plan's gate is 40 ms p95 on the bench box; this is a developer sandbox running the whole suite, so the bound
    # here is deliberately loose and the row in docs/STATUS.md says which machine the real number belongs to.
    assert elapsed_ms < 900, f"cold start took {elapsed_ms:.0f} ms"


@pytest.mark.parametrize("stdin", [b"not json at all", b"[1,2,3]", b""])
def test_a_broken_hook_body_never_fails_the_agent(tmp_path: Path, stdin: bytes) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    finished = subprocess.run([sys.executable, "-m", "blackbox", "ingest", "--tool", "claude-code",
                               "--event", "PreToolUse", "--inbox", str(tmp_path / "inbox")],
                              input=stdin, capture_output=True, env=env, timeout=60)
    assert finished.returncode == 0, "a hook that breaks its agent is worse than a hook that records nothing"
    if stdin == b"not json at all":
        assert b"not JSON" in finished.stderr
        assert inbox.pending(str(tmp_path / "inbox")) == []
