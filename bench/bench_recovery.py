"""§15.1 — recovery time, RSS, and disk amplification, measured on this machine.

* recovery: open a killed tape of N frames and time `scanner.scan` (the number the gate names);
* RSS: peak resident set of a process that appends N events and commits them;
* amplification: bytes on disk per byte of event payload.

Usage: PYTHONPATH=src python bench/bench_recovery.py [--frames 10000] [--out ...]
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.tape import scanner
from blackbox.tape.writer import TapeWriter

sys.path.insert(0, str(ROOT / "bench"))
import _fs
from bench_frame import hardware

CHILD = ROOT / "tests" / "kill" / "_append_loop.py"

RSS_PROBE = """
import json, resource, sys, tempfile
sys.path.insert(0, "src")
from blackbox import constants as C
from blackbox.tape.writer import TapeWriter


def rss_fields():
    # C8 / C18: anon, file and shmem separately, plus the cgroup's memory limit, because a sandbox that
    # reclaims clean pages makes this number a property of the environment, not of the writer.
    out = {"ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                if key in ("RssAnon", "RssFile", "RssShmem", "VmRSS", "VmSwap"):
                    out[key] = int(value.split()[0])
    except OSError:
        out["status"] = "unavailable (no /proc)"
    try:
        with open("/sys/fs/cgroup/memory.max", encoding="ascii") as fh:
            out["cgroup_memory_max"] = fh.read().strip()
    except OSError:
        pass
    return out


frames, event_bytes = int(sys.argv[1]), int(sys.argv[2])
baseline = rss_fields()               # "above baseline" needs the baseline measured first
w = TapeWriter(tempfile.mkdtemp(), segment_bytes=C.DEFAULT_SEGMENT_BYTES).open()
payload = b"r" * event_bytes
for _ in range(frames):
    w.append_and_publish(C.KIND_MODEL_REQUEST, payload)
w.flush()
after = rss_fields()
peak_kib = after["ru_maxrss_kib"]
base_rss, after_rss = baseline.get("VmRSS", 0), after.get("VmRSS", peak_kib)
out = {"events": frames, "segment_bytes": C.DEFAULT_SEGMENT_BYTES,
       # C18: state the procedure, not just the delta
       "procedure": ("read /proc/self/status twice - before `open()` and after the last append - and report "
                     "the difference; since ADR-005 the segment is not mapped, so a shared file mapping's "
                     "RssFile accounting does not apply and the batch buffer (16 KiB + 1 MiB worst case) is "
                     "RssAnon"),
       "head_off_at_sampling": w.head.head_off,
       "bytes_written": w.head.head_off - C.SEGMENT_HEADER_BYTES,
       "segment_percent_written": round(100.0 * (w.head.head_off - C.SEGMENT_HEADER_BYTES)
                                        / C.DEFAULT_SEGMENT_BYTES, 2),
       "peak_kib": peak_kib, "peak_mib": round(peak_kib / 1024, 2),
       "above_baseline_mib": round((after_rss - base_rss) / 1024, 2),
       "rss_anon_delta_mib": round((after.get("RssAnon", 0) - baseline.get("RssAnon", 0)) / 1024, 2),
       "rss_file_delta_mib": round((after.get("RssFile", 0) - baseline.get("RssFile", 0)) / 1024, 2),
       "baseline": baseline, "after": after}
print(json.dumps(out))
w.abort()
"""

AMP_PROBE = """
import json, sys, tempfile
sys.path.insert(0, "src")
from blackbox import constants as C
from blackbox.tape.writer import TapeWriter
frames, event_bytes, batch = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
w = TapeWriter(tempfile.mkdtemp(), segment_bytes=64 << 20).open()
w.batch_bytes = batch
payload = b"r" * event_bytes
for _ in range(frames):
    w.emit(C.KIND_MODEL_REQUEST, payload)
w.flush()
committed = w.head.head_off - C.SEGMENT_HEADER_BYTES
seals = 0
w.close()
print(json.dumps({"batch_bytes": batch, "committed_frame_bytes": committed,
                  "payload_bytes": frames * event_bytes,
                  "ratio": round(committed / (frames * event_bytes), 3)}))
"""


def build_tape(frames: int, event_bytes: int, column: dict | None = None) -> tuple[str, str]:
    tmp = _fs.scratch(column, "bbx-bench-rec-") if column else tempfile.mkdtemp(prefix="bbx-bench-rec-")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.Popen([sys.executable, str(CHILD), tmp, "process", str(64 << 20), str(event_bytes), "0"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    tape = Path(tmp) / "tape"
    deadline = time.time() + 120
    uuid = None
    while time.time() < deadline:
        if tape.exists():
            dirs = [p for p in tape.iterdir() if p.is_dir()]
            if dirs:
                uuid = dirs[0].name
                try:
                    sc = scanner.scan(tmp, uuid)
                    if sc.last_seq >= frames:
                        break
                except Exception:
                    pass
        time.sleep(0.05)
    os.kill(proc.pid, 9)
    proc.wait(timeout=60)
    assert uuid is not None
    return tmp, uuid


def measure_column(column: dict, args) -> dict:
    tmp, uuid = build_tape(args.frames, args.event_bytes, column)
    times = []
    for _ in range(args.rounds):
        t0 = time.perf_counter()
        result = scanner.scan(tmp, uuid)
        times.append((time.perf_counter() - t0) * 1000)
    q = statistics.quantiles(times, n=100) if len(times) > 2 else [min(times)] * 99
    recovered = result.frame_count

    # amplification with the default 16 MiB segment: a 64 MiB segment would measure the segment size
    tmp2 = _fs.scratch(column, "bbx-bench-amp-")
    t0 = time.perf_counter()
    w = TapeWriter(tmp2, segment_bytes=C.DEFAULT_SEGMENT_BYTES, mode="process").open()
    payload = b"r" * args.event_bytes
    for _ in range(args.frames):
        w.append_and_publish(C.KIND_MODEL_REQUEST, payload)
    w.flush()
    append_s = time.perf_counter() - t0
    rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    committed = w.head.head_off - C.SEGMENT_HEADER_BYTES
    reserved = w.segment_bytes
    w.close()

    amp_runs = []
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "TMPDIR": column["dir"]}
    for batch in (0, 4096, 16384, 65536):
        proc = subprocess.run([sys.executable, "-c", AMP_PROBE, str(args.frames), str(args.event_bytes), str(batch)],
                              capture_output=True, text=True, env=env, timeout=300)
        if proc.returncode == 0:
            amp_runs.append(json.loads(proc.stdout.strip().splitlines()[-1]))
        else:
            amp_runs.append({"batch_bytes": batch, "error": proc.stderr.strip().splitlines()[-1][:300]})
    payload_bytes = args.frames * args.event_bytes
    _fs.release(tmp)
    _fs.release(tmp2)
    return {
        "filesystem": column["name"], "scratch": _fs.label(column["dir"]),
        "recovery": {"frames_on_tape": recovered, "requested_frames": args.frames, "rounds": args.rounds,
                     "p50_ms": round(q[49], 2), "p95_ms": round(q[94], 2), "p99_ms": round(q[98], 2),
                     "min_ms": round(min(times), 2), "max_ms": round(max(times), 2),
                     "per_10k_frames_ms": round(q[49] / max(1, recovered) * 10_000, 1)},
        "rss_this_process_kib": rss_kib,
        "amplification_by_batch": amp_runs,
        "amplification": {"payload_bytes": payload_bytes, "committed_frame_bytes": committed,
                          "ratio": round(committed / payload_bytes, 3) if payload_bytes else None,
                          "reserved_segment_bytes": reserved,
                          "append_events_per_s": round(args.frames / append_s, 1)},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=10_000)
    ap.add_argument("--event-bytes", type=int, default=256)
    ap.add_argument("--rounds", type=int, default=7)
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args(argv)

    # Order matters: Linux carries the parent's RSS high-water mark across fork+exec, so the write-only probe
    # has to run before this process allocates anything large (it reported the parent's peak otherwise).
    rss_children = {}
    for column in _fs.columns():
        child = subprocess.run([sys.executable, "-c", RSS_PROBE, str(args.frames), str(args.event_bytes)],
                               capture_output=True, text=True, timeout=300,
                               env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "TMPDIR": column["dir"]})
        rss_children[column["name"]] = (json.loads(child.stdout.strip().splitlines()[-1])
                                        if child.returncode == 0 else {"error": child.stderr[-400:]})

    columns = _fs.columns()
    if args.only:
        columns = [c for c in columns if c["name"] == args.only]
    out = {
        "date": time.strftime("%Y-%m-%d"), "probe": "recovery time, RSS, amplification",
        "gate_column": _fs.GATE_FS,
        "hardware": hardware(),
        "columns": {c["name"]: {"path": _fs.label(c["dir"]), "gate": c["gate"], "note": c["note"]} for c in columns},
        "rss_write_only_process": rss_children,
        "results": {c["name"]: measure_column(c, args) for c in columns},
    }
    path = args.out or str(ROOT / "bench" / "results" / f"{out['date']}-recovery.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    for name, block in out["results"].items():
        rec, amp = block["recovery"], block["amplification"]
        print(f"  [{name:6s}] recovery {rec['frames_on_tape']} frames: p50 {rec['p50_ms']} ms, "
              f"p99 {rec['p99_ms']} ms ({rec['per_10k_frames_ms']} ms/10k) · "
              f"amplification {amp['ratio']}x · {amp['append_events_per_s']:.0f} ev/s publish-per-event")
        print(f"  [{name:6s}] RSS write-only {out['rss_write_only_process'][name].get('peak_mib')} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
