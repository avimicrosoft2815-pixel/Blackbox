"""`blackbox index` — build the data-directory index from the capsules, and rebuild it from nothing.

    blackbox index --rebuild                 # throw the index away, re-read every capsule
    blackbox index                           # ingest what is new, leave what is there (idempotent)
    blackbox index --dump                    # print the canonical dump's sha256

The command reads capsules and writes one file under `<data>/index/`. It never touches a tape, never seals anything and
never deletes a capsule: a rebuild is recoverable because the sources are still there, which is the whole point of I8.
Exit codes: **0** the index was built (with or without unreadable capsules, which are reported), **2** the data
directory cannot be used at all.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from blackbox.cli.paths import default_data_dir
from blackbox.index import build_index, dump_hash, rebuild_index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="blackbox index",
        description="Index the sealed capsules in a data directory. `--rebuild` throws the index away first; the result "
                    "is a pure function of the capsules, so a rebuild of an unchanged corpus produces the same rows.")
    ap.add_argument("--data", help="data directory (default $BLACKBOX_DATA_DIR, $BLACKBOX_HOME, or the current directory)")
    ap.add_argument("--rebuild", action="store_true", help="delete the index and re-read every capsule")
    ap.add_argument("--dump", action="store_true", help="print the canonical dump's sha256 and row count")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    data_dir = args.data or default_data_dir()
    if not os.path.isdir(data_dir):
        print(f"blackbox index: {data_dir} is not a directory", file=sys.stderr)
        return 2
    try:
        report = rebuild_index(data_dir) if args.rebuild else build_index(data_dir)
    except OSError as exc:
        print(f"blackbox index: cannot write the index: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.dump:
        report["dump_sha256"] = dump_hash(data_dir)
        report["dump_rows"] = sum(report["counts"].values())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    counts = report["counts"]
    print(f"blackbox index: {report['mode']} of {report['capsules_seen']} capsule(s) in {report['elapsed_ms']:.0f} ms")
    print(f"  runs {counts['run']}, nodes {counts['node']}, events {counts['event']}, artifacts {counts['artifact']}"
          f" -> {report['path']}")
    for row in report["unreadable"]:
        print(f"  skipped {row['capsule']}: {row['error']}")
    for line in report["disagreements"]:
        print(f"  disagrees: {line}")
    if args.dump:
        print(f"  canonical dump sha256 {report['dump_sha256']} over {report['dump_rows']} row(s)")
    return 0
