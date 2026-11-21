"""What one index build costs: wall time, transactions and statements, for N capsules.

The acceptance row (I8) builds 500 capsules and rebuilds the index; defect 114 records that on Windows runners that test
crossed the suite's 120 s watchdog three times while its neighbours passed. The number a reader needs to judge the fix is
not "seconds on this laptop" but **how many sync points the build asks the filesystem for** — a commit is a journal write
and a sync, and that is the part a Windows volume with a scanner on it charges most for.

Usage:

    PYTHONPATH=src .venv/bin/python tools/index_build_cost.py --runs 500 --out bench/results/<date>-index-build-cost.json

It builds N one-run directories, seals each with the real recovery path, moves the capsule into a corpus directory (the
same construction `tests/test_index_rebuild.py` uses), then measures an incremental build and a rebuild with a trace
callback attached, so the counts are the statements the product issued rather than an estimate.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import blackbox.index.build as index_build                      # noqa: E402
from blackbox.index import schema                               # noqa: E402
from tests.test_index_rebuild import _seal_everything, _tape    # noqa: E402   the acceptance test's own corpus builder


def _corpus(root: Path, runs: int) -> tuple[Path, float]:
    data = root / "corpus"
    (data / "capsules").mkdir(parents=True)
    started = time.perf_counter()
    for run in range(runs):
        scratch = root / f"run-{run:03d}"
        scratch.mkdir()
        _tape(scratch, run_id=f"r_{run:016x}")
        if _seal_everything(scratch) != 1:
            raise SystemExit(f"run {run} did not seal exactly one capsule")
        (capsule,) = list(Path(scratch, "capsules").glob("*.bbx"))
        capsule.rename(data / "capsules" / capsule.name)
    return data, time.perf_counter() - started


def _measure(data: Path, statements: list[str]) -> dict[str, float]:
    real_connect = schema.connect

    def traced(*args, **kwargs):
        con = real_connect(*args, **kwargs)
        con.set_trace_callback(lambda sql: statements.append(sql.strip().split()[0].upper()) if sql.strip() else None)
        return con

    schema.connect = traced
    try:
        started = time.perf_counter()
        report = index_build.build(data)
        incremental_ms = (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        index_build.rebuild(data)
        rebuild_ms = (time.perf_counter() - started) * 1000.0
    finally:
        schema.connect = real_connect
    return {"incremental_ms": round(incremental_ms, 1), "rebuild_ms": round(rebuild_ms, 1),
            "rows_added": report["rows_added"], "counts": report["counts"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=120)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    root = Path(tempfile.mkdtemp(prefix="bbx-index-cost-"))
    data, corpus_s = _corpus(root, args.runs)
    statements: list[str] = []
    measured = _measure(data, statements)
    document = {
        "tool": "tools/index_build_cost.py",
        "command": f"PYTHONPATH=src .venv/bin/python tools/index_build_cost.py --runs {args.runs}",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runs": args.runs,
        "host": {"platform": platform.platform(), "machine": platform.machine(),
                 "python": platform.python_version(), "cpus": __import__("os").cpu_count()},
        "corpus_build_s": round(corpus_s, 2),
        "commits": statements.count("COMMIT"),
        "begins": statements.count("BEGIN"),
        "selects": statements.count("SELECT"),
        "inserts": statements.count("INSERT"),
        **measured,
        "note": ("`commits` counts the COMMIT statements the *build* and the *rebuild* issued together. It is the number "
                 "of sync points a filesystem is asked for; before defect 114's fix it was one per capsule per build."),
    }
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
