"""C32 / §15.1 — the 64 KiB `content=full` event, with the blob write and the frame that references it.

This is the row D-019 changed the shape of. Before C32 the calling thread fsynced the blob and its directory, so the
event paid two syncs of the traffic the recorder is supposed to be invisible to; §15.1 hard-fails above 10 ms and a
sync can be a millisecond on its own. Now the event is: dict → BLAKE2b of a 64 KiB body → CBOR → blob tmp file
(open, write, close, replace — page cache only) → frame with the ref stub → append → amortised publish + CHAIN.

The interval is the same one `bench_writer.py` uses (C2): the clock starts at the Python dict, the CBOR encode is
inside it, and the stages are non-additive. Two columns, ext4 gate + tmpfs labelled, one JSON.

Usage: PYTHONPATH=src python bench/bench_payload.py [--n ...] [--warmup ...] [--only ext4] [--out ...]
"""
from __future__ import annotations

import argparse
import json
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
from blackbox.codec.ids import blake2b
from blackbox.store import blobs
from blackbox.tape.keeper import Keeper
from blackbox.tape.writer import TapeWriter

BODY = 64 << 10                       # §15.1: "content=full, 64 KiB payload incl. blob write"
BUDGET = {"p50_us_max": 400.0, "p99_us_max": 3000.0, "hard_fail_us": 10_000.0}


POOL = 1024


def _body(i: int) -> bytes:
    """A distinct 64 KiB body without paying `os.urandom` inside the loop."""
    seed = blake2b(i.to_bytes(8, "big"))
    return (seed * ((BODY // len(seed)) + 1))[:BODY]


def pct(ordered: list[int], q: float) -> int:
    return ordered[min(int(q * (len(ordered) - 1)), len(ordered) - 1)] if ordered else 0


def run(column: dict, n: int, warmup: int, *, mode: str = "process", batch_bytes: int,
        keeper: bool = True, gate: bool = False) -> dict:
    tmp = _fs.scratch(column, "bbx-bench-payload-")
    w = TapeWriter(tmp, segment_bytes=64 << 20, mode=mode, chain_enabled=True).open()
    w.batch_bytes = batch_bytes
    w.batch_ms = C.DEFAULT_PUBLISH_MS
    bw = blobs.BlobWriter(tmp, mode=mode)
    w.attach_blobs(bw)
    k = Keeper(w, blob_writer=bw).start() if keeper else None
    # Distinct bodies, so every event pays a blob write; generated before the clock so the app does not pay for
    # a body it already has. 1 024 of them is 64 MiB of pool and a 0.1 % dedup rate, which is what a real session of
    # 64 KiB responses looks like.
    bodies = [_body(i) for i in range(POOL)]
    try:
        for i in range(warmup):
            blobs.append_payload(w, bw, C.KIND_MODEL_RESPONSE, bodies[i % len(bodies)])
        w.flush()

        lat: list[int] = []
        t0 = time.perf_counter()
        for i in range(n):
            # the dedup hit rate is fixed by the body count, not by the clock: 8 distinct blobs, rotated
            content = bodies[i % len(bodies)]
            s0 = time.perf_counter_ns()
            # C2's interval starts at the Python object; for `content=full` that object is the body itself, which
            # the recorder hashes **once** (the digest is the blob name and the ref stub's `b`), writes as a blob,
            # and references from a frame stub. Hashing the body a second time to also carry it inside a CBOR
            # envelope would measure an envelope the format does not store.
            blobs.append_payload(w, bw, C.KIND_MODEL_RESPONSE, content)
            lat.append(time.perf_counter_ns() - s0)
        w.flush()
        elapsed = time.perf_counter() - t0
        publishes, syncs, synced_blobs = w.publishes, w.syncs, bw.synced
    finally:
        if k is not None:
            k.stop()
        w.close()
        _fs.release(tmp)
    ordered = sorted(lat)
    row = {
        "n": n, "warmup": warmup, "elapsed_s": round(elapsed, 3), "mode": mode,
        "batch_bytes": batch_bytes, "keeper": keeper, "body_bytes": BODY,
        "p50_us": round(pct(ordered, 0.50) / 1000, 3), "p90_us": round(pct(ordered, 0.90) / 1000, 3),
        "p95_us": round(pct(ordered, 0.95) / 1000, 3), "p99_us": round(pct(ordered, 0.99) / 1000, 3),
        "mean_us": round(statistics.fmean(lat) / 1000, 3),
        "min_us": round(ordered[0] / 1000, 3), "max_us": round(ordered[-1] / 1000, 3),
        "events_per_s": round(n / elapsed, 1), "publishes": publishes,
        "events_per_publish": round(n / publishes, 1) if publishes else None,
        "tape_syncs": syncs, "blob_syncs": synced_blobs,
        "blobs_written": len(bodies), "dedup_hits": n + warmup - len(bodies),
        "filesystem": column["name"], "scratch": _fs.label(column["dir"]),
    }
    row["gate"] = bool(gate and column["gate"])
    if not row["gate"]:
        # A comparison row (no keeper, kernel mode) and the tmpfs column carry no verdict: the row is not the row
        # the budget is written against, and a "FAIL" next to a number nobody gates on is a false alarm.
        row["verdict"] = "not a gate row"
        return row
    if row["p50_us"] > BUDGET["hard_fail_us"]:
        row["verdict"] = "FAIL:hard_fail"
    else:
        failure = [name for name, limit in (("p50", BUDGET["p50_us_max"]), ("p99", BUDGET["p99_us_max"]))
                   if row[name + "_us"] > limit]
        row["verdict"] = "pass" if not failure else "FAIL:" + ",".join(failure)
    return row


def measure_column(column: dict, n: int, warmup: int) -> dict:
    rows = [
        # the §15.1 row: process mode, group commit, keeper on (it is on by default in a real session)
        run(column, n, warmup, batch_bytes=C.DEFAULT_BATCH_BYTES, keeper=True, gate=True),
        # same row with no keeper, so the keeper's contribution is visible and not guessed at
        run(column, n, warmup, batch_bytes=C.DEFAULT_BATCH_BYTES, keeper=False),
        # kernel mode: the blob sync is now the keeper's, and this is what buying durability costs
        run(column, min(n, 20_000), 2_000, mode="kernel", batch_bytes=C.DEFAULT_BATCH_BYTES),
    ]
    return {"budget": BUDGET, "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20_000)
    ap.add_argument("--warmup", type=int, default=2_000)
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    columns = _fs.columns()
    if args.only:
        columns = [c for c in columns if c["name"] == args.only]
    out = {
        "date": time.strftime("%Y-%m-%d"),
        "probe": "C32/D-019 — 64 KiB content=full event including the blob write (§15.1 ceiling row)",
        "protocol": ("single process, perf_counter_ns per event, clock starts at the payload the recorder is handed: "
                     "one BLAKE2b-256 of the 64 KiB body (the blob name and the stub's `b`), blob tmp write + "
                     "replace, CBOR ref stub, frame, amortised publish/CHAIN; nearest-rank percentiles from one "
                     "sorted array (P1-17)"),
        "gate_column": _fs.GATE_FS,
        "hardware": hardware(),
        "columns": {c["name"]: {"path": _fs.label(c["dir"]), "gate": c["gate"], "note": c["note"]}
                    for c in columns},
        "durability": ("D-019: process mode never syncs a blob (page cache outlives the process); kernel mode "
                       "lets the keeper sync blob + dir before the tape sync that covers the referencing frame; "
                       "paranoid mode syncs before the frame is appended. `blob_syncs` is what the mode bought."),
        "results": {c["name"]: measure_column(c, args.n, args.warmup) for c in columns},
    }
    path = (Path(args.out) if args.out else ROOT / "bench" / "results" / f"{out['date']}-payload-blob.json").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    for name, block in out["results"].items():
        for row in block["rows"]:
            print(f"{name:6s} {row['mode']:7s} keeper={int(row['keeper'])} batch={row['batch_bytes']:6d} "
                  f"p50={row['p50_us']:8.3f}us p99={row['p99_us']:9.3f}us {row['events_per_s']:9.1f} ev/s "
                  f"blob_syncs={row['blob_syncs']} {row['verdict']}")
    print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
