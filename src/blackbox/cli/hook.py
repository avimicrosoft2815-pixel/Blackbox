"""`blackbox hook install|remove|status --claude-code [--project|--user] [--settings <path>]` (P3B-05).

The command is thin on purpose: every decision about somebody's settings file is in `hooks/claude_code.py`, where a
test can call it with a path instead of a home directory. What lives here is only argument parsing, the one-line report
a human reads after the command, and the exit codes — 0 for "done", 2 for "refused, and here is why".
"""
from __future__ import annotations

import argparse
import json
import sys

from blackbox.errors import BlackBoxError
from blackbox.hooks import claude_code


def _parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="blackbox hook",
                                 description="install or remove a coding agent's hook adapter (§11.9)")
    ap.add_argument("action", choices=("install", "remove", "status"))
    ap.add_argument("--claude-code", action="store_true", help="the Claude Code adapter (P3B-05)")
    ap.add_argument("--codex", action="store_true", help="the Codex CLI adapter (P3B-06, not built yet)")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--project", action="store_true", help="the project's .claude/settings.json (default)")
    scope.add_argument("--user", action="store_true", help="the user's ~/.claude/settings.json")
    ap.add_argument("--settings", default=None, help="an explicit settings file, for tests and unusual layouts")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.codex:
        print("blackbox hook: the Codex adapter is P3B-06 and is not built yet; nothing was changed", file=sys.stderr)
        return 2
    if not args.claude_code:
        print("blackbox hook: choose an adapter (--claude-code)", file=sys.stderr)
        return 2
    scope = "user" if args.user else "project"
    try:
        if args.action == "status":
            body = claude_code.status(args.settings, scope=scope)
            if args.json:
                print(json.dumps(body, indent=2, sort_keys=True))
            else:
                state = "installed" if body["installed"] else "not installed"
                print(f"{claude_code.TOOL}: {state} — {body['path']}")
                if body["installed"]:
                    print(f"  events: {', '.join(body['events'])}")
                if body["missing"] and body["installed"]:
                    print(f"  missing: {', '.join(body['missing'])}")
            return 0
        result = (claude_code.install if args.action == "install" else claude_code.remove)(
            args.settings, scope=scope)
    except BlackBoxError as exc:
        print(f"blackbox hook: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    elif args.action == "install":
        print(f"installed {len(result.events)} hook events into {result.path}"
              + (f" (backup: {result.backup})" if result.backup else ""))
    else:
        print(f"removed the BlackBox hooks from {result.path}"
              + ("" if result.changed else " (nothing to remove)"))
    return 0
