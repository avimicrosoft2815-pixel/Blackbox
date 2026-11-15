"""`blackbox ingest --tool <tool> --event <name>` — the command a coding agent's hook actually runs (P3B-07).

Reads the tool's JSON from **stdin**, writes one atomic inbox file, exits 0. That is all it does, and the smallness is
the requirement: the agent is blocked on this process, so its cold start is part of the user's latency. The mapping to
tape frames happens later, at fold time (`hooks/events.py`, D-039) — which also means a mapping fixed tomorrow still
applies to the events this command wrote today.

**A broken hook must not break the agent.** A body that is not JSON, a missing `--session`, a directory that cannot be
created: every one of them exits 0 after writing a line to stderr. The alternative — a non-zero exit in the middle of
somebody's prompt because our recorder had a problem — is the trade this project refused everywhere else (C5, fail
open), and a hook is the worst possible place to make an exception.
"""
from __future__ import annotations

import argparse
import json
import sys

from blackbox.hooks import inbox


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="blackbox ingest",
                                 description="one hook event from stdin to the inbox (P3B-07)")
    ap.add_argument("--tool", required=True, help="the coding agent the hook belongs to (claude-code, codex)")
    ap.add_argument("--event", required=True, help="the tool's own event name, taken verbatim")
    ap.add_argument("--session", default=None, help="the tool's session id (default: read `session_id` from the body)")
    ap.add_argument("--inbox", default=None, help="the inbox directory (default: $BLACKBOX_INBOX or "
                                                 "<$BLACKBOX_DATA_DIR>/inbox, else ~/.blackbox/inbox)")
    ap.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = ap.parse_args(argv)

    raw = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    try:
        body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"blackbox ingest: stdin was not JSON ({exc}); nothing recorded", file=sys.stderr)
        return 0                                   # never fail the agent
    if not isinstance(body, dict):
        body = {"value": body}
    session = args.session or body.get("session_id") or body.get("session") or "unknown"
    try:
        path = inbox.write_event(inbox.default_dir(args.inbox), tool=args.tool, event=args.event,
                                 session_id=str(session), body=body,
                                 now_ns=body.get("at_ns") if isinstance(body.get("at_ns"), int) else None)
    except OSError as exc:
        print(f"blackbox ingest: could not write the inbox entry ({exc}); nothing recorded", file=sys.stderr)
        return 0                                   # never fail the agent
    if not args.quiet:
        print(path)
    return 0
