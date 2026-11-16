"""P3B-03: the proxy rebind budget, measured on **watchdog-triggered** rebinds only.

§11.8 and P3B-03's done-when both say the launcher super&#118;ises the proxy as a thread and, if the listener dies, it is
**re-bound within 100 ms**. The subject of that sentence is not ``rebind()``; it is the listener coming back. So this
bench measures the quantity a child actually experiences:

    outage_ms = (listener serving again) - (listener stopped serving)

which is *detection* (the supervisor's tick, because a watchdog can only notice on a tick) **plus** the rebind itself.
`rebind_ms` is recorded beside it from `ProxyThread.rebinds`, so the two costs are visible separately and neither can be
quoted as the other. A measurement that reported only `rebind_ms` would be answering an easier question than the plan
asks.

Why every sample is kept: the earlier evidence for this row was a single wall-clock sample, and when a shared macOS
runner spent 107.9 ms on it the response was to quote a median of five — one of which was the watchdog's and four of
which were manual. That is a statistic about a different experiment (defect 40). This file keeps all of them and the
gate is per-sample, because a child does not get the median.

Two columns:

* **idle** — nothing else running. This is the machine we control.
* **loaded** — one busy process per CPU. A shared CI runner is at best this and at worst worse, so a bound that only
  survives the idle column is not a bound anybody can rely on.

Everything the row needs to be re-read later is written out: tick interval, poll interval used by the *measurement* (its
own granularity is part of the number), every sample, whether the port was preserved, and the hardware block. Nothing
here is a gate unless it is labelled `gate: true`.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import multiprocessing
import os
import platform
import random
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

from _stats import summary

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.supervisor import REBIND_BUDGET_MS, ProxyThread

POLL_S = 0.0005          # the measurement's own granularity; recorded in the JSON because it bounds the answer


def _table() -> Routes:
    return Routes([Route(name="openai", base="http://127.0.0.1:9", prefix="/openai"),
                   Route(name="anthropic", base="http://127.0.0.1:9", prefix="/anthropic")],
                  default="openai")


def _burn(stop: Any) -> None:
    """CPU contention, in its own process: a thread would hand the GIL back and measure nothing (see defect 40's family)."""
    value = 0
    while not stop.is_set():
        for _ in range(20000):
            value = (value * 1103515245 + 12345) & 0x7FFFFFFF


def _outage_once(proxy: ProxyThread) -> dict[str, Any]:
    """Break the listener and wait for the supervisor to bring it back. Returns one sample."""
    server = proxy.server
    loop = proxy._loop
    if server is None or loop is None:
        raise RuntimeError("the proxy thread is not running")
    port_before = proxy.port
    rebinds_before = len(proxy.rebinds)
    started = time.perf_counter_ns()
    asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
    deadline = time.monotonic() + 5.0
    restored = False
    while time.monotonic() < deadline:
        current = proxy.server
        if (current is not None and current is not server and current.is_serving
                and len(proxy.rebinds) > rebinds_before):
            restored = True
            break
        time.sleep(POLL_S)
    ended = time.perf_counter_ns()
    rebind_ms = proxy.rebinds[rebinds_before] if len(proxy.rebinds) > rebinds_before else None
    return {
        "outage_ms": round((ended - started) / 1_000_000, 3),
        "rebind_ms": None if rebind_ms is None else round(rebind_ms, 3),
        "restored": restored,
        "port_before": port_before,
        "port_after": proxy.port,
        "port_kept": proxy.port == port_before,
    }


def _column(trials: int, *, supervise_ms: float, loaded: bool, seed: int = 20261116) -> dict[str, Any]:
    stoppers: list[Any] = []
    busy: list[multiprocessing.Process] = []
    if loaded:
        for _ in range(max(1, os.cpu_count() or 1)):
            stop = multiprocessing.Event()
            process = multiprocessing.Process(target=_burn, args=(stop,), daemon=True)
            process.start()
            stoppers.append(stop)
            busy.append(process)
    samples: list[dict[str, Any]] = []
    proxy = ProxyThread(_table(), supervise_ms=supervise_ms)
    rng = random.Random(seed)
    try:
        proxy.start()
        time.sleep(0.05)                        # let the watchdog arm before the first break
        for _ in range(trials):
            # Phase matters: a listener that dies just after a tick waits a whole tick, one that dies just before a
            # tick waits almost nothing. Back-to-back trials lock onto the tick and every sample becomes the worst
            # case (the first run of this bench read 50.35..51.88 ms with n=40, which is a maximum, not a
            # distribution). Sleeping a random fraction of a tick samples the whole space; the max over these samples
            # is the number the budget must survive.
            time.sleep(rng.uniform(0.0, supervise_ms / 1000.0))
            samples.append(_outage_once(proxy))
    finally:
        with contextlib.suppress(Exception):
            proxy.stop()
        for stop in stoppers:
            stop.set()
        for process in busy:
            process.join(5)
    outages = [s["outage_ms"] for s in samples if s["restored"]]
    rebinds = [s["rebind_ms"] for s in samples if s["rebind_ms"] is not None]
    not_restored = [s for s in samples if not s["restored"]]
    moved = [s for s in samples if not s["port_kept"]]
    detection = [round(o - r, 3) for o, r in zip(outages, rebinds, strict=False) if r is not None]
    over = [o for o in outages if o > REBIND_BUDGET_MS]
    return {
        "supervise_ms": supervise_ms,
        "phase_seed": seed,
        "loaded": loaded,
        "n": len(samples),
        "outage_ms": summary(outages),
        "rebind_ms": summary(rebinds),
        "detection_ms": summary(detection),
        "not_restored": len(not_restored),
        "port_moved": len(moved),
        "samples": samples,
        "budget_ms": REBIND_BUDGET_MS,
        "over_budget": {"count": len(over), "worst_ms": max(over) if over else None},
        "pass": bool(outages) and not over and not not_restored and not moved,
    }


def run(trials: int, *, supervise_ms: float = 20.0) -> dict[str, Any]:
    idle = _column(trials, supervise_ms=supervise_ms, loaded=False)
    loaded = _column(trials, supervise_ms=supervise_ms, loaded=True)
    gate = {
        "watchdog_rebind_ms": {
            "n": idle["n"] + loaded["n"],
            "budget": REBIND_BUDGET_MS,
            "idle_worst_ms": idle["outage_ms"].get("max"),
            "loaded_worst_ms": loaded["outage_ms"].get("max"),
            "over_budget_samples": idle["over_budget"]["count"] + loaded["over_budget"]["count"],
            # Per-sample, both columns. The requirement is about a child waiting for a listener, and a child does not
            # get the median of a distribution that happens to look better.
            "pass": idle["pass"] and loaded["pass"],
        }
    }
    return {
        "gate": gate,
        "idle": idle,
        "loaded": loaded,
        "method": {
            "outage_ms": "from the listener being closed to a new listener serving on the same port; includes the "
                         "supervisor tick, because a watchdog can only notice on a tick",
            "rebind_ms": "ProxyThread.rebinds: inside rebind(), excluding detection",
            "detection_ms": "outage_ms - rebind_ms, the tick's contribution",
            "phase": "each trial sleeps a seeded random fraction of a tick before breaking the listener, so the "
                     "samples cover the whole detection phase instead of locking onto the worst case",
            "poll_s": POLL_S,
            "loaded": "one busy process per CPU, in separate interpreters",
        },
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60, help="watchdog-triggered breaks per column")
    ap.add_argument("--supervise-ms", type=float, default=20.0)
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / f"{time.strftime('%Y-%m-%d')}-rebind.json"))
    args = ap.parse_args(argv)
    started = time.time()
    report = run(args.n, supervise_ms=args.supervise_ms)
    report["hardware"] = {"cpu_count": os.cpu_count(), "python": platform.python_version(),
                          "implementation": platform.python_implementation(), "os": platform.platform(),
                          "machine": platform.machine(), "n": args.n, "pid": os.getpid(),
                          "topology": "one interpreter hosting the proxy thread and the measuring client; the loaded "
                                      "column adds one busy process per CPU, because a shared runner is the condition "
                                      "the budget has to survive"}
    report["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report["wall_s"] = round(time.time() - started, 3)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for column in ("idle", "loaded"):
        block = report[column]
        print(f"{column:7s} outage {json.dumps(block['outage_ms'])}  rebind {json.dumps(block['rebind_ms'])}")
        print(f"{'':7s} over-budget {block['over_budget']}  port_moved {block['port_moved']}  "
              f"not_restored {block['not_restored']}")
    verdict = report["gate"]["watchdog_rebind_ms"]
    print(f"{'gate':7s} {'PASS' if verdict['pass'] else 'FAIL'}  {json.dumps({k: v for k, v in verdict.items() if k != 'pass'})}")
    print(f"wrote {args.out}")
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
