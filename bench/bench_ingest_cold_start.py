"""P3B-07's bench-box row, made runnable anywhere: `blackbox ingest` cold start, with a baseline.

The row says **p50 / p95 ≤ 40 ms on the bench box**. This branch had a number for it in two prose places that
disagreed (p50 32.0 / p95 40.2 in `docs/STATUS.md` and `docs/progress.md`, p50 30.3 / p95 38.3 in
`docs/RELEASE-BLOCKERS.md`), and no artefact under `bench/results/` — which is the state a measurement is in right
before it becomes folklore. This script is the protocol, so the bench box runs the same thing this sandbox ran:

    PYTHONPATH=src python bench/bench_ingest_cold_start.py --n 60 --where sandbox

Three columns, because a cold start is three costs stacked and only one of them is ours:

| column | what is spawned | what it measures |
|---|---|---|
| `bare_python` | `python -c pass` | the interpreter floor every Python CLI pays |
| `cli_floor` | `blackbox ingest --help` | the entry point, imports and argparse, with no work done |
| `ingest` | `blackbox ingest --tool claude-code --event PostToolUse` with a real body on stdin | the row: JSON in, one atomic inbox file out, exit 0 |

Each sample is a **real process** launched from scratch (no reuse, no fork server), timed around `subprocess.run` with
`perf_counter_ns`, and the order of the three columns is randomised per trial so a load spike lands on all of them
instead of on whichever ran last. Percentiles are nearest-rank on one sorted array (`bench/_stats.py`, P1-17), `n` sits
beside every percentile, and the median absolute deviation travels with each column so a reader can see whether the
machine was quiet. The inbox is emptied between samples, because an inbox that grows makes later samples slower and
the row is about the first event, not the hundredth.

What this file does **not** do is claim the bench-box row: `verdict.on_the_bench_box` is false unless
`--where bench-box` says otherwise, and the numbers below are this host's. The honest reading is "the same protocol,
on the wrong machine" — which is what `docs/RELEASE-BLOCKERS.md` B6 asks the release machine to finish.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

import _stats
from bench_frame import hardware

BUDGET_P95_MS = 40.0              # the plan's P3B-07 row, per host
BUDGET_SOURCE = "docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md P3B-07 (cold start <= 40 ms p95 on the bench box)"

#: A real Claude Code `PostToolUse` body, trimmed to the fields the mapping reads: the row is about the command a hook
#: runs, so the body should be the shape a hook actually pipes in rather than `{}`.
BODY = json.dumps({
    "session_id": "s-cold-start",
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "ls -la src/blackbox", "description": "list the package"},
    "tool_response": {"stdout": "…", "stderr": "", "interrupted": False},
    "cwd": "/home/user/Blackbox",
}).encode()


def _entry(python: str) -> tuple[list[str], str]:
    """The command the hook runs: the console script when this venv has one, else `python -m blackbox`.

    A hook executes `blackbox ingest`, so measuring `python -m blackbox` would time importlib's runpy path instead of the
    entry point. The choice is recorded in the artefact, because a reader comparing two columns from different shapes
    should be able to see that.
    """
    script = str(Path(python).with_name("blackbox"))
    if Path(script).exists():
        return [script], "console-script"
    return [python, "-m", "blackbox"], "python -m"


def _columns(python: str) -> dict[str, list[str]]:
    blackbox, _ = _entry(python)
    return {
        "bare_python": [python, "-c", "pass"],
        "cli_floor": [*blackbox, "ingest", "--help"],
        "ingest": [*blackbox, "ingest", "--tool", "claude-code", "--event", "PostToolUse",
                   "--session", "s-cold-start"],
    }


def _run(command: list[str], *, env: dict[str, str], stdin: bytes) -> float:
    start = time.perf_counter_ns()
    proc = subprocess.run(command, input=stdin, capture_output=True, env=env, cwd=str(ROOT))
    elapsed_ms = (time.perf_counter_ns() - start) / 1e6
    if proc.returncode != 0:
        raise SystemExit(f"{' '.join(command)} exited {proc.returncode}: {proc.stderr.decode()[-400:]}")
    return elapsed_ms


def _spread(samples: list[float]) -> float:
    """Median absolute deviation: a cheap way to say "this machine was quiet" without claiming normality."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    median = _stats.nearest_rank(ordered, 0.50)
    return round(_stats.nearest_rank(sorted(abs(s - median) for s in samples), 0.50), 3)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="`blackbox ingest` cold start (P3B-07)")
    ap.add_argument("--n", type=int, default=60, help="samples per column")
    ap.add_argument("--warmup", type=int, default=5, help="samples per column, discarded")
    ap.add_argument("--where", default="sandbox", choices=("sandbox", "bench-box"),
                    help="which host this is; only `bench-box` can satisfy the row")
    ap.add_argument("--seed", type=int, default=20261119)
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / f"{time.strftime('%Y-%m-%d')}-ingest-cold-start.json"))
    args = ap.parse_args(argv)

    python = sys.executable
    columns = _columns(python)
    entry_point = _entry(python)[1]
    rng = random.Random(args.seed)
    data_dir = Path(tempfile.mkdtemp(prefix="bbx-cold-"))
    env = {**os.environ, "BLACKBOX_DATA_DIR": str(data_dir), "BLACKBOX_INBOX": str(data_dir / "inbox"),
           "PYTHONPATH": str(ROOT / "src")}
    samples: dict[str, list[float]] = {name: [] for name in columns}

    def one(name: str) -> float:
        inbox = data_dir / "inbox"
        shutil.rmtree(inbox, ignore_errors=True)
        stdin = BODY if name == "ingest" else b""
        return _run(columns[name], env=env, stdin=stdin)

    try:
        for _ in range(args.warmup):                       # the disk's page cache, not ours: discarded
            for name in columns:
                one(name)
        for _ in range(args.n):
            order = list(columns)
            rng.shuffle(order)                             # a spike lands on every column, not on the last one
            for name in order:
                samples[name].append(one(name))
        # The timed samples are the numbers; this one extra run is the proof that the thing being timed did the work.
        # A command that exits 0 after failing open is fast and means nothing, so a final ingest is run and the inbox
        # is inspected: one file, no `.tmp` left behind.
        shutil.rmtree(data_dir / "inbox", ignore_errors=True)
        proof_exit = _run(columns["ingest"], env=env, stdin=BODY)
        files = sorted(p for p in (data_dir / "inbox").rglob("*") if p.is_file()) if (data_dir / "inbox").is_dir() else []
        left_tmp = [p for p in files if p.name.endswith(".tmp")]
        inbox_files = [p for p in files if not p.name.endswith(".tmp")]
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)

    results = {}
    for name, values in samples.items():
        summary = _stats.summary(values, digits=3)
        summary["mad"] = _spread(values)
        summary["unit"] = "ms"
        results[name] = summary
    results["ingest"]["budget"] = {"p95_ms_max": BUDGET_P95_MS}
    results["ingest"]["gate"] = args.where == "bench-box"
    results["ingest"]["marginal_over_cli_floor_p50_ms"] = round(
        results["ingest"]["p50"] - results["cli_floor"]["p50"], 3)
    results["ingest"]["marginal_over_interpreter_p50_ms"] = round(
        results["ingest"]["p50"] - results["bare_python"]["p50"], 3)

    verdict = {
        "on_the_bench_box": args.where == "bench-box",
        "p95_within_40_ms": results["ingest"]["p95"] <= BUDGET_P95_MS,
        "the_command_wrote_an_inbox_file": bool(inbox_files) and not left_tmp,
        "row_satisfied_here": args.where == "bench-box" and results["ingest"]["p95"] <= BUDGET_P95_MS,
    }
    document = {
        "date": time.strftime("%Y-%m-%d"),
        "ticket": "P3B-07",
        "probe": "`blackbox ingest` cold start: one hook event from stdin to the inbox, real process per sample",
        "protocol": (
            "each sample is a fresh `subprocess.run` timed with perf_counter_ns around the spawn; three columns "
            "(bare interpreter, `ingest --help` as the CLI floor, and the real ingest with a populated PostToolUse body "
            "on stdin); column order randomised per trial with a fixed seed; the inbox is removed before every sample "
            "so this is the first event and not the hundredth; warm-up samples discarded; nearest-rank percentiles from "
            "one sorted array with n and MAD beside them (P1-17); exit code checked and a non-zero exit aborts rather "
            "than being timed"),
        "budget": {"p95_ms_max": BUDGET_P95_MS, "source": BUDGET_SOURCE},
        "where": args.where,
        "entry_point": entry_point,
        "hardware": hardware(),
        "host": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                 "os": f"{platform.system()}-{platform.release()}"},
        "n_per_column": args.n,
        "warmup_per_column": args.warmup,
        "command": " ".join([Path(sys.executable).name, "bench/bench_ingest_cold_start.py", "--n", str(args.n),
                             "--where", args.where]),
        "results": results,
        "proof": {"final_ingest_ms": round(proof_exit, 3), "inbox_files": len(inbox_files),
                  "tmp_files_left": len(left_tmp)},
        "verdict": verdict,
    }
    Path(args.out).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(verdict, indent=2))
    print(f"ingest p50 {results['ingest']['p50']} / p95 {results['ingest']['p95']} / p99 {results['ingest']['p99']} ms "
          f"(n={results['ingest']['n']}, mad {results['ingest']['mad']}); "
          f"floor {results['cli_floor']['p50']} ms; wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
