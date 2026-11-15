"""The four §15.1 capture rows that had no number, measured rather than asserted.

`docs/STATUS.md` said, in as many words, that these rows were *not measured*. A budget row with no measurement is a
target, and this file is the difference:

| row (§15.1) | what is timed | target (p50 / p99) | hard fail |
|---|---|---|---|
| Disabled (`BLACKBOX_DISABLED=1`) | overhead per decorated call, against the same function undecorated | ≤ 1 / ≤ 3 µs | > 5 µs |
| `content=off` | one 1 KiB read enqueued (redaction + frame decision, no body kept) | ≤ 8 / ≤ 40 µs | > 100 µs |
| `content=hash`, 1 KiB | one read as a full event: redact, digest, header, frame | ≤ 25 / ≤ 250 µs | > 1 ms |
| provider call latency | Δ p95 of a ~700 ms streamed call, recorded minus bare | ≤ 3 % | > 10 % |
| `install()` | wall time to open a tape and start a keeper | ≤ 25 ms | > 150 ms |

Two honest notes about the protocol, because a benchmark that hides them is a marketing number:

* the enqueue rows use an **in-memory emitter**: they measure the recorder's own work, not the disk. The row that
  includes durability is §15.1's 64 KiB `content=full` row, measured on ext4 in `bench/bench_payload.py`.
* the latency row is end-to-end over loopback against the mock provider, with a real tape on the gate filesystem in
  the recorded case, so a page fault or a flush shows up in the p95 where a user would feel it.

Usage: PYTHONPATH=src python bench/bench_capture.py [--n ...] [--calls ...] [--out ...]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

import _fs
import _stats
from bench_frame import hardware

from blackbox import api
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.cap_sdk.preflight import choose
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink

READ = 1024                                  # §15.1's "1 KiB payload"
SSE = b"data: {\"choices\":[{\"delta\":{\"content\":\"x\"}}]}\n\n"          # ordinary streamed output
SSE_KEYWORD = b'data: {"token": "abc", "email": "a@b.test"}\n\n'                  # the same size, with the words
# that make the two expensive patterns run: this is the honest worst case, not the gate row.
BUDGET = {
    "disabled_call": {"p50_us_max": 1.0, "p99_us_max": 3.0, "hard_fail_us": 5.0},
    "enqueue_off": {"p50_us_max": 8.0, "p99_us_max": 40.0, "hard_fail_us": 100.0},
    "enqueue_hash": {"p50_us_max": 25.0, "p99_us_max": 250.0, "hard_fail_us": 1000.0},
    "enqueue_hash_keyword": {"p50_us_max": None, "p99_us_max": None, "hard_fail_us": 1000.0},
    "provider_latency": {"p95_delta_pct_max": 3.0, "hard_fail_pct": 10.0},
    "startup": {"p50_ms_max": 25.0, "p99_ms_max": 25.0, "hard_fail_ms": 150.0},
}


def _percentiles(samples_ns: list[int], divisor: float) -> dict:
    """µs (or ms) percentiles from one sorted array, through the shared nearest-rank definition (C37)."""
    return _stats.summary([value / divisor for value in samples_ns], digits=3)


def measure_disabled(n: int, warmup: int) -> dict:
    """§15.1 "Disabled": the decorator must cost less than a `clock_gettime` on a fast machine.

    The comparison is against the *same* function undecorated in the same process, because a bare `lambda: None` and
    a decorated one differ by two things (the wrapper and the recorder's disabled check) and only one of them is ours.
    """
    os.environ["BLACKBOX_DISABLED"] = "1"
    try:
        # No `install()` at all: disabled means no tape, and the row is about what the *decorator* costs when the
        # recorder is not there. `install()` under the same variable is measured in the startup row's opposite case.
        @api.step("noop")
        def decorated() -> int:
            return 1

        def bare() -> int:
            return 1

        for _ in range(warmup):
            decorated()
            bare()
        dec_ns: list[int] = []
        bare_ns: list[int] = []
        for _ in range(n):
            start = time.perf_counter_ns()
            bare()
            middle = time.perf_counter_ns()
            decorated()
            end = time.perf_counter_ns()
            bare_ns.append(middle - start)
            dec_ns.append(end - middle)
        overhead = [max(0, a - b) for a, b in zip(dec_ns, bare_ns, strict=True)]
        return {"n": n, "warmup": warmup, "bare": _percentiles(bare_ns, 1000.0),
                "decorated": _percentiles(dec_ns, 1000.0), "overhead_us": _percentiles(overhead, 1000.0),
                "disabled": api.disabled(), "budget": BUDGET["disabled_call"], "unit": "us"}
    finally:
        api.uninstall()
        os.environ.pop("BLACKBOX_DISABLED", None)


def measure_enqueue(content: str, n: int, warmup: int, body_source: bytes) -> dict:
    """One read through `SinkRecorder.receive` with an in-memory emitter, so the number is the recorder's own work."""
    frames = [0]

    def emit(kind: int, payload: bytes, **_fields: object) -> None:
        frames[0] += 1

    sink = TapeSink(emit, content=content, coalesce_bytes=8 << 20)   # one group: no flush inside the timed region
    recorder = SinkRecorder(sink)
    call = recorder.open_call("bench", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
    body = (body_source * 40)[:READ]
    for _ in range(warmup):
        recorder.receive(call, body)
    samples: list[int] = []
    for _ in range(n):
        start = time.perf_counter_ns()
        recorder.receive(call, body)
        samples.append(time.perf_counter_ns() - start)
    recorder.close_call(call, status=200)
    key = "enqueue_off" if content == "off" else (
        "enqueue_hash_keyword" if body_source is SSE_KEYWORD else "enqueue_hash")
    return {"n": n, "warmup": warmup, "read_bytes": len(body), "content": content,
            "body_class": "sse-with-keywords" if body_source is SSE_KEYWORD else "ordinary-sse",
            "us": _percentiles(samples, 1000.0), "budget": BUDGET[key], "frames_written": frames[0], "unit": "us"}


def measure_provider(calls: int, column: dict) -> dict:
    """Δ p95 of a ~700 ms streamed call: bare against recorded, same provider, same scenario, alternating."""
    scenario = Scenario(chunks=14, delay_ms=50, chunk_text="token ")      # 14 × 50 ms = 700 ms of stream
    bare: list[int] = []
    recorded: list[int] = []
    scratch = _fs.scratch(column, "bbx-bench-capture-")
    with MockProvider(scenario) as provider:
        import httpx

        for index in range(calls):
            if index % 2 == 0:
                start = time.perf_counter_ns()
                with httpx.Client(base_url=provider.base_url, timeout=30) as client, \
                        client.stream("POST", "/v1/chat/completions",
                                      json={"model": "mock-1", "stream": True}) as response:
                    for _ in response.iter_bytes():
                        pass
                bare.append(time.perf_counter_ns() - start)
            else:
                data_dir = os.path.join(scratch, f"call{index}")
                api.install(data_dir=data_dir, publish_ms=0.0)
                try:
                    recorder = SinkRecorder(TapeSink(api.emit))
                    handle = choose(recorder)
                    start = time.perf_counter_ns()
                    with httpx.Client(base_url=provider.base_url, timeout=30) as client, \
                            client.stream("POST", "/v1/chat/completions",
                                          json={"model": "mock-1", "stream": True}) as response:
                        for _ in response.iter_bytes():
                            pass
                    recorded.append(time.perf_counter_ns() - start)
                    if handle.handle is not None:
                        handle.handle.uninstall()
                finally:
                    api.uninstall()

    bare_ms = _stats.summary([value / 1e6 for value in bare], digits=2)
    recorded_ms = _stats.summary([value / 1e6 for value in recorded], digits=2)
    delta = 100.0 * (recorded_ms["p95"] - bare_ms["p95"]) / bare_ms["p95"] if bare_ms["p95"] else 0.0
    return {"calls": calls, "bare_ms": bare_ms, "recorded_ms": recorded_ms, "delta_p95_pct": round(delta, 2),
            "budget": BUDGET["provider_latency"], "unit": "ms"}


def measure_startup(column: dict, n: int) -> dict:
    """`install()` from nothing: open the tape, write RUN_START, start the keeper — the cost a CLI pays at launch."""
    scratch = _fs.scratch(column, "bbx-bench-startup-")
    samples: list[int] = []
    for index in range(n):
        data_dir = os.path.join(scratch, f"install{index}")
        start = time.perf_counter_ns()
        status = api.install(data_dir=data_dir, publish_ms=0.0)
        samples.append(time.perf_counter_ns() - start)
        assert status.get("enabled"), status
        api.uninstall()
    return {"n": n, "ms": _percentiles(samples, 1e6), "budget": BUDGET["startup"], "unit": "ms"}


def run_column(column: dict, args: argparse.Namespace) -> dict:
    return {
        "column": column["name"],
        "gate": column["gate"],
        "disabled": measure_disabled(args.n, args.warmup),
        "enqueue_off": measure_enqueue("off", args.n, args.warmup, SSE),
        "enqueue_hash": measure_enqueue("hash", args.n, args.warmup, SSE),
        "enqueue_hash_keyword": measure_enqueue("hash", args.n, args.warmup, SSE_KEYWORD),
        "provider_latency": measure_provider(args.calls, column),
        "startup": measure_startup(column, args.installs),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="capture overhead rows of §15.1")
    parser.add_argument("--n", type=int, default=5000)
    parser.add_argument("--warmup", type=int, default=500)
    parser.add_argument("--calls", type=int, default=40, help="streamed calls per column; half bare, half recorded")
    parser.add_argument("--installs", type=int, default=20)
    parser.add_argument("--only", default=None, help="ext4 | tmpfs")
    parser.add_argument("--out", default=str(ROOT / "bench" / "results" / f"{time.strftime('%Y-%m-%d')}-capture-overhead.json"))
    args = parser.parse_args(argv)

    results = {}
    for column in _fs.columns():
        if args.only and column["name"] != args.only:
            continue
        results[column["name"]] = run_column(column, args)
        print(f"[{column['name']}] done", file=sys.stderr)

    document = {
        "date": time.strftime("%Y-%m-%d"),
        "probe": "§15.1 capture rows: Disabled, content=off, content=hash 1 KiB, provider Δ p95, install()",
        "protocol": (
            "single process, perf_counter_ns per operation; enqueue rows use an in-memory emitter so the number is the "
            "recorder's own work (durability is the 64 KiB content=full row in bench_payload.py); the latency row is "
            "end-to-end over loopback against the mock provider, alternating bare and recorded calls of ~700 ms with "
            "a real tape in the recorded case; install() from nothing including RUN_START and the keeper; percentiles "
            "by nearest rank from one sorted array (P1-17), n printed beside every percentile"),
        "gate_column": _fs.GATE_FS,
        "budget_source": "docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md §15.1",
        "hardware": hardware(),
        "host": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                 "os": f"{platform.system()}-{platform.release()}"},
        "results": results,
    }
    Path(args.out).write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {_fs.label(args.out)}", file=sys.stderr)

    gate = results.get(_fs.GATE_FS)
    if gate:
        verdicts = {
            "disabled overhead p50": (gate["disabled"]["overhead_us"]["p50"],
                                      gate["disabled"]["budget"]["p50_us_max"]),
            "disabled overhead p99": (gate["disabled"]["overhead_us"]["p99"],
                                      gate["disabled"]["budget"]["p99_us_max"]),
            "content=off p50": (gate["enqueue_off"]["us"]["p50"], gate["enqueue_off"]["budget"]["p50_us_max"]),
            "content=off p99": (gate["enqueue_off"]["us"]["p99"], gate["enqueue_off"]["budget"]["p99_us_max"]),
            "content=hash p50": (gate["enqueue_hash"]["us"]["p50"], gate["enqueue_hash"]["budget"]["p50_us_max"]),
            "content=hash p99": (gate["enqueue_hash"]["us"]["p99"], gate["enqueue_hash"]["budget"]["p99_us_max"]),
            "hash worst-case p50": (gate["enqueue_hash_keyword"]["us"]["p50"],
                                    gate["enqueue_hash_keyword"]["budget"]["hard_fail_us"]),
            "provider Δ p95 %": (gate["provider_latency"]["delta_p95_pct"],
                                 gate["provider_latency"]["budget"]["p95_delta_pct_max"]),
            "install p50 ms": (gate["startup"]["ms"]["p50"], gate["startup"]["budget"]["p50_ms_max"]),
            "install p99 ms": (gate["startup"]["ms"]["p99"], gate["startup"]["budget"]["p99_ms_max"]),
        }
        for name, (value, limit) in verdicts.items():
            print(f"  {name:24s} {value:9.2f}  <= {limit:9.2f}  {'PASS' if value <= limit else 'FAIL'}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
