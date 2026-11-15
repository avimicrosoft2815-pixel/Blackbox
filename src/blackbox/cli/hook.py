"""`blackbox hook install|remove|status --claude-code|--codex [--project|--user|--settings <path>]` (P3B-05, P3B-06).

The command is thin on purpose: every decision about somebody's settings file is in `hooks/settings.py`, where a test
can call it with a path instead of a home directory, and the two adapters differ only in the data they hand it. What
lives here is argument parsing, the one-line report a human reads afterwards, and the exit codes — 0 for "done", 2 for
"refused, and here is why".

`--codex` prints its `schema_verified: false` in both the human and the JSON output. That is not decoration: the
adapter's location and block shape are an assumption this build could not check, and a tool that hides that in a
document somewhere is how an assumption becomes a claim.
"""
from __future__ import annotations

import argparse
import json
import sys
from types import ModuleType

from blackbox.errors import BlackBoxError
from blackbox.hooks import claude_code, codex


def _parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="blackbox hook",
                                 description="install or remove a coding agent's hook adapter (§11.9)")
    ap.add_argument("action", choices=("install", "remove", "status"))
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--claude-code", action="store_true", help="the Claude Code adapter (P3B-05)")
    which.add_argument("--codex", action="store_true", help="the Codex CLI adapter (P3B-06, schema unverified)")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--project", action="store_true", help="the project's settings file (default)")
    scope.add_argument("--user", action="store_true", help="the user's settings file")
    ap.add_argument("--settings", default=None, help="an explicit settings file, for tests and unusual layouts")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    return ap.parse_args(argv)


def _adapter(args: argparse.Namespace) -> ModuleType | None:
    if args.codex:
        return codex
    if args.claude_code:
        return claude_code
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    adapter = _adapter(args)
    if adapter is None:
        print("blackbox hook: choose an adapter (--claude-code or --codex)", file=sys.stderr)
        return 2
    scope = "user" if args.user else "project"
    tool: str = adapter.TOOL
    try:
        if args.action == "status":
            body = adapter.status(args.settings, scope=scope)
            if args.json:
                print(json.dumps(body, indent=2, sort_keys=True))
            else:
                state = "installed" if body["installed"] else "not installed"
                print(f"{tool}: {state} — {body['path']}")
                if body["installed"]:
                    print(f"  events: {', '.join(body['events'])}")
                    if body["missing"]:
                        print(f"  missing: {', '.join(body['missing'])}")
                if not body.get("schema_verified", True):
                    print(f"  schema: NOT verified in this build — {body.get('note', '')}")
            return 0
        result = (adapter.install if args.action == "install" else adapter.remove)(
            args.settings, scope=scope)
    except BlackBoxError as exc:
        print(f"blackbox hook: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"blackbox hook: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    elif args.action == "install":
        print(f"installed {len(result.events)} hook events for {tool} into {result.path}"
              + (f" (backup: {result.backup})" if result.backup else ""))
    else:
        print(f"removed the BlackBox hooks for {tool} from {result.path}"
              + ("" if result.changed else " (nothing to remove)"))
    if not result.verified:
        print(f"  note: {tool}'s settings schema is an assumption in this build (see docs/STATUS.md); "
              f"`--settings` overrides the path", file=sys.stderr)
    return 0
