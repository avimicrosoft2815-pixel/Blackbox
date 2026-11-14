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

from blackbox import constants as C          # noqa: E402
from blackbox.tape import scanner            # noqa: E402
from blackbox.tape.writer import TapeWriter  # noqa: E402

sys.path.insert(0, str(ROOT / "bench"))
from bench_frame import hardware              # noqa: E402

CHILD = ROOT / "tests" / "kill" / "_append_loop.py"

RSS_PROBE = """
import json, resource, sys, tempfile
sys.path.insert(0, "src")
from blackbox import constants as C
from blackbox.tape.writer import TapeWriter
frames, event_bytes = int(sys.argv[1]), int(sys.argv[2])
w = TapeWriter(tempfile.mkdtemp(), segment_bytes=C.DEFAULT_SEGMENT_BYTES).open()
payload = b"r" * event_bytes
for _ in range(frames):
    w.append_and_publish(C.KIND_MODEL_REQUEST, payload)
w.flush()
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"peak_kib": rss, "peak_mib": round(rss / 1024, 2), "events": frames,
                  "segment_bytes_mapped": C.DEFAULT_SEGMENT_BYTES}))
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


def build_tape(frames: int, event_bytes: int) -> tuple[str, str]:
    tmp = tempfile.mkdtemp(prefix="bbx-bench-rec-")
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=10_000)
    ap.add_argument("--event-bytes", type=int, default=256)
    ap.add_argument("--rounds", type=int, default=7)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    # Order matters: Linux carries the parent's RSS high-water mark across fork+exec, so the write-only probe
    # has to run before this process allocates anything large (it reported the parent's peak otherwise).
    # RSS must come from a process that only writes: scanning 10k frames in this process would measure the
    # reader's memory instead (it holds every decoded frame dict).
    rss_child = subprocess.run([sys.executable, "-c", RSS_PROBE, str(args.frames), str(args.event_bytes)],
                               capture_output=True, text=True,
                               env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=300)
    rss = json.loads(rss_child.stdout.strip().splitlines()[-1]) if rss_child.returncode == 0 else {}

    tmp, uuid = build_tape(args.frames, args.event_bytes)
    times = []
    for _ in range(args.rounds):
        t0 = time.perf_counter()
        result = scanner.scan(tmp, uuid)
        times.append((time.perf_counter() - t0) * 1000)
    q = statistics.quantiles(times, n=100) if len(times) > 2 else [min(times)] * 99
    recovered = result.frame_count

    # amplification, publish-per-event then group commit, with the default 16 MiB segment: the mapping is the dominant term, so a 64 MiB
    # segment would measure the segment size rather than the writer
    tmp2 = tempfile.mkdtemp(prefix="bbx-bench-rss-")
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
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
    for batch in (0, 4096, 16384, 65536):
        proc = subprocess.run([sys.executable, "-c", AMP_PROBE, str(args.frames), str(args.event_bytes), str(batch)],
                              capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=300)
        if proc.returncode == 0:
            amp_runs.append(json.loads(proc.stdout.strip().splitlines()[-1]))
    payload_bytes = args.frames * args.event_bytes
    out = {
        "date": time.strftime("%Y-%m-%d"), "probe": "recovery time, RSS, amplification",
        "hardware": hardware(),
        "recovery": {"frames_on_tape": recovered, "requested_frames": args.frames, "rounds": args.rounds,
                     "p50_ms": round(q[49], 2), "p95_ms": round(q[94], 2), "p99_ms": round(q[98], 2),
                     "min_ms": round(min(times), 2), "max_ms": round(max(times), 2)},
        "rss_write_only_process": rss,
        "rss_this_process_kib": rss_kib,
        "amplification_by_batch": amp_runs,
        "amplification": {"payload_bytes": payload_bytes, "committed_frame_bytes": committed,
                          "ratio": round(committed / payload_bytes, 3) if payload_bytes else None,
                          "reserved_segment_bytes": reserved,
                          "append_events_per_s": round(args.frames / append_s, 1)},
    }
    path = args.out or str(ROOT / "bench" / "results" / f"{out['date']}-recovery.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    print(f"  recovery {recovered} frames: p50 {out['recovery']['p50_ms']} ms, "
          f"p99 {out['recovery']['p99_ms']} ms")
    print(f"  recovery normalised: {round(out['recovery']['p50_ms'] / max(1, recovered) * 10_000, 1)} ms per 10k frames")
    print(f"  RSS (write-only process) {out['rss_write_only_process'].get('peak_mib')} MiB · "
          f"amplification {out['amplification']['ratio']}x "
          f"(4 KiB {amp_runs[1]['ratio'] if len(amp_runs) > 1 else None}x, "
          f"16 KiB {amp_runs[2]['ratio'] if len(amp_runs) > 2 else None}x) · "
          f"{out['amplification']['append_events_per_s']:.0f} ev/s with a publish per event")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
