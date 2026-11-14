"""P1-17 — per-stage microbench with paired toggling.

This is the single measurement protocol for every §15.1 / §15.1a budget row from here on:
N >= 100 000, 10 000 warm-up, p50/p95/p99, hardware metadata, one JSON per run.

Usage: PYTHONPATH=src python bench/bench_frame.py [--n 100000] [--warmup 10000] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

from blackbox import constants as C                      # noqa: E402
from blackbox.codec import cbor                          # noqa: E402
from blackbox.codec.crc32 import crc32                  # noqa: E402
from blackbox.codec.ids import blake2b                   # noqa: E402
from blackbox.tape import chain as chain_mod             # noqa: E402
from blackbox.tape import frame as frame_mod             # noqa: E402
from blackbox.tape import head as head_mod               # noqa: E402
from blackbox.tape.writer import TapeWriter              # noqa: E402
import _fs                                              # noqa: E402


def hardware() -> dict:
    cpu = "unknown"
    try:
        for line in open("/proc/cpuinfo", encoding="utf-8", errors="replace"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    facts = {}
    try:
        for line in open("/proc/meminfo", encoding="utf-8"):
            if line.startswith("MemTotal"):
                facts["mem_total_kib"] = int(line.split()[1])
                break
    except OSError:
        pass
    return {
        "cpu": cpu, "cpu_count": os.cpu_count(), "python": platform.python_version(),
        "implementation": platform.python_implementation(), "os": platform.platform(),
        "machine": platform.machine(), **facts,
    }


def stats(samples: list[int]) -> dict:
    q = statistics.quantiles(samples, n=100)
    return {
        "p50_us": round(q[49] / 1000, 3), "p95_us": round(q[94] / 1000, 3),
        "p99_us": round(q[98] / 1000, 3), "mean_us": round(statistics.fmean(samples) / 1000, 3),
        "min_us": round(min(samples) / 1000, 3), "max_us": round(max(samples) / 1000, 3),
    }


def measure(fn, n: int, warmup: int) -> dict:
    for _ in range(warmup):
        fn()
    samples: list[int] = []
    pc = time.perf_counter_ns
    for _ in range(n):
        t0 = pc()
        fn()
        samples.append(pc() - t0)
    out = stats(samples)
    out.update({"n": n, "warmup": warmup})
    return out


EVENT = {
    "p": "openai", "m": "gpt-4o-mini", "pt": 3, "ph": b"\x01" * 32, "fd": 2500,
    "rf": b"\x02" * 16, "st": 1, "te": 2, "pm": {"temperature": 0.2, "max_tokens": 256},
}


def measure_column(column: dict, args) -> dict:
    """Every stage, measured twice: once on the real disk (gate) and once on tmpfs (labelled column)."""
    payload_1k = b"x" * 1024
    ev_bytes = cbor.encode(EVENT)
    frame_1k = frame_mod.encode(1, C.KIND_MODEL_REQUEST, payload_1k, priority=0)
    chain_payload = chain_mod.encode_payload(prev=b"\x00" * 32, batch=blake2b(b"y" * 1024),
                                             first_seq=1, last_seq=9)

    tmp = _fs.scratch(column, "bbx-bench-frame-")
    nc_tmp = _fs.scratch(column, "bbx-bench-frame-nc-")
    writer = TapeWriter(tmp, segment_bytes=64 << 20, mode="process").open()
    writer_nc = TapeWriter(nc_tmp, segment_bytes=64 << 20, mode="process", chain_enabled=False).open()

    seg_mm = writer._mm                     # noqa: SLF001 - the bench measures the real mapping
    hmap = head_mod.HeadMap(os.path.join(tmp, "head_bench"))

    slot = head_mod.HeadSlot(gen=1, head_off=C.SEGMENT_HEADER_BYTES)

    def publish_once() -> None:
        slot.head_off = C.SEGMENT_HEADER_BYTES + 1024
        hmap.publish(slot)          # gen += 1 happens inside; no object is allocated per publish

    stages: dict[str, tuple[str, object]] = {
        "crc32_1kib": ("CRC-32 over 1 KiB", lambda: crc32(payload_1k)),
        "crc32_44b": ("CRC-32 over a 44 B frame header", lambda: crc32(frame_1k[:44])),
        "blake2b_1kib": ("BLAKE2b-256 over 1 KiB (batch hash)", lambda: blake2b(payload_1k)),
        "cbor_encode_event": ("CBOR encode the fixed event payload", lambda: cbor.encode(EVENT)),
        "cbor_decode_event": ("CBOR decode the fixed event payload", lambda: cbor.decode_exact(ev_bytes)),
        "frame_encode_1kib": ("frame encode, 1 KiB payload", lambda: frame_mod.encode(1, 2, payload_1k)),
        "frame_encode_90b": ("frame encode, 26 B payload", lambda: frame_mod.encode(1, 2, ev_bytes)),
        "chain_frame_encode": ("CHAIN payload + frame encode", lambda: frame_mod.encode(
            1, C.KIND_CHAIN, chain_payload, priority=0)),
        "memcpy_mmap_1kib": ("slice-copy 1 KiB into the segment mapping",
                             lambda: seg_mm.__setitem__(slice(4096, 5120), payload_1k)),
        "head_publish": ("head pack_into + CRC (one publish)", publish_once),
        "append_1kib_no_publish": ("writer.append, 1 KiB payload", lambda: writer.append(
            C.KIND_MODEL_REQUEST, payload_1k, priority=0)),
        "event_append_publish": ("writer.append_and_publish, 26 B payload (chain on)",
                                 lambda: writer.append_and_publish(C.KIND_MODEL_REQUEST, ev_bytes)),
        "event_append_publish_nochain": ("writer.append_and_publish, 26 B payload (chain off)",
                                         lambda: writer_nc.append_and_publish(C.KIND_MODEL_REQUEST, ev_bytes)),
        "event_emit_chain_off": ("writer.emit with no publish (enqueue only)",
                                 lambda: writer_nc.append(C.KIND_MODEL_REQUEST, ev_bytes)),
    }
    results = {}
    for name, (label, fn) in stages.items():
        results[name] = {"label": label, **measure(fn, args.n, args.warmup)}

    # paired toggling: the same shape with one component switched off, so the difference is attributable
    pairs = [
        {"pair": "publish_cost", "with": "event_append_publish", "without": "event_emit_chain_off",
         "note": "append+chain+publish vs enqueue only"},
        {"pair": "chain_cost", "with": "event_append_publish", "without": "event_append_publish_nochain",
         "note": "CHAIN frame + blake2b + payload encode per publish"},
        {"pair": "crc_vs_payload", "with": "frame_encode_1kib", "without": "memcpy_mmap_1kib",
         "note": "encode allocates header+payload+trailer; the memcpy is the unavoidable part"},
    ]
    for pair in pairs:
        a, b = results[pair["with"]]["p50_us"], results[pair["without"]]["p50_us"]
        pair["delta_p50_us"] = round(a - b, 3)
        pair["ratio"] = round(a / b, 2) if b else None

    writer.close()
    writer_nc.close()
    hmap.close()
    _fs.release(tmp)
    _fs.release(nc_tmp)
    return {"stages": results, "pairs": pairs, "filesystem": column["name"], "scratch": column["dir"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100_000)
    ap.add_argument("--warmup", type=int, default=10_000)
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args(argv)

    columns = _fs.columns()
    if args.only:
        columns = [c for c in columns if c["name"] == args.only]
    out = {
        "date": time.strftime("%Y-%m-%d"),
        "probe": "P1-17 frame/append stages",
        "protocol": f"N={args.n}, warm-up={args.warmup}, per-call perf_counter_ns, single process",
        "gate_column": _fs.GATE_FS,
        "hardware": hardware(),
        "workload": {"payload_1kib": 1024, "event_payload_bytes": len(cbor.encode(EVENT)),
                     "frame_1kib_bytes": len(frame_mod.encode(1, C.KIND_MODEL_REQUEST, b"x" * 1024, priority=0))},
        "columns": {c["name"]: {"path": c["dir"], "gate": c["gate"], "note": c["note"]} for c in columns},
        "results": {},
    }
    for c in columns:
        out["results"][c["name"]] = measure_column(c, args)
    path = args.out or str(ROOT / "bench" / "results" / f"{out['date']}-frame.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    for fs_name, block in out["results"].items():
        for name, r in block["stages"].items():
            print(f"  [{fs_name:6s}] {name:28s} p50 {r['p50_us']:8.3f} us   p99 {r['p99_us']:9.3f} us")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
