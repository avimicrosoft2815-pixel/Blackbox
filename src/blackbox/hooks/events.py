"""§11.9's hook-event table, as one pure function.

A coding agent's hook fires, hands us a JSON body and waits: the agent will not proceed until we exit. So the fast path
(`blackbox ingest`, `hooks/inbox.py`) does the least that is durable — one atomic file — and the **mapping lives here**,
where it runs at fold time. The plan's §11.9 sentence says the ingest command "maps it"; this module is why the build
does not, and the reason is cold start: mapping needs the tape's own vocabulary (kind ids, canonical CBOR, the
fingerprint codec), and `ingest` is a 40 ms budget that a hook's user is paying in the middle of their prompt
(D-039). The envelope keeps the raw body, so a mapping fixed later still applies to events already on disk.

Two rules the table encodes, and both are about not inventing data:

* **An unmapped event is a `MARK`, not a drop.** §11.9 says so for Codex and the same rule is right here: a tool that
  adds a tenth event should show up as `MARK{kind: "tool_event", raw_name: …}` in the tape, because "the adapter did
  not know this" is a fact the reader can act on and silence is not.
* **A close without an open is not a close.** `Stop` closes the turn and `PostToolUse` closes a tool node; if the inbox
  never saw the matching open (a session that began before the adapter was installed, an entry the fold quarantined),
  the folder writes a `MARK` saying exactly that instead of a `NODE_CLOSE` for a node no reader ever saw open. A
  fabricated close is how a graph acquires a parent it never had.

The hash fields are hashes on purpose: `content=hash` is the default (§Appendix A), and a prompt's text is the single
most sensitive thing a coding agent handles.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from blackbox import constants as C

# §11.9's table, in the names Claude Code fires, in the order it documents them. The plan's prose counts nine and its
# table names ten; all ten are mapped, and `tests/test_hook_inbox.py` asserts the count so the discrepancy stays visible.
CLAUDE_CODE = "claude-code"
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                 "Notification", "PreCompact", "Stop", "SubagentStop", "SessionEnd")
EVENTS = CLAUDE_EVENTS

# Codex's twelve names. These are the **adapter's contract with the folder**, not a claim about the tool: Codex's own
# vocabulary could not be verified from this build's sandbox (P3B-06 is labelled as such), and the design survives being
# wrong — a name we do not know becomes `MARK{tool_event, raw_name}`, so a wrong guess is visible rather than silent.
CODEX = "codex"
CODEX_EVENTS = ("session.start", "session.end", "turn.start", "turn.end", "user.prompt", "tool.call",
                "tool.result", "tool.error", "agent.message", "context.compact", "notify", "error")
TOOLS = (CLAUDE_CODE, CODEX)

# The node a record opens or closes is named here; the *slot* is the folder's business, because only the tape knows
# which slots are already taken (hooks/inbox.py).
TURN = "turn"
SUBAGENT = "subagent"


@dataclass(frozen=True)
class Record:
    """One frame the table says to write, before the folder gives it a slot and an id."""

    kind: int
    payload: dict[str, Any] = field(default_factory=dict)
    opens: str | None = None            # the node name this record opens
    closes: str | None = None           # the node name this record closes


def _digest(data: Any) -> str:
    """A stable fingerprint of any JSON value: key order and whitespace must not change it."""
    if isinstance(data, (dict, list)):
        text = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    else:
        text = "" if data is None else str(data)
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=16).hexdigest()


def _text(body: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = body.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _prompt_hash(body: Mapping[str, Any]) -> dict[str, Any]:
    prompt = body.get("prompt")
    text = prompt if isinstance(prompt, str) else json.dumps(prompt, sort_keys=True, default=str)
    raw = (text or "").encode("utf-8", "replace")
    return {"hash": hashlib.blake2b(raw, digest_size=16).hexdigest(), "bytes": len(raw),
            "content": "hash"}


def _tool_call(body: Mapping[str, Any]) -> dict[str, Any]:
    """`PreToolUse`'s node payload: the tool's name and a fingerprint of its arguments, never the arguments."""
    name = _text(body, "tool_name", "tool") or "tool"
    args = body.get("tool_input", body.get("args"))
    return {"name": name, "kind": "tool", "tool": name, "args_fingerprint": _digest(args)}


def _tool_result(body: Mapping[str, Any], outcome: str) -> dict[str, Any]:
    name = _text(body, "tool_name", "tool") or "tool"
    payload: dict[str, Any] = {"name": name, "kind": "tool", "outcome": outcome}
    error = body.get("error")
    if error is not None or outcome == "error":
        # The *type* of failure, not its text: a tool's error string routinely contains a path, a token or a row of
        # customer data, and the tape is the thing that outlives the session.
        payload["error_type"] = _digest(error)[:16] if error is not None else "reported"
    return payload


def _claude_code(event: str, body: Mapping[str, Any]) -> list[Record] | None:
    """The nine-and-one rows of §11.9's table (the plan's prose says nine; its table names ten, and all ten map)."""
    if event == "SessionStart":
        return [Record(C.KIND_RUN_START, {"source": _text(body, "source") or "startup"})]
    if event == "SessionEnd":
        return [Record(C.KIND_RUN_END, {"reason": _text(body, "reason") or "session_end"})]
    if event == "UserPromptSubmit":
        return [Record(C.KIND_MARK, {"name": "prompt", "kind": "prompt", **_prompt_hash(body)})]
    if event == "PreToolUse":
        return [Record(C.KIND_NODE_OPEN, _tool_call(body), opens=_text(body, "tool_name", "tool") or "tool")]
    if event in ("PostToolUse", "PostToolUseFailure"):
        outcome = "ok" if event == "PostToolUse" else "error"
        return [Record(C.KIND_NODE_CLOSE, _tool_result(body, outcome),
                       closes=_text(body, "tool_name", "tool") or "tool")]
    if event == "Notification":
        return [Record(C.KIND_MARK, {"name": "notification", "kind": "notice",
                                     "message": _digest(body.get("message"))[:16]})]
    if event == "PreCompact":
        return [Record(C.KIND_MARK, {"name": "precompact", "kind": "compact",
                                     "trigger": _text(body, "trigger") or "auto"})]
    if event == "Stop":
        return [Record(C.KIND_NODE_CLOSE, {"name": TURN, "kind": "turn", "outcome": "ok"}, closes=TURN)]
    if event == "SubagentStop":
        return [Record(C.KIND_NODE_CLOSE, {"name": SUBAGENT, "kind": "subagent", "outcome": "ok"}, closes=SUBAGENT)]
    return None


def _codex(event: str, body: Mapping[str, Any]) -> list[Record] | None:
    """The Codex table. Tool names come from the body, as Claude Code's do; every close names the same node as its open."""
    if event == "session.start":
        return [Record(C.KIND_RUN_START, {"source": _text(body, "source") or "startup"})]
    if event == "session.end":
        return [Record(C.KIND_RUN_END, {"reason": _text(body, "reason") or "session_end"})]
    if event == "turn.start":
        return [Record(C.KIND_NODE_OPEN, {"name": TURN, "kind": "turn", "turn": body.get("turn") or 0}, opens=TURN)]
    if event == "turn.end":
        return [Record(C.KIND_NODE_CLOSE, {"name": TURN, "kind": "turn", "outcome": "ok"}, closes=TURN)]
    if event == "user.prompt":
        return [Record(C.KIND_MARK, {"name": "prompt", "kind": "prompt", **_prompt_hash(body)})]
    if event == "tool.call":
        return [Record(C.KIND_NODE_OPEN, _tool_call(body), opens=_text(body, "tool_name", "tool") or "tool")]
    if event in ("tool.result", "tool.error"):
        outcome = "ok" if event == "tool.result" else "error"
        return [Record(C.KIND_NODE_CLOSE, _tool_result(body, outcome),
                       closes=_text(body, "tool_name", "tool") or "tool")]
    if event == "agent.message":
        return [Record(C.KIND_MARK, {"name": "assistant", "kind": "assistant", **_prompt_hash(body)})]
    if event == "context.compact":
        return [Record(C.KIND_MARK, {"name": "precompact", "kind": "compact",
                                     "trigger": _text(body, "trigger") or "auto"})]
    if event == "notify":
        return [Record(C.KIND_MARK, {"name": "notify", "kind": "notice",
                                     "message": _digest(body.get("message", body))[:16]})]
    if event == "error":
        return [Record(C.KIND_MARK, {"name": "error", "kind": "error",
                                     "error_type": _digest(body.get("error", body))[:16]})]
    return None


def map_event(tool: str, event: str, body: Mapping[str, Any]) -> list[Record]:
    """Every frame this event implies. An unknown tool or event is one honest `MARK{tool_event}`."""
    table = {CLAUDE_CODE: _claude_code, CODEX: _codex}.get(tool)
    records = table(event, body) if table is not None else None
    if records is None:
        return [Record(C.KIND_MARK, {"name": event or "unknown", "kind": "tool_event",
                                     "raw_name": event, "tool": tool})]
    return records
