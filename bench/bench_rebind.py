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
sys.path.insert(0, str(ROOT / "tools"))

from _stats import summary
from rebind_probe import ServingProbe, measure_outage

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.supervisor import REBIND_BUDGET_MS, ProxyThread

POLL_S = 0.0005          # the old in-process poll; kept in the JSON so an older file can still be read


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


def _outage_once(proxy: ProxyThread, probe: ServingProbe, *, notify: bool = True) -> dict[str, Any]:
    """Break the listener and measure the outage **a request** experiences. Returns one sample.

    `notify=False` removes the listener's own notice before closing it, so the loss is the kind nothing announces (an
    OS-level death, a socket pulled from under the loop): the watchdog tick is then the only detector, and the column
    measures the backstop rather than the fast path.

    The clock is `tools/rebind_probe.py`: a real request in a tight loop on the port the child was told, from the first
    refusal to the first answer. It is the only definition that covers the interval inside `close()` before the coroutine
    returns — the socket stops accepting before the caller is woken, and an in-process clock cannot see that (defect 60's
    second revision; `tests/test_rebind_probe.py` makes the interval 60 ms wide and asserts the clock contains it).
    """
    server = proxy.server
    loop = proxy._loop
    if server is None or loop is None:
        raise RuntimeError("the proxy thread is not running")
    if not notify:
        server.lost_cb = None
    port_before = proxy.port
    rebinds_before = len(proxy.rebinds)
    noticed_before = len(proxy.noticed_ms)
    try:
        sampled = measure_outage(probe, lambda: asyncio.run_coroutine_threadsafe(server.close(), loop).result(5))
    except RuntimeError as exc:
        return {"outage_ms": None, "outage_upper_ms": None, "restored": False, "error": str(exc),
                "rebind_ms": None, "noticed_ms": None, "port_before": port_before, "port_after": proxy.port,
                "port_kept": proxy.port == port_before, "refused_requests": 0, "observed_failure": False,
                "probe_cycle_ms": probe.cycle_ms()}
    rebind_ms = proxy.rebinds[rebinds_before] if len(proxy.rebinds) > rebinds_before else None
    noticed = proxy.noticed_ms[len(proxy.noticed_ms) - 1] if len(proxy.noticed_ms) > noticed_before else None
    return {
        "outage_ms": round(float(sampled["outage_ms"]), 3),
        "outage_upper_ms": round(float(sampled["outage_upper_ms"]), 3),
        "rebind_ms": None if rebind_ms is None else round(rebind_ms, 3),
        "noticed_ms": None if noticed is None else round(noticed, 3),
        "restored": True,
        "port_before": port_before,
        "port_after": proxy.port,
        "port_kept": proxy.port == port_before,
        "refused_requests": int(sampled["refused_requests"]),
        "observed_failure": bool(sampled["observed_failure"]),
        "probe_cycle_ms": round(float(sampled["probe_cycle_ms"]), 4),
        "probe_answer_timeout_ms": round(float(sampled["probe_answer_timeout_ms"]), 3),
    }


def _column(trials: int, *, supervise_ms: float, loaded: bool, notify: bool = True,
            seed: int = 20261116) -> dict[str, Any]:
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
    probe: ServingProbe | None = None
    rng = random.Random(seed)
    try:
        proxy.start()
        probe = ServingProbe(proxy.port).start()
        time.sleep(0.05)                        # let the watchdog arm before the first break
        for _ in range(trials):
            # Phase matters: a listener that dies just after a tick waits a whole tick, one that dies just before a
            # tick waits almost nothing. Back-to-back trials lock onto the tick and every sample becomes the worst
            # case (the first run of this bench read 50.35..51.88 ms with n=40, which is a maximum, not a
            # distribution). Sleeping a random fraction of a tick samples the whole space; the max over these samples
            # is the number the budget must survive.
            time.sleep(rng.uniform(0.0, supervise_ms / 1000.0))
            samples.append(_outage_once(proxy, probe, notify=notify))
    finally:
        if probe is not None:
            with contextlib.suppress(Exception):
                probe.stop()
        with contextlib.suppress(Exception):
            proxy.stop()
        for stop in stoppers:
            stop.set()
        for process in busy:
            process.join(5)
    outages = [s["outage_ms"] for s in samples if s["restored"] and s["outage_ms"] is not None]
    uppers = [s["outage_upper_ms"] for s in samples if s["restored"] and s["outage_upper_ms"] is not None]
    rebinds = [s["rebind_ms"] for s in samples if s["rebind_ms"] is not None]
    noticed = [s["noticed_ms"] for s in samples if s["noticed_ms"] is not None]
    not_restored = [s for s in samples if not s["restored"]]
    moved = [s for s in samples if not s["port_kept"]]
    detection = [round(o - r, 3) for o, r in zip(outages, rebinds, strict=False) if r is not None]
    # The gate is on the conservative bound: it covers the whole gap even when no request happened to be refused.
    over = [o for o in uppers if o > REBIND_BUDGET_MS]
    return {
        "supervise_ms": supervise_ms,
        "phase_seed": seed,
        "loaded": loaded,
        "detection": "listener-notice" if notify else "watchdog-tick",
        "n": len(samples),
        "outage_ms": summary(outages),
        "outage_upper_ms": summary(uppers),
        "probe_cycle_ms": summary([s["probe_cycle_ms"] for s in samples if s.get("probe_cycle_ms") is not None]),
        "probe_answer_timeout_ms": max((s["probe_answer_timeout_ms"] for s in samples
                                        if s.get("probe_answer_timeout_ms") is not None), default=None),
        "refused_requests": sum(int(s.get("refused_requests") or 0) for s in samples),
        "rebind_ms": summary(rebinds),
        # Two independent views of detection, reported side by side and never merged: the external one (outage minus
        # rebind, defined for every sample) and the supervisor's own (notify -> noticed, defined only for a listener
        # that said it was going), with its own sample count so an absent number is visible as absent.
        "detection_ms": summary(detection),
        "noticed_ms": summary(noticed),
        "noticed_n": len(noticed),
        "not_restored": len(not_restored),
        "port_moved": len(moved),
        "samples": samples,
        "budget_ms": REBIND_BUDGET_MS,
        "over_budget": {"count": len(over), "worst_ms": max(over) if over else None},
        "pass": bool(outages) and not over and not not_restored and not moved,
    }


def run(trials: int, *, supervise_ms: float = 20.0) -> dict[str, Any]:
    """Four columns: the two conditions (idle, loaded) crossed with the two detectors (notice, tick).

    The point of the cross is that they measure different things and neither may be quoted as the other: a listener
    that says it is going is noticed in microseconds, while one that dies silently still waits for a tick. Both are
    real, and a bound that only covers the first is a bound on a lucky death.
    """
    columns = {
        "notice_idle": _column(trials, supervise_ms=supervise_ms, loaded=False, notify=True),
        "notice_loaded": _column(trials, supervise_ms=supervise_ms, loaded=True, notify=True),
        "tick_idle": _column(trials, supervise_ms=supervise_ms, loaded=False, notify=False),
        "tick_loaded": _column(trials, supervise_ms=supervise_ms, loaded=True, notify=False),
    }
    total = sum(column["n"] for column in columns.values())
    over = sum(column["over_budget"]["count"] for column in columns.values())
    worst = max(column["outage_upper_ms"].get("max") or 0.0 for column in columns.values())
    gate = {
        "watchdog_rebind_ms": {
            "n": total,
            "budget": REBIND_BUDGET_MS,
            # The gate is the end-to-end outage, per sample, in all four columns — the number a child experiences.
            # Nothing here relaxes it; the columns only say which part of it was detection and which was the rebind.
            "worst_outage_upper_ms": worst,
            "by_condition": {name: column["outage_upper_ms"].get("max") for name, column in columns.items()},
            "over_budget_samples": over,
            "pass": over == 0 and all(column["not_restored"] == 0 and column["port_moved"] == 0
                                      for column in columns.values()),
        }
    }
    return {
        "gate": gate,
        **columns,
        "method": {
            "outage_ms": "measured by a real request on the child's port (tools/rebind_probe.py): from the first "
                         "request that was not served to the first one that was. It covers the interval inside "
                         "close() before the coroutine returns, which an in-process clock cannot see (defect 60)",
            "outage_upper_ms": "the conservative bound: from the last request served before the break to the first "
                               "served after it. The gate is on this one; it is never smaller than outage_ms",
            "probe_cycle_ms": "resolution of the request probe (median served request). Every outage number is "
                              "bounded by it, so it is recorded beside them",
            "probe_answer_timeout_ms": "the answer cap, measured on the machine that ran this file (calibrate): 3x "
                                       "its median answered request, never below 10 ms and never above the 50 ms "
                                       "attempt budget. A request accepted and never answered is charged at most this, "
                                       "so it is the resolution of the outage tail, and it is recorded",
            "rebind_ms": "ProxyThread.rebinds: inside rebind(), excluding detection and excluding the requests' queueing",
            "detection_ms": "outage_ms - rebind_ms, every sample: what was not the rebind",
            "noticed_ms": "the supervisor's own detection time (lost_cb -> watchdog saw it); defined only when the "
                          "listener notified, so `noticed_n` may be smaller than `n`",
            "detection": "`listener-notice` closes a listener that still has its notice attached (the fast path); "
                         "`watchdog-tick` removes the notice first, so the tick is the only detector (the backstop)",
            "bound": "the 100 ms budget is a gate on the observed samples of the machine that ran this file. It is "
                     "NOT a claim that a rebind is inside 100 ms on any machine: a thread the OS does not schedule "
                     "cannot be bounded by this or any sleeping-thread design (defect 58)",
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
    for name in ("notice_idle", "notice_loaded", "tick_idle", "tick_loaded"):
        block = report[name]
        print(f"{name:14s} outage {json.dumps(block['outage_ms'])}")
        print(f"{'':14s} rebind {json.dumps(block['rebind_ms'])}  detection {json.dumps(block['detection_ms'])}")
        print(f"{'':14s} noticed {json.dumps(block['noticed_ms'])} (n={block['noticed_n']})  "
              f"over-budget {block['over_budget']['count']}  port_moved {block['port_moved']}  "
              f"not_restored {block['not_restored']}")
    verdict = report["gate"]["watchdog_rebind_ms"]
    print(f"{'gate':7s} {'PASS' if verdict['pass'] else 'FAIL'}  {json.dumps({k: v for k, v in verdict.items() if k != 'pass'})}")
    print(f"wrote {args.out}")
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
