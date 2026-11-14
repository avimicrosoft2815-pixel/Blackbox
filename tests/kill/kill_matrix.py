"""P2-09 — randomized, asynchronous kill matrix.

The parent sends `SIGKILL` to a child in a tight append loop at a random delay, so the kill lands wherever it
lands: mid-memcpy, mid-slot-write, mid-roll. This is the asynchronous counterpart of the named `os._exit` stages
in `tests/kill/stage_probe.py` (P2-08).

Invariants checked per trial, from the recovered tape alone:

* the recovered seq numbers are a dense prefix `1..N` — no duplicate, no hole;
* every recovered frame passes its CRC (the scanner enforces it);
* nothing is reported as a torn tail inside the committed region;
* the tape reopens and the interrupted roll (if any) is adopted;
* `head_seq >= N` (the head never lags a frame that was published).

Usage: PYTHONPATH=src python tests/kill/kill_matrix.py --trials 500 [--json PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.tape import scanner
from blackbox.tape.writer import TapeWriter

CHILD = ROOT / "tests" / "kill" / "_append_loop.py"

CONFIGS = [
    {"mode": "process", "segment_bytes": 64 << 10, "event_bytes": 32},
    {"mode": "process", "segment_bytes": 64 << 10, "event_bytes": 1024},
    {"mode": "process", "segment_bytes": 1 << 20, "event_bytes": 256},
    {"mode": "kernel", "segment_bytes": 1 << 20, "event_bytes": 256},
]
BATCHES = [0, 4096]


def one_trial(tmp: Path, cfg: dict, batch: int, delay_s: float, seed: int) -> dict:
    data_dir = tmp / f"t{seed}"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env.pop("BLACKBOX_DIE_AT", None)
    proc = subprocess.Popen([sys.executable, str(CHILD), str(data_dir), cfg["mode"],
                             str(cfg["segment_bytes"]), str(cfg["event_bytes"]), str(batch)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
    time.sleep(delay_s)
    os.kill(proc.pid, signal.SIGKILL)
    _, err = proc.communicate(timeout=60)
    result = {"seed": seed, "delay_ms": round(delay_s * 1000, 1), "batch_bytes": batch, **cfg,
              "violations": [], "not_started": False}

    tape_dirs = [p for p in (data_dir / "tape").iterdir() if p.is_dir()] if (data_dir / "tape").exists() else []
    if not tape_dirs:
        # the kill can land before the child finished importing and created its tape dir; that trial says
        # nothing about the commit protocol, so it is recorded separately rather than counted as a violation
        result["not_started"] = not (data_dir / "tape").exists()
        if err:
            result["stderr"] = err.decode()[-400:]
        return result
    uuid = tape_dirs[0].name
    try:
        scan = scanner.scan(str(data_dir), uuid)
    except Exception as exc:                       # a scan that raises is a violation in itself
        result["violations"].append(f"scan raised {type(exc).__name__}: {exc}")
        return result

    seqs = [f["seq"] for f in scan.frames]
    result.update({"frames": len(seqs), "head_seq": scan.head.head_seq if scan.head else None,
                   "last_seq": scan.last_seq, "truncated_tail": scan.truncated_tail,
                   "gaps": scan.gaps, "segments": len(scan.segments_seen)})
    if not scan.meta and not seqs:
        result["not_started"] = True          # the child died before it wrote its meta page
        return result
    if seqs != list(range(1, len(seqs) + 1)):
        result["violations"].append("recovered seq numbers are not a dense prefix from 1")
    if scan.gaps:
        result["violations"].append("scan reported gaps inside the committed region")
    if scan.truncated_tail:
        result["violations"].append("committed region reported as a torn tail")
    if scan.head is None and seqs:
        result["violations"].append("head unreadable while frames exist")
    elif scan.head is not None and scan.head.head_seq < len(seqs):
        result["violations"].append("head lags behind recovered frames")

    # the tape must reopen: adoption of an interrupted roll included
    try:
        writer = TapeWriter(str(data_dir), writer_uuid=bytes.fromhex(uuid),
                            segment_bytes=cfg["segment_bytes"], mode=cfg["mode"]).open()
        result["adopted"] = bool(writer.adopted)
        writer.append_and_publish(9, b"post-kill")
        writer.close()
        after = scanner.scan(str(data_dir), uuid)
        if after.last_seq < (scan.last_seq or 0):
            result["violations"].append("frames lost by iterating on a recovered tape")
    except Exception as exc:
        result["violations"].append(f"reopen failed: {type(exc).__name__}: {exc}")
    return result


def _detect_fstype(path: str) -> str:
    """C1: a kill test on tmpfs proves the protocol, not the storage; label which one ran."""
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "unknown"
    best, best_len = "unknown", -1
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            mount = parts[1].replace("\\040", " ")
            if path.startswith(mount) and len(mount) > best_len:
                best, best_len = parts[2], len(mount)
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=500)
    ap.add_argument("--max-delay-ms", type=int, default=250)
    ap.add_argument("--json", default=None)
    ap.add_argument("--seed", type=int, default=20261114)
    ap.add_argument("--root", default=None, help="scratch directory (C1: a real disk, not tmpfs)")
    ap.add_argument("--filesystem", default=None, help="label for the filesystem actually used")
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)      # noqa: S311 - scheduling jitter, not secrecy
    tmp = Path(__import__("tempfile").mkdtemp(prefix="bbx-kill-", dir=args.root))
    trials: list[dict] = []
    started = time.perf_counter()
    for i in range(args.trials):
        cfg = CONFIGS[i % len(CONFIGS)]
        batch = BATCHES[(i // len(CONFIGS)) % len(BATCHES)]
        delay = rng.uniform(0.05, args.max_delay_ms / 1000)
        trials.append(one_trial(tmp, cfg, batch, delay, i))
        if (i + 1) % 50 == 0:
            bad = sum(1 for t in trials if t["violations"])
            print(f"  {i + 1}/{args.trials} trials, {bad} with violations", flush=True)

    elapsed = time.perf_counter() - started
    violations = [t for t in trials if t["violations"]]
    summary: dict = {}
    for t in trials:
        key = f"{t['mode']}/seg{t['segment_bytes']}/ev{t['event_bytes']}/batch{t['batch_bytes']}"
        row = summary.setdefault(key, {"trials": 0, "violations": 0, "frames": 0})
        row["trials"] += 1
        row["violations"] += len(t["violations"])
        row["frames"] += t.get("frames", 0)
    out = {
        "date": time.strftime("%Y-%m-%d"), "probe": "P2-09 randomized parent-SIGKILL matrix",
        "trials": args.trials, "seed": args.seed, "max_delay_ms": args.max_delay_ms,
        "elapsed_s": round(elapsed, 1),
        "filesystem": args.filesystem or _detect_fstype(str(tmp)),
        "scratch": "<scratch>/" + Path(tmp).name,
        "hardware": {"python": platform.python_version(), "os": platform.platform(),
                     "machine": platform.machine(), "cpu_count": os.cpu_count()},
        "configs": [{"config": k, **v} for k, v in sorted(summary.items())],
        "violations": violations[:20],
        "violation_count": len(violations),
        "not_started": sum(1 for t in trials if t.get("not_started")),
        "trials_detail": trials,
    }
    path = args.json or str(ROOT / "bench" / "results" / f"{out['date']}-kill-random.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    __import__("shutil").rmtree(tmp, ignore_errors=True)      # a 500-trial matrix is GiB of scratch
    print(f"wrote {path}: {args.trials} trials, {len(violations)} with violations, {elapsed:.1f}s")
    for row in out["configs"]:
        print(f"  {row['config']:44s} trials={row['trials']:4d} violations={row['violations']}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
