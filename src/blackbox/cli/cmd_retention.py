"""`blackbox retention` — what the retention policy would delete, and why, without deleting anything.

    blackbox retention                                  # explain against the configured limits
    blackbox retention --max-bytes 5000000 --keep-hours 24
    blackbox retention --json

Retention is normally applied by the writer that owns a tape, at the moment it rolls a segment: committing a `GAP`, then
unlinking, in that order. This command deliberately cannot delete: it has no writer, so it cannot commit the `GAP` that
makes a deletion honest, and a tool that unlinked a segment without one would leave a tape that looks whole and is not.
What it can do — and what the release gate asks for — is explain every deletion before it happens: one row per segment,
the reason it is a victim or not, and the `GAP` span that the real pass would declare.

Exit codes: **0** a plan was produced (including "nothing to delete"), **2** the data directory or tape cannot be read.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from blackbox.cli.paths import default_data_dir
from blackbox.tape import retention as retention_mod
from blackbox.tape import scanner


def _newest_tape(data_dir: str) -> tuple[str, str] | None:
    root = os.path.join(data_dir, "tape")
    if not os.path.isdir(root):
        return None
    best: tuple[int, str] | None = None
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, name)
    return (os.path.join(root, best[1]), best[1]) if best else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="blackbox retention",
        description="Explain what the retention policy would delete from a tape, and why. This command never deletes: "
                    "deletion has to commit a GAP frame first, which only the tape's writer can do.")
    ap.add_argument("--data", help="data directory (default $BLACKBOX_DATA_DIR, $BLACKBOX_HOME, or the current directory)")
    ap.add_argument("--max-bytes", type=int, default=None, help="the budget for one tape, in bytes")
    ap.add_argument("--keep-hours", type=float, default=None, help="do not delete anything younger than this many hours")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    args = ap.parse_args(argv)

    data_dir = args.data or default_data_dir()
    if not os.path.isdir(data_dir):
        print(f"blackbox retention: {data_dir} is not a directory", file=sys.stderr)
        return 2
    found = _newest_tape(data_dir)
    if found is None:
        print(f"blackbox retention: {data_dir} holds no tape", file=sys.stderr)
        return 2
    tape_dir, writer_uuid = found
    try:
        scan = scanner.scan(data_dir, writer_uuid)
    except Exception as exc:
        print(f"blackbox retention: {getattr(exc, 'code', type(exc).__name__)}: {exc}", file=sys.stderr)
        return 2
    if scan.head is None:
        print(f"blackbox retention: the newest tape ({writer_uuid}) has no valid head", file=sys.stderr)
        return 2

    plan = retention_mod.select(tape_dir, active_segment=scan.head.active_segment,
                                max_bytes=args.max_bytes, keep_hours=args.keep_hours)
    rows = retention_mod.explain(plan)
    would_declare: list[dict[str, int | list[int]]] = []
    if plan["victims"]:
        spans = [(c["first_seq"], c["last_seq"]) for c in plan["victims"]]
        would_declare = [{"from": min(a for a, _ in spans), "to": max(b for _, b in spans),
                          "segments": sorted(c["segment"] for c in plan["victims"])}]

    if args.json:
        print(json.dumps({"tape": tape_dir, "writer_uuid": writer_uuid, "dry_run": True,
                          "limits": {"max_bytes": args.max_bytes, "keep_hours": args.keep_hours},
                          "tape_bytes": plan["tape_bytes"], "sealed_bytes": plan["sealed_bytes"],
                          "active_bytes": plan["active_bytes"], "would_free_bytes": plan["freed_bytes"],
                          "bytes_after": plan["keep_bytes"], "under_budget": plan["under_budget"],
                          "would_declare": would_declare, "segments": rows}, indent=2, sort_keys=True))
        return 0

    print(f"blackbox retention: {tape_dir}")
    print(f"  {plan['tape_bytes']} B on the tape: {plan['sealed_bytes']} B sealed in "
          f"{len(plan['candidates'])} segment(s), {plan['active_bytes']} B active")
    limits = []
    if args.max_bytes is not None:
        limits.append(f"budget {args.max_bytes} B")
    if args.keep_hours is not None:
        limits.append(f"keep window {args.keep_hours:g} h")
    print(f"  limits: {', '.join(limits) if limits else 'none given — nothing would be deleted'}")
    print("  dry run: this command never deletes; the writer commits a GAP first, then unlinks")
    for row in rows:
        span = f"seq {row['first_seq']}–{row['last_seq']}" if row["first_seq"] is not None else "seq unknown"
        print(f"  segment {row['segment']:>2}  {span:<16} {row['bytes']:>8} B  {row['verdict']:<7} "
              f"{'; '.join(row['reasons'])}")
    if would_declare:
        planned = would_declare[0]
        print(f"  would free {plan['freed_bytes']} B; {plan['keep_bytes']} B would remain. The real pass would commit "
              f"one GAP for seq {planned['from']}–{planned['to']}.")
    else:
        print(f"  nothing would be deleted; {plan['keep_bytes']} B would remain.")
    return 0
