"""`blackbox ls` — what is in this data directory (P4-05, the half a new user needs first).

The quickstart used to end with "the capsule is under `data/capsules/`": a directory listing, which works only if the
reader already knows which of the two capsules a run produces is the one to open. `ls` reads each capsule's **manifest**
(1 KiB, not the entries) and prints what a person needs to choose: the capsule file, the run, the recovery status and
why, the number of events, and the size. A capsule that cannot be read is listed as unreadable with the reason rather
than dropped — a data directory with a broken capsule in it is exactly when someone runs `ls`.

It reads nothing else: no derived parts, no segments, no verification (that is `blackbox verify`), and it never writes.
Exit codes follow the front door: 0 listed something, 0 with an empty directory (an empty list is an answer), 2 the data
directory itself is unusable.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from typing import Any

from blackbox.cap import format as cap
from blackbox.cli.paths import default_data_dir


def capsules_in(data_dir: str) -> list[str]:
    """Every `.bbx` under `data_dir/capsules`, sorted newest-first by modification time, then by name."""
    paths = glob.glob(os.path.join(data_dir, "capsules", "*.bbx"))

    def key(path: str) -> tuple[float, str]:
        try:
            return (-os.path.getmtime(path), path)
        except OSError:
            return (0.0, path)

    return sorted(paths, key=key)


def describe(path: str) -> dict[str, Any]:
    """One row: what the manifest says, or why the file cannot be read. Never raises for a bad capsule."""
    row: dict[str, Any] = {"capsule": path, "name": os.path.basename(path),
                           "bytes": None, "readable": False, "error": None}
    try:
        row["bytes"] = os.path.getsize(path)
    except OSError as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row
    try:
        capsule = cap.read_capsule(path)
    except Exception as exc:                      # a broken capsule is a row, not a crash
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row
    manifest = capsule.manifest
    run_block, counts_block = manifest.get("run"), manifest.get("counts")
    run: dict[str, Any] = run_block if isinstance(run_block, dict) else {}
    counts: dict[str, Any] = counts_block if isinstance(counts_block, dict) else {}
    row.update({
        "readable": True,
        "capsule_id": manifest.get("capsule_id"),
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "status_cause": run.get("status_cause"),
        "completeness": run.get("completeness"),
        "events": counts.get("events"),
        "nodes": counts.get("nodes"),
        "root_ok": bool(capsule.root_ok),
        "derived_parts": len(manifest.get("derived_parts") or []),
    })
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="blackbox ls",
        description="List the capsules in a data directory by reading their manifests. Read-only; a capsule that cannot "
                    "be read is listed with the reason rather than dropped. `--json` for tooling.")
    ap.add_argument("--data", help="data directory (default $BLACKBOX_DATA_DIR, $BLACKBOX_HOME, or the current "
                                    "directory, which is where `blackbox run` writes)")
    ap.add_argument("--limit", type=int, default=0, help="show at most N capsules (default: all)")
    ap.add_argument("--json", action="store_true", help="print the rows as JSON")
    args = ap.parse_args(argv)

    data_dir = default_data_dir(args.data)
    if os.path.exists(data_dir) and not os.path.isdir(data_dir):
        print(f"blackbox ls: {data_dir} is not a directory")
        return 2
    rows = [describe(path) for path in capsules_in(data_dir)]
    if args.limit > 0:
        rows = rows[:args.limit]
    if args.json:
        print(json.dumps({"data_dir": os.path.abspath(data_dir), "capsules": rows}, indent=2, sort_keys=True))
        return 0
    if not rows:
        print(f"blackbox ls: no capsules under {os.path.join(os.path.abspath(data_dir), 'capsules')}")
        print("  run an app under the recorder first: blackbox run -- python app.py")
        return 0
    header = ("CAPSULE", "RUN", "STATUS", "EVENTS", "SIZE")
    table = []
    for row in rows:
        if not row["readable"]:
            table.append((row["name"], "-", "unreadable", "-", _size(row["bytes"]), row["error"] or ""))
            continue
        cause = f"{row['status']}/{row['status_cause']}" if row.get("status_cause") else str(row.get("status"))
        table.append((str(row.get("capsule_id") or row["name"]), str(row.get("run_id")),
                      cause, str(row.get("events")), _size(row["bytes"]),
                      "" if row["root_ok"] else "integrity root does not verify"))
    widths = [max(len(str(header[i])), *(len(str(item[i])) for item in table)) for i in range(5)]
    print("  ".join(str(header[i]).ljust(widths[i]) for i in range(5)))
    for item in table:
        print("  ".join(str(item[i]).ljust(widths[i]) for i in range(5)) + (f"  {item[5]}" if item[5] else ""))
    bad = [row for row in rows if not row["readable"] or not row["root_ok"]]
    if bad:
        print(f"blackbox ls: {len(bad)} of {len(rows)} capsule(s) need attention (see the last column)")
    return 0


def _size(value: Any) -> str:
    if not isinstance(value, int):
        return "-"
    for unit, scale in (("MiB", 1 << 20), ("KiB", 1 << 10)):
        if value >= scale:
            return f"{value / scale:.1f} {unit}"
    return f"{value} B"


if __name__ == "__main__":
    raise SystemExit(main())
