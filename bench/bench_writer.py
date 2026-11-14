"""P1-08 / §15.1 — writer throughput and latency for a full event.

The measured interval (C2): a Python `dict` holding one 1 KiB `content` field, `content=hash`
(BLAKE2b-256 of the body), CBOR encode, frame encode, append into the active segment, and the amortised
**publish + CHAIN** — that is the §15.1 row the gate is written against. Everything before redaction is
included; redaction is added at G-Capture (P3B), so this number is a writer-level **lower bound** for the
§15.1 row. The bench does *not* pre-encode: the CBOR encode happens inside the timed loop, and the stages are
non-additive (CBOR allocates, the frame reuses a mapping), which is why the stage table in
`bench/results/*-frame.json` does not sum to the event latency.

Kernel mode adds a group-commit `fdatasync`; its cost is reported separately as the **stall p99**, because a
sync lands on one event in a batch and would otherwise vanish into the average.

Usage: PYTHONPATH=src python bench/bench_writer.py [--n ...] [--out ...] [--columns]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

import _fs
from bench_frame import hardware

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.codec.ids import blake2b
from blackbox.tape.keeper import Keeper
from blackbox.tape.writer import TapeWriter


def event_dict(content: bytes) -> dict:
    """The §15.1 starting point: a Python dict with a 1 KiB `content` field."""
    return {"p": "openai", "m": "gpt-4o-mini", "pt": 3, "ch": blake2b(content), "phn": len(content),
            "st": 1, "te": 2}


def encode_event(ev: dict) -> bytes:
    return cbor.encode(ev)          # content=hash: the body itself is never in the frame


def run(column: dict, n: int, warmup: int, body: bytes, batch_bytes: int, chain: bool,
        mode: str = "process", publish_ms: float = C.DEFAULT_PUBLISH_MS, keeper: bool = False,
        sync_bytes: int = C.DEFAULT_SYNC_BYTES, sync_ms: int = C.DEFAULT_SYNC_MS) -> dict:
    tmp = _fs.scratch(column, "bbx-bench-writer-")
    w = TapeWriter(tmp, segment_bytes=64 << 20, mode=mode, chain_enabled=chain,
                   sync_ms=sync_ms, sync_bytes=sync_bytes).open()
    w.batch_bytes = batch_bytes
    w.batch_ms = publish_ms
    body_hash = blake2b(body)
    ev = {"p": "openai", "m": "gpt-4o-mini", "pt": 3, "ch": body_hash, "phn": len(body), "st": 1, "te": 2}
    k = Keeper(w).start() if keeper else None
    try:
        for _ in range(warmup):
            w.emit(C.KIND_MODEL_REQUEST, encode_event(ev))
        w.flush()

        lat: list[int] = []
        stall: list[int] = []
        t0 = time.perf_counter()
        for _ in range(n):
            payload = encode_event(ev)
            s0, syncs0 = time.perf_counter_ns(), w.syncs
            w.emit(C.KIND_MODEL_REQUEST, payload)
            dt = time.perf_counter_ns() - s0
            (stall if w.syncs > syncs0 else lat).append(dt)
            lat.append(dt)
        w.flush()
        elapsed = time.perf_counter() - t0
        committed = w.head.head_seq
        publishes, window_max = w.publishes, w.window_max_ms
    finally:
        if k is not None:
            k.stop()
        w.close()
        _fs.release(tmp)
    q = statistics.quantiles(lat, n=100)
    return {
        "n": n, "warmup": warmup, "body_bytes": len(body), "payload_bytes": len(encode_event(ev)),
        "batch_bytes": batch_bytes, "publish_ms": publish_ms, "chain": chain, "mode": mode,
        "keeper": keeper, "sync_ms": sync_ms, "sync_bytes": sync_bytes, "syncs": w.syncs,
        "p50_us": round(q[49] / 1000, 3), "p95_us": round(q[94] / 1000, 3), "p99_us": round(q[98] / 1000, 3),
        "mean_us": round(statistics.fmean(lat) / 1000, 3),
        "stall_n": len(stall),
        "stall_max_us": round(max(stall) / 1000, 3) if stall else None,
        "stall_p99_us": round(statistics.quantiles(stall, n=100)[98] / 1000, 3) if len(stall) > 99 else None,
        "events_per_s": round(n / elapsed, 1), "committed_seq": committed, "publishes": publishes,
        "events_per_publish": round(n / publishes, 1) if publishes else None,
        "window_max_ms": round(window_max, 3),
        "filesystem": column["name"], "scratch": _fs.label(column["dir"]),
    }


def paced(column: dict, rate: float, seconds: float, batch_bytes: int) -> dict:
    """C3: the commit window at an offered rate — 1 ev/s, 1 000 ev/s, and as fast as the loop goes."""
    tmp = _fs.scratch(column, "bbx-bench-writer-pace-")
    w = TapeWriter(tmp, segment_bytes=64 << 20, mode="process").open()
    w.batch_bytes = batch_bytes
    w.batch_ms = C.DEFAULT_PUBLISH_MS
    k = Keeper(w).start()
    payload = encode_event(event_dict(os.urandom(1024)))
    n = 0
    t_end = time.perf_counter() + seconds
    try:
        while time.perf_counter() < t_end:
            w.emit(C.KIND_MODEL_REQUEST, payload)
            n += 1
            if rate:
                target = time.perf_counter() + 1.0 / rate
                while time.perf_counter() < target:
                    time.sleep(0)
        w.flush()
        out = {"offered_ev_s": rate if rate else "unbounded", "seconds": seconds, "events": n,
               "actual_ev_s": round(n / seconds, 1), "publishes": w.publishes,
               "events_per_publish": round(n / w.publishes, 2) if w.publishes else None,
               "window_max_ms": round(w.window_max_ms, 3), "filesystem": column["name"]}
    finally:
        k.stop()
        w.close()
        _fs.release(tmp)
    return out


def measure_column(column: dict, args, body: bytes) -> dict:
    rows = [
        run(column, 20_000, 2_000, body, 0, True),                       # publish per event (§15.1 worst case)
        run(column, args.n, args.warmup, body, C.DEFAULT_BATCH_BYTES, True),   # §15.1 gate, group commit
        run(column, args.n, args.warmup, body, C.DEFAULT_BATCH_BYTES, True, keeper=True),
        run(column, args.n, args.warmup, body, C.DEFAULT_BATCH_BYTES, False),
        run(column, 20_000, 2_000, body, 0, False),
        run(column, args.n, args.warmup, body, C.DEFAULT_BATCH_BYTES, True,
            mode="kernel", keeper=True),                                  # kernel-mode row (C1)
        run(column, args.n, args.warmup, body, C.DEFAULT_BATCH_BYTES, True,
            mode="kernel", keeper=True, sync_bytes=64 << 10),             # sync-dense: enough syncs for a p99
    ]
    return {"rows": rows,
            "group_commit": [paced(column, 1, 5, C.DEFAULT_BATCH_BYTES),
                             paced(column, 1_000, 3, C.DEFAULT_BATCH_BYTES),
                             paced(column, 0, 3, C.DEFAULT_BATCH_BYTES)]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200_000)
    ap.add_argument("--warmup", type=int, default=20_000)
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", default=None, help="restrict to one filesystem column by name")
    args = ap.parse_args(argv)

    body = os.urandom(1024)
    columns = _fs.columns()
    if args.only:
        columns = [c for c in columns if c["name"] == args.only]
    out = {
        "date": time.strftime("%Y-%m-%d"),
        "probe": "P1-08/P1-17 writer throughput and group-commit window",
        "protocol": ("single process, per-event perf_counter_ns, content=hash of a 1 KiB body; "
                     "the timed interval includes the CBOR encode, the frame, the append and the amortised "
                     "publish+CHAIN (C2)"),
        "interval": ("writer-level, lower bound for the §15.1 row; redaction is added at G-Capture. "
                     "Stages are non-additive: the CBOR encode is inside the timed loop and the frame "
                     "writes into the mapping, so the stage table does not sum to this number."),
        "gate_column": _fs.GATE_FS,
        "hardware": hardware(),
        "columns": {c["name"]: {"path": _fs.label(c["dir"]), "gate": c["gate"], "note": c["note"]} for c in columns},
        "results": {},
    }
    for c in columns:
        out["results"][c["name"]] = measure_column(c, args, body)
    path = args.out or str(ROOT / "bench" / "results" / f"{out['date']}-writer-throughput.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    for fs_name, block in out["results"].items():
        for r in block["rows"]:
            print(f"  [{fs_name:6s}] chain={r['chain']!s:5s} batch={r['batch_bytes']:5d} mode={r['mode']:7s} "
                  f"p50={r['p50_us']:8.3f} p99={r['p99_us']:9.3f} stall_max={r['stall_max_us']} n={r['syncs']} "
                  f"{r['events_per_s']:>10.0f} ev/s  win={r['window_max_ms']} ms")
        for g in block["group_commit"]:
            print(f"  [{fs_name:6s}] rate={g['offered_ev_s']} ev/s win={g['window_max_ms']} ms "
                  f"ev/publish={g['events_per_publish']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
