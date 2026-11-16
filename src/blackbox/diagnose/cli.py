"""`blackbox diagnose <capsule|dir>` — read one capsule and say what it supports, and what it does not.

Exit codes are part of the interface, because this will be called from scripts and by agents:

* `0` — a report was produced (findings or not: "the evidence is too thin" is an answer, not an error).
* `1` — nothing readable at that path (`EvidenceError`). The message names the path and what was tried.
* `2` — bad usage: an unreadable argument, a `--brief` path that already exists without `--force`.

`--brief` and `--out` are the **only** writes this command performs, both to a path the caller names, and both refuse to
overwrite an existing file without `--force`: a diagnosis must not be able to destroy the notes a human wrote next to it.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

from blackbox.diagnose.evidence import DEFAULT_MAX_EVENTS, EvidenceError, load
from blackbox.diagnose.report import build


def _newest_capsule(path: str) -> str:
    """A directory means "the newest capsule under it", and the choice is printed so it is never implicit."""
    found = glob.glob(os.path.join(path, "**", "*.bbx"), recursive=True)
    if not found:
        raise EvidenceError(f"{path} holds no .bbx capsule (looked recursively)")
    return max(found, key=os.path.getmtime)


def _write(path: str, text: str, *, force: bool) -> int | None:
    if os.path.exists(path) and not force:
        print(f"refusing to overwrite {path}: pass --force to replace it", file=sys.stderr)
        return 2
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="blackbox diagnose",
                                 description="Inspect a capsule and report what the evidence supports (read-only).")
    ap.add_argument("path", help="a .bbx capsule, or a data directory (the newest capsule under it is used)")
    ap.add_argument("--json", action="store_true", help="print the machine-readable report instead of markdown")
    ap.add_argument("--brief", metavar="PATH", help="also write the repair brief to PATH")
    ap.add_argument("--out", metavar="PATH", help="write the markdown report to PATH instead of stdout")
    ap.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS,
                    help=f"parse at most this many frames (default {DEFAULT_MAX_EVENTS})")
    ap.add_argument("--force", action="store_true", help="allow --brief/--out to replace an existing file")
    args = ap.parse_args(argv)
    if args.max_events < 1:
        print("--max-events must be at least 1", file=sys.stderr)
        return 2
    try:
        target = _newest_capsule(args.path) if os.path.isdir(args.path) else args.path
        evidence = load(target, max_events=args.max_events)
    except EvidenceError as exc:
        print(f"blackbox diagnose: {exc}", file=sys.stderr)
        return 1
    if target != args.path:
        print(f"reading the newest capsule under {args.path}: {target}", file=sys.stderr)
    report = build(evidence)
    if args.brief:
        refusal = _write(args.brief, report.brief(), force=args.force)
        if refusal is not None:
            return refusal
        print(f"wrote the repair brief to {args.brief} ({len(report.brief())} chars; review it before using it)",
              file=sys.stderr)
    if args.json:
        import json

        text = json.dumps(report.to_json(), indent=2, sort_keys=False) + "\n"
    else:
        text = report.markdown()
    if args.out:
        refusal = _write(args.out, text, force=args.force)
        if refusal is not None:
            return refusal
        print(f"wrote the report to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0
