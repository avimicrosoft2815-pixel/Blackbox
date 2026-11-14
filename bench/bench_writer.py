"""P1-08 / §15.1 — writer throughput and latency for a full event.

A "full event" here is: CBOR-encode the fixed event payload (content=hash: the 1 KiB body is hashed, not
inlined), append the frame, and commit it with a CHAIN frame — the §15.1 row the gate is written against.

Usage: PYTHONPATH=src python bench/bench_writer.py [--n ...] [--out ...]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C          # noqa: E402
from blackbox.codec import cbor               # noqa: E402
from blackbox.codec.ids import blake2b        # noqa: E402
from blackbox.tape.writer import TapeWriter   # noqa: E402

sys.path.insert(0, str(ROOT / "bench"))
from bench_frame import hardware              # noqa: E402


def event_payload(content: bytes) -> bytes:
    """content=hash: the body is replaced by its digest, so the frame stays small whatever the body size."""
    return cbor.encode({"p": "openai", "m": "gpt-4o-mini", "pt": 3, "ph": blake2b(content),
                        "st": 1, "te": 2, "phn": len(content)})


def run(n: int, warmup: int, body: bytes, batch_bytes: int, chain: bool, full: bool = False) -> dict:
    tmp = tempfile.mkdtemp(prefix="bbx-bench-writer-")
    w = TapeWriter(tmp, segment_bytes=256 << 20, mode="process", chain_enabled=chain).open()
    w.batch_bytes = batch_bytes
    payload = event_payload(body)
    for _ in range(warmup):
        w.emit(C.KIND_MODEL_REQUEST, payload if not full else event_payload(body))
    w.flush()

    lat: list[int] = []
    t0 = time.perf_counter()
    for _ in range(n):
        s = time.perf_counter_ns()
        w.emit(C.KIND_MODEL_REQUEST, payload if not full else event_payload(body))
        lat.append(time.perf_counter_ns() - s)
    w.flush()
    elapsed = time.perf_counter() - t0
    committed = w.head.head_seq
    w.close()
    q = statistics.quantiles(lat, n=100)
    return {
        "n": n, "warmup": warmup, "body_bytes": len(body), "payload_bytes": len(payload),
        "batch_bytes": batch_bytes, "chain": chain, "full_event": full,
        "p50_us": round(q[49] / 1000, 3), "p95_us": round(q[94] / 1000, 3), "p99_us": round(q[98] / 1000, 3),
        "mean_us": round(statistics.fmean(lat) / 1000, 3),
        "events_per_s": round(n / elapsed, 1), "committed_seq": committed,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200_000)
    ap.add_argument("--warmup", type=int, default=20_000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    body = os.urandom(1024)                     # 1 KiB content, hashed
    results = [
        run(20_000, 2_000, body, 0, True, full=True),      # §15.1 gate, publish per event
        run(200_000, 20_000, body, 4096, True, full=True),  # §15.1 gate, group commit (plan §8.2)
        run(20_000, 2_000, body, 0, True),           # publish per event, chain on
        run(20_000, 2_000, body, 0, False),          # publish per event, chain off
        run(args.n, args.warmup, body, 4096, True),  # group commit, chain on
        run(args.n, args.warmup, body, 4096, False),  # group commit, chain off
    ]
    out = {"date": time.strftime("%Y-%m-%d"), "probe": "P1-08 writer throughput",
           "protocol": "single process, tmpfs, per-event perf_counter_ns, content=hash 1 KiB",
           "hardware": hardware(), "results": results}
    path = args.out or str(ROOT / "bench" / "results" / f"{out['date']}-writer-throughput.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    for r in results:
        print(f"  full={str(r['full_event']):5s} batch={r['batch_bytes']:5d} chain={str(r['chain']):5s} p50={r['p50_us']:8.3f}us "
              f"p99={r['p99_us']:9.3f}us  {r['events_per_s']:>10.0f} ev/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
