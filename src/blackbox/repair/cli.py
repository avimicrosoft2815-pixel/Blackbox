"""`blackbox repair preview|apply|verify` — the approval gate in front of a proposal someone else wrote.

Exit codes are the contract a script or an agent will read:

* `0` — it did what was asked (a preview was printed, a proposal was applied, the verification commands all passed).
* `2` — usage or refusal: a bad path, a proposal that fails validation, an apply without the right token. **Nothing was
  written.**
* `3` — the verification ran and something failed, or nothing was run at all (an unverified change is not a success).
* `1` — an apply failed part-way and the rollback ran; the message says what was restored. The tree is reported, not
  assumed: the caller should read it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from blackbox.repair.apply import apply as apply_proposal
from blackbox.repair.apply import verify as verify_change
from blackbox.repair.proposal import ProposalError, assessed_risk, load


def _preview(args: argparse.Namespace) -> int:
    proposal = load(args.proposal, repo=args.repo)
    scope = proposal.scope()
    print(f"# Repair proposal: {proposal.title}")
    print()
    print(f"{scope['files']} file(s), +{scope['added_lines']} / -{scope['removed_lines']} line(s), "
          f"directories: {', '.join(scope['directories'])}")
    print()
    if proposal.rationale:
        print(proposal.rationale)
        print()
    print("## Exact paths")
    for change in proposal.changes:
        verb = "create" if change.is_new() else "replace"
        print(f"- {verb} `{change.path}` (current sha256 {str(change.current_sha256)[:12] or 'absent'})")
    print()
    print("## Diff")
    for change in proposal.changes:
        print()
        print(f"```diff\n{change.diff()}\n```")
    print()
    print("## Risk")
    for note in list(proposal.risk) + assessed_risk(proposal):
        print(f"- {note}")
    print()
    print("## Tests the proposal suggests — **not run, and not runnable by this command**")
    for test in proposal.suggested_tests or ["(none suggested)"]:
        print(f"- `{test}`")
    print()
    print("## Scope")
    print(f"- creates: {', '.join(scope['creates']) or '—'}")
    print(f"- replaces: {', '.join(scope['replaces']) or '—'}")
    print()
    print("## Approval")
    print("Nothing has been written. To apply exactly this, run:")
    print()
    print(f"    blackbox repair apply {args.proposal} --repo {args.repo} --approve {proposal.token()}")
    print()
    print("The token is bound to this proposal's bytes, this repository, and the hash of every file it touches. If any of")
    print("them change, the apply is refused and you have to look again.")
    return 0


def _apply(args: argparse.Namespace) -> int:
    # `load` is inside the guard: a proposal whose paths have moved is refused at read time as well as at write time,
    # and a refusal must be exit 2 with a message, never a traceback a caller has to parse.
    try:
        proposal = load(args.proposal, repo=args.repo)
    except ProposalError as exc:
        print(f"blackbox repair: {exc}", file=sys.stderr)
        return 2
    if not args.approve:
        print("refusing to apply without --approve <token>; run `blackbox repair preview` first", file=sys.stderr)
        print(f"the token for the current state is {proposal.token()} — but read the preview before using it",
              file=sys.stderr)
        return 2
    try:
        result = apply_proposal(proposal, approval=args.approve)
    except ProposalError as exc:
        print(f"blackbox repair: {exc}", file=sys.stderr)
        return 2
    for path in result.created:
        print(f"created {path}")
    for path in result.written:
        print(f"wrote   {path}")
    print(f"originals saved in {result.backup_dir}")
    print("nothing was run: this change is unverified until you run `blackbox repair verify --test …`")
    return 0


def _verify(args: argparse.Namespace) -> int:
    commands = args.test or []
    if commands:
        # Said before anything runs, because this is the one command in this product that executes user-supplied text.
        print(f"Executing {len(commands)} command(s) through the platform shell in {os.path.abspath(args.repo)}:")
        for command in commands:
            print(f"  $ {command}")
        print("These run with your privileges: they can change anything you can. Nothing from the proposal is executed —")
        print("only the commands above, which you supplied on this command line.")
        print()
    report = verify_change(commands, cwd=args.repo)
    for row in report["ran"]:
        state = "PASS" if row["exit_code"] == 0 else "FAIL"
        print(f"{state}  exit {row['exit_code']:>3}  {row['seconds']:6.2f}s  {row['command']}")
        for line in row["tail"]:
            print(f"      {line}")
    print()
    print(report["note"])
    if not commands:
        return 3
    return 0 if report["verified"] else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="blackbox repair",
        description="Review and apply a repair proposal written by a human or an agent.",
        epilog="`preview` and `apply` execute nothing. `verify --test CMD` DOES execute CMD: it runs through the "
               "platform shell in --repo, as you, and prints each command before running it. The proposal's own `tests` "
               "list is only ever displayed.")
    ap.add_argument("action", choices=("preview", "apply", "verify"), help="what to do with the proposal")
    ap.add_argument("proposal", help="path to a proposal JSON file")
    ap.add_argument("--repo", default=".", help="repository root the proposal's paths are relative to (default: .)")
    ap.add_argument("--approve", metavar="TOKEN", help="the token `preview` printed; required by `apply`")
    ap.add_argument("--test", action="append", metavar="CMD",
                    help="a command to RUN for `verify` (repeatable). It is executed through the platform shell in "
                         "`--repo`, with your privileges; nothing from the proposal is ever executed")
    ap.add_argument("--json", action="store_true", help="print the preview's machine-readable summary")
    args = ap.parse_args(argv)
    if args.action == "preview":
        try:
            proposal = load(args.proposal, repo=args.repo)
        except ProposalError as exc:
            print(f"blackbox repair: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"title": proposal.title, "token": proposal.token(), "scope": proposal.scope(),
                              "risk": list(proposal.risk) + assessed_risk(proposal),
                              "suggested_tests": proposal.suggested_tests,
                              "changes": [{"path": c.path, "create": c.is_new(),
                                           "current_sha256": c.current_sha256} for c in proposal.changes]},
                             indent=2))
            return 0
        return _preview(args)
    if args.action == "apply":
        return _apply(args)
    return _verify(args)
