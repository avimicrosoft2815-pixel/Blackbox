"""Watchdog rebind latency, measured on recoveries the watchdog actually performed (P3B-03, D-033, defect 40).

The number this publishes is not a median. It is every sample from real watchdog-triggered recoveries, with both halves
of the latency kept apart:

* `rebind_ms`   — the restart itself, the operation the plan budgets at `REBIND_BUDGET_MS` (100 ms);
* `watchdog_ms` — from the watchdog waking and finding the listener down to service returning (the health check and the
                  restart). This is what a child experiences once the death has been noticed;
* `end_to_end_ms` — from the test closing the listener to service returning. It *includes* the tick that noticed the
                  death, so its floor is the tick: a listener that dies a microsecond after a tick waits a full tick.

The gate is `max(every sample)`, because that is the only statistic that survives a bad day. A median over manual
rebinds is what hid the 107.9 ms macOS sample in defect 40; averaging an outlier away is how a budget stops being one.

Usage:
    PYTHONPATH=src python tools/bench_watchdog.py                       # 20 samples per tick, ticks 10 and 50 ms
    PYTHONPATH=src python tools/bench_watchdog.py --samples 5 --ticks 10 --out /tmp/w.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.supervisor import REBIND_BUDGET_MS, ProxyThread


def _table() -> Routes:
    return Routes([Route(name="openai", base="http://127.0.0.1:9", prefix="/openai")], default="openai")


def _machine() -> dict:
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        cpu = ""
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or cpu,
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }


def measure(tick_ms: float, samples: int, warmup: int = 2) -> dict:
    """Kill the listener `samples` times and let the watchdog repair it; return every raw sample."""
    end_to_end: list[float] = []
    with ProxyThread(_table(), supervise_ms=tick_ms) as proxy:
        port = proxy.port
        loop = proxy._loop
        if loop is None:
            raise RuntimeError("the proxy thread did not start")
        for index in range(samples + warmup):
            server = proxy.server
            if server is None or not server.is_serving:
                raise RuntimeError("the listener was not serving before the kill")
            closed_at = time.perf_counter()
            asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
            deadline = time.monotonic() + 5.0
            restored = False
            while time.monotonic() < deadline:
                current = proxy.server
                if current is not None and current is not server and current.is_serving:
                    restored = True
                    break
                time.sleep(0.001)
            if not restored:
                raise RuntimeError("the watchdog did not restore service within 5 s")
            if proxy.port != port:
                raise RuntimeError("the watchdog restored service on a different port")
            if index >= warmup:                       # warm-up washes the first-touch page faults out
                end_to_end.append((time.perf_counter() - closed_at) * 1000.0)
        evidence = proxy.watchdog_stats()
    recoveries = evidence["recoveries"][warmup:]
    return {
        "tick_ms": tick_ms,
        "samples": samples,
        "warmup": warmup,
        "rebind_ms": [round(r["rebind_ms"], 3) for r in recoveries],
        "watchdog_ms": [round(r["watchdog_ms"], 3) for r in recoveries],
        "end_to_end_ms": [round(value, 3) for value in end_to_end],
        "errors": evidence["errors"],
        "port_changed": evidence["port_changed"],
        "stats": {name: _summary(values) for name, values in
                  (("rebind_ms", [r["rebind_ms"] for r in recoveries]),
                   ("watchdog_ms", [r["watchdog_ms"] for r in recoveries]),
                   ("end_to_end_ms", end_to_end))},
    }


def idle_cost(tick_ms: float, seconds: float = 2.0) -> dict:
    """What the tick costs while nothing is wrong: CPU seconds burned by a proxy thread idling for `seconds`.

    A 10 ms tick is 100 wakes a second, which sounds like a lot until it is measured. `supervise_ms=0` disables the
    watchdog and is the baseline, so the difference is the tick's own cost and not the proxy's.
    """
    with ProxyThread(_table(), supervise_ms=tick_ms) as proxy:
        if proxy.server is None:
            raise RuntimeError("the proxy thread did not start")
        started, cpu_started = time.perf_counter(), time.process_time()
        time.sleep(seconds)
        return {
            "tick_ms": tick_ms,
            "wall_s": round(time.perf_counter() - started, 3),
            "cpu_s": round(time.process_time() - cpu_started, 4),
            "cpu_percent": round(100.0 * (time.process_time() - cpu_started) / seconds, 3),
        }


def _summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(statistics.median(ordered), 3),
        "p99": round(ordered[min(len(ordered) - 1, max(0, round(0.99 * (len(ordered) - 1))))], 3),
        "max": round(ordered[-1], 3),
        "mean": round(statistics.fmean(ordered), 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=int, default=20, help="watchdog-triggered recoveries per tick")
    parser.add_argument("--ticks", default="10,50", help="comma-separated watchdog intervals in ms")
    parser.add_argument("--out", default=str(ROOT / "bench" / "results" / "2026-11-16-watchdog.json"))
    args = parser.parse_args(argv)

    runs = {}
    for tick in [float(part) for part in args.ticks.split(",") if part.strip()]:
        result = measure(tick, args.samples)
        runs[f"tick_{tick:g}ms"] = result
        print(f"tick {tick:g} ms: rebind max {result['stats']['rebind_ms']['max']:.2f} ms, "
              f"wake-to-service max {result['stats']['watchdog_ms']['max']:.2f} ms, "
              f"end-to-end max {result['stats']['end_to_end_ms']['max']:.2f} ms")

    idle = [idle_cost(tick) for tick in (0.0, 10.0, 50.0)]
    every_rebind = [value for run in runs.values() for value in run["rebind_ms"]]
    every_watchdog = [value for run in runs.values() for value in run["watchdog_ms"]]
    document = {
        "what": "watchdog-triggered listener rebind latency",
        "requirement": {
            "source": "plan P3B-03 / D-033: 'proxy thread supervision + rebind <= 100 ms'",
            "budget_ms": REBIND_BUDGET_MS,
            "statistic": "max of every watchdog-triggered sample (no median, no percentile)",
            "met": bool(every_rebind) and max(every_rebind) <= REBIND_BUDGET_MS
                   and max(every_watchdog) <= REBIND_BUDGET_MS,
            "rebind_max_ms": round(max(every_rebind), 3) if every_rebind else None,
            "watchdog_max_ms": round(max(every_watchdog), 3) if every_watchdog else None,
        },
        "detection_floor": "a recovery cannot be noticed sooner than one watchdog tick, so end-to-end recovery is "
                           "bounded below by tick_ms; the budget applies to the work the watchdog does once it looks",
        "method": "for each sample: close the live listener from outside the watchdog thread, wait for a different "
                  "listener object to report is_serving, keep the sample. No sleep-tuned thresholds, no retries "
                  "counted as one sample",
        "command": "PYTHONPATH=src python tools/bench_watchdog.py",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": _machine(),
        "filesystem": "not applicable: the measured path is a loopback socket, no disk I/O",
        "runs": runs,
        "idle_cost": idle,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0 if document["requirement"]["met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
