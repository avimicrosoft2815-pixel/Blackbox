"""The interruption lab: kill a real process at each interruption class and check what the evidence says afterwards.

This is the evidence script behind `tests/test_interruption_lab.py`. The tests assert the properties; this writes the
artifact those assertions were run against, with the machine it ran on, so the claim can be re-checked rather than
believed.

    .venv/bin/python bench/interruption_lab.py --repeats 3

What the output does and does not mean:

* **it does** show, for each §14.4 class, the node statuses and diagnosis findings produced from a tape left by a
  process that was killed with a real signal, recovered by the real recovery, parsed by the real reader;
* **it does not** measure anything. These are counts and labels, not latencies: there is no number here that a
  competitor could be compared against, and the report says so in its own `limits` block;
* **it does not prove** the kill landed where the scenario intended on every OS. `os.kill` with `SIGKILL` is a hard kill
  on POSIX; on Windows the child's `SIGTERM` fallback is `TerminateProcess`, which is also a hard kill — but a machine
  whose signal handling differs would show up as a different node status, and the row would say so rather than pass
  quietly.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.diagnose import build, load
from blackbox.rec import recovery

CHILD = ROOT / "tests" / "_interruption_child.py"
SCENARIOS = ("mid-stream", "before-first-byte", "in-tool", "after-response", "provider-error", "clean")


def run_once(scenario: str, workdir: Path) -> dict:
    data = workdir / scenario / "data"
    data.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUNBUFFERED": "1"}
    completed = subprocess.run([sys.executable, str(CHILD), scenario, str(data)],
                               capture_output=True, text=True, env=env, timeout=180)
    recovered = recovery.recover(str(data))
    row: dict = {"scenario": scenario, "child_exit": completed.returncode,
                 "capsule": bool(recovered.capsule_path)}
    if not recovered.capsule_path:
        row["stderr_tail"] = completed.stderr[-600:]
        return row
    evidence = load(recovered.capsule_path, max_events=1000)
    report = build(evidence)
    row.update({
        "capsule_id": evidence.capsule_id,
        "capsule_bytes": os.path.getsize(recovered.capsule_path),
        "frames": evidence.events_seen,
        "root_ok": evidence.root_ok,
        "reached_run_end": evidence.has_run_end,
        "content_mode": evidence.content_mode,
        "node_statuses": {str(node.slot): node.status for node in evidence.nodes},
        "findings": [{"id": f.id, "verdict": f.verdict, "confidence": f.confidence,
                      "alternatives": len(f.alternatives)} for f in report.findings],
        "brief_has_unknowns": "## Unknowns" in report.brief(),
        "brief_names_no_location": "No file or line is named here" in report.brief(),
    })
    return row


def run(repeats: int, workdir: Path) -> dict:
    rows = {scenario: [run_once(scenario, workdir) for _ in range(repeats)] for scenario in SCENARIOS}
    summary = {}
    for scenario, runs in rows.items():
        statuses = sorted({s for run_ in runs for s in run_.get("node_statuses", {}).values()})
        findings = sorted({f["id"] for run_ in runs for f in run_.get("findings", [])})
        verdicts = sorted({f["verdict"] for run_ in runs for f in run_.get("findings", [])})
        summary[scenario] = {
            "runs": len(runs),
            "capsules_built": sum(1 for r in runs if r["capsule"]),
            "all_roots_ok": all(r.get("root_ok") for r in runs if r["capsule"]),
            "stable_node_statuses": statuses,
            "stable_findings": findings,
            "verdicts_seen": verdicts,
            "child_exits": sorted({r["child_exit"] for r in runs}),
        }
    return {"rows": rows, "summary": summary}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / f"{time.strftime('%Y-%m-%d')}-interruption-lab.json"))
    args = ap.parse_args(argv)
    workdir = Path(time.strftime("/tmp/bbx-interruption-lab-%Y%m%dT%H%M%S"))
    workdir.mkdir(parents=True, exist_ok=True)
    report = run(args.repeats, workdir)
    report["hardware"] = {"cpu_count": os.cpu_count(), "python": platform.python_version(),
                          "implementation": platform.python_implementation(), "os": platform.platform(),
                          "machine": platform.machine(), "repeats": args.repeats, "pid": os.getpid(),
                          "topology": "one child process per run, killed with a real signal; recovery and diagnosis "
                                      "in the parent, so the tape crosses a process boundary in both directions"}
    report["limits"] = [
        "these are counts and labels, not measurements: nothing here is a latency, a throughput or a cost",
        "one machine, one kernel; the kill lands at the point the scenario names on POSIX, and on Windows the child's "
        "SIGTERM fallback is TerminateProcess — a different signal model would show as a different node status",
        "the six classes are the ones §14.4 names; a real-world interruption can combine them (a torn tail plus a cut "
        "stream), which this lab does not synthesise",
        "the diagnosis is read-only: this artifact cannot show that a fix works, only that the evidence supports the "
        "labels it prints",
    ]
    report["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for scenario, row in report["summary"].items():
        print(f"{scenario:18s} caps {row['capsules_built']}/{row['runs']}  roots_ok={row['all_roots_ok']!s:5s} "
              f"nodes={','.join(row['stable_node_statuses']) or '—'}")
        print(f"{'':18s} findings={','.join(row['stable_findings']) or '—'}  verdicts={set(row['verdicts_seen'])}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
