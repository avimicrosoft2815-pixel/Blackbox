"""P3B-07's second half: the launcher folds the hook inbox into its own tape, and the capsule links the two.

This is the first test in the suite where all three vantage points are real at once: a child process fires hooks through
`blackbox ingest` (the hook vantage), the launcher folds them into a tape of their own, and the **recovery of that tape**
resolves an `exact` link to the launcher's tape because both carry the same run id. That link is the product claim the
reviewer asked to see — one timeline, from two processes, with the class of the claim written down.

The failure modes that matter are the boring ones, so they are asserted too: a run with no hooks leaves no hook tape
(no empty artefact), a corrupt entry is quarantined without changing the child's exit code, and the folder thread is
gone when `launch()` returns (a recorder that keeps a thread alive per run is a leak that shows up in week two).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.codec import cbor
from blackbox.hooks import inbox
from blackbox.rec import correlate, launcher, recovery
from blackbox.tape import scanner

AGENT = """
import json, os, subprocess, sys
env = dict(os.environ)
env["PYTHONPATH"] = {src!r}
def fire(event, body):
    subprocess.run([sys.executable, "-m", "blackbox", "ingest", "--tool", "claude-code", "--event", event],
                   input=json.dumps(body).encode(), env=env, check=True, capture_output=True)
fire("SessionStart", {{"session_id": "s1"}})
fire("UserPromptSubmit", {{"session_id": "s1", "prompt": "find the bug"}})
fire("PreToolUse", {{"session_id": "s1", "tool_name": "Bash", "tool_input": {{"command": "ls"}}}})
fire("PostToolUse", {{"session_id": "s1", "tool_name": "Bash"}})
fire("SessionEnd", {{"session_id": "s1"}})
print("agent done")
"""


def _hook_frames(data_dir: Path) -> tuple[str, list[dict]]:
    """The hook tape's writer uuid and its frames, found by vantage rather than by guessing at a name."""
    tapes = []
    for directory in sorted((data_dir / "tape").iterdir()):
        scan = scanner.scan(str(data_dir), directory.name)
        if scan.meta.get("vantage") == 3:
            # CHAIN and SEG_END are the writer's own bookkeeping, not events: they are on every tape and would make
            # "the last frame" a fact about sealing rather than about the session.
            tapes.append((directory.name, [f for f in scan.frames
                                           if f["kind"] not in (C.KIND_CHAIN, C.KIND_SEG_END)]))
    assert len(tapes) <= 1
    return tapes[0] if tapes else ("", [])


def test_a_childs_hooks_land_in_their_own_tape_and_the_capsule_links_it(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    result = launcher.launch([sys.executable, "-c", AGENT.format(src=str(ROOT / "src"))],
                             data_dir=str(data_dir), recover=True)
    assert result.exit_code == 0
    assert result.hooks["events"] == 5 and result.hooks["frames"] == 5, result.hooks
    assert result.hooks["quarantined"] == [] and result.hooks["errors"] == []

    tape, frames = _hook_frames(data_dir)
    assert tape, "the hook vantage wrote a tape of its own"
    kinds = [frame["kind"] for frame in frames]
    assert kinds[0] == C.KIND_RUN_START and kinds[-1] == C.KIND_RUN_END
    assert kinds.count(C.KIND_NODE_OPEN) == 1 and kinds.count(C.KIND_NODE_CLOSE) == 1
    assert kinds.count(C.KIND_MARK) == 1, "one prompt, one MARK — the rest map to nodes and the run"
    open_frame = next(frame for frame in frames if frame["kind"] == C.KIND_NODE_OPEN)
    close_frame = next(frame for frame in frames if frame["kind"] == C.KIND_NODE_CLOSE)
    assert open_frame["node_slot"] == close_frame["node_slot"] != 0
    prompt = cbor.decode_exact(next(f for f in frames if f["kind"] == C.KIND_MARK)["payload"])
    assert "find the bug" not in json.dumps(prompt), "the prompt's text is never on the tape"

    # The claim the reviewer asked about: two tapes, one run, and the class of the link written down.
    links = correlate.resolve_one(str(data_dir), tape)
    assert [link.klass for link in links] == ["exact"], links
    assert links[0].right_writer == result.writer_uuid
    assert links[0].evidence["carried_by"] == "x-blackbox-run"

    capsule = cap.read_capsule(result.recovered["capsule_path"])
    document = json.loads(capsule.entries["links.json"].decode())
    assert document["summary"]["exact"] >= 1, "the launcher's own capsule names the link too"

    assert inbox.pending(str(data_dir / "inbox")) == [], "the folder emptied the inbox it was given"


def test_a_child_that_fires_no_hooks_leaves_no_hook_tape(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    result = launcher.launch([sys.executable, "-c", "print('no hooks here')"], data_dir=str(data_dir), recover=False)
    assert result.exit_code == 0
    assert result.hooks["tape"] is None and result.hooks["events"] == 0
    tape, frames = _hook_frames(data_dir)
    assert tape == "" and frames == [], "an empty tape is noise; the absence of frames already says it"


def test_a_corrupt_entry_is_quarantined_and_the_child_still_exits_zero(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    script = ("import os, pathlib;"
              f"d = pathlib.Path({str(data_dir / 'inbox')!r}) / 's1'; d.mkdir(parents=True, exist_ok=True);"
              "(d / '0000000000000000001-bad.json').write_bytes(b'{not json');"
              "print('wrote garbage')")
    result = launcher.launch([sys.executable, "-c", script], data_dir=str(data_dir), recover=False)
    assert result.exit_code == 0
    assert result.hooks["quarantined"] == ["0000000000000000001-bad.json"]
    assert (data_dir / "inbox" / ".bad" / "0000000000000000001-bad.json").exists()


def test_the_folder_thread_is_finished_when_launch_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    original = launcher._InboxFolder.close

    def spy(self: launcher._InboxFolder) -> None:
        original(self)
        seen.append(self.alive)

    monkeypatch.setattr(launcher._InboxFolder, "close", spy)
    launcher.launch([sys.executable, "-c", "pass"], data_dir=str(tmp_path / "data"), recover=False)
    assert seen == [False], "the fold thread must not outlive the run that owns it"


def test_the_hook_tape_carries_the_runs_id_so_a_capsule_can_correlate_it(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    result = launcher.launch([sys.executable, "-c", AGENT.format(src=str(ROOT / "src"))],
                             data_dir=str(data_dir), recover=False)
    _tape, frames = _hook_frames(data_dir)
    start = cbor.decode_exact(frames[0]["payload"])
    assert start["run_id"] == result.run_id, "one run id, two tapes — that is what `exact` means"
    assert start["vantage"] == "hook"
    rec = recovery.recover(str(data_dir), result.writer_uuid)
    capsule = cap.read_capsule(rec.capsule_path)
    assert b"links.json" in capsule.entries or "links.json" in capsule.entries
