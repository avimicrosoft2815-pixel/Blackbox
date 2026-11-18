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


def _summary_of(capsule_path: str) -> tuple[int, int, str]:
    """(model_calls, events, run_id) from a capsule's manifest, or a conservative zero-filled tuple if it will not open.

    The manifest is what `ls` reads too; a capsule that cannot be opened is still a candidate — it just cannot win a
    comparison on content.
    """
    try:
        from blackbox.cap.format import read_capsule

        manifest = read_capsule(capsule_path, max_bytes=32 << 20).manifest
    except Exception:
        return (0, 0, "")
    capture = manifest.get("capture") or {}
    counts = manifest.get("counts") or {}
    run = manifest.get("run") or {}
    try:
        return (int(capture.get("model_calls") or 0), int(counts.get("events") or 0), str(run.get("run_id") or ""))
    except (TypeError, ValueError):
        return (0, 0, str(run.get("run_id") or ""))


def _newest_capsule(path: str) -> tuple[str, str]:
    """A directory means *the newest run's capsule that has something to say*, and the choice is printed.

    Every run of a Python child leaves two capsules with the same run id — the child's tape and the SDK helper tape — and
    the newest file is not always the interesting one: a five-frame helper tape can win on mtime and describe nothing
    (B7 in `docs/RELEASE-BOOKERS.md`[*]). Inside the newest run, the capsule with the most model calls wins, then the most
    events; a capsule outside that run is never preferred. The message names what was passed over, so the reader can
    overrule it by naming the capsule explicitly.

    [*] `docs/RELEASE-BLOCKERS.md`.
    """
    found = glob.glob(os.path.join(path, "**", "*.bbx"), recursive=True)
    if not found:
        raise EvidenceError(f"{path} holds no .bbx capsule (looked recursively)")
    newest = max(found, key=os.path.getmtime)
    summaries = {candidate: _summary_of(candidate) for candidate in found}
    newest_run = summaries[newest][2]
    same_run = [candidate for candidate in found if newest_run and summaries[candidate][2] == newest_run]
    pool = same_run or [newest]
    chosen = max(pool, key=lambda candidate: (summaries[candidate][0], summaries[candidate][1],
                                              os.path.getmtime(candidate)))
    if len(found) == 1:
        return chosen, f"reading {chosen}"
    passed_over = [candidate for candidate in found if candidate != chosen]
    detail = "; ".join(f"{candidate}: {summaries[candidate][0]} model call(s), {summaries[candidate][1]} event(s)"
                       for candidate in passed_over)
    return chosen, (f"reading {chosen} ({summaries[chosen][0]} model call(s), {summaries[chosen][1]} event(s); "
                    f"passed over {detail} — name a capsule explicitly to diagnose a different one)")


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
        if os.path.isdir(args.path):
            target, choice = _newest_capsule(args.path)
        else:
            target, choice = args.path, ""
        evidence = load(target, max_events=args.max_events)
    except EvidenceError as exc:
        print(f"blackbox diagnose: {exc}", file=sys.stderr)
        return 1
    if choice:
        print(choice, file=sys.stderr)
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
