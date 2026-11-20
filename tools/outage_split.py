#!/usr/bin/env python3
"""Split a rebind-outage sample into the part that was ours and the part that was not.

Defect 108 is one Windows cell that failed the 100 ms rebind budget with a 686.2 ms outlier. The failure message carried
the numbers; the *decision* the numbers support was never written down, because the job log was unreadable from the build
sandbox (GitHub redirects logs to a host it cannot reach) and a rerun passed, which proves nothing either way.

This tool takes a sample — the outages a probe measured and the product's own record of what it did in each one — and
splits every sample into four shares:

* **rebind** — the product's own work, `rebinds[i]`, measured inside the supervisor;
* **dispatch** — the harness's cross-thread hop, `dispatch[i]`, measured by the test that asked for the close;
* **tick** — how late the watchdog tick woke (`wake_late[i]`), which is time the process spent not running;
* **unexplained** — the remainder, which is the part no clock in the product accounts for.

The classification is deliberately crude and stated as a rule, not as a diagnosis:

| share of the budget dominated by | label |
|---|---|
| the rebind | `ours:rebind` |
| tick lateness plus unexplained | `not-ours:tick-or-loop` |
| neither, and the sample is inside the budget | `inside-budget` |
| both over 20 % | `mixed` |

It is a *rule for reading a failure*, not a verdict about a cause. A sample labelled `not-ours:tick-or-loop` says that
the time was spent before (or outside) the code that rebinds; it cannot say whether the runner was starved, the loop was
parked, or the machine swapped. What it does say, and what the recorded defect-108 sample shows, is that the rebind was
not what spent the budget.

    python tools/outage_split.py --sample bench/results/2026-11-20-defect-108-split.json
    python tools/outage_split.py --outages 20.6,18.5,19.3,15.0,12.3,9.1,686.2,20.1 --rebinds 0,0,0,16,0,0,0,15 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BUDGET_MS = 100.0
DOMINANT = 0.20            # a share of the budget that counts as "this is where the time went"


def _round(values: list[float], digits: int = 3) -> list[float]:
    return [round(float(value), digits) for value in values]


def split_one(outage_ms: float, *, rebind_ms: float | None, dispatch_ms: float | None = None,
              wake_late_ms: float | None = None, budget_ms: float = BUDGET_MS) -> dict:
    """One sample, split. Every share is clamped at zero: a clock may over-report by a rounding error, not by a story."""
    rebind = max(0.0, float(rebind_ms or 0.0))
    dispatch = max(0.0, float(dispatch_ms or 0.0))
    tick = max(0.0, float(wake_late_ms or 0.0))
    outage = max(0.0, float(outage_ms))
    explained = rebind + dispatch + tick
    unexplained = max(0.0, outage - explained)
    over = outage > budget_ms
    if not over:
        label = "inside-budget"
    elif rebind / budget_ms >= 0.5:
        label = "ours:rebind"
    elif (tick + unexplained) / budget_ms >= DOMINANT and (tick + unexplained) / max(outage, 1e-9) >= 0.5:
        label = "not-ours:tick-or-loop"
    elif rebind > 0 and (tick + unexplained) > 0:
        label = "mixed"
    else:
        label = "not-ours:tick-or-loop"
    return {
        "outage_ms": round(outage, 3),
        "rebind_ms": round(rebind, 3),
        "dispatch_ms": round(dispatch, 3),
        "wake_late_ms": round(tick, 3),
        "unexplained_ms": round(unexplained, 3),
        "over_budget": over,
        "rebind_share_of_outage": round(rebind / outage, 4) if outage else 0.0,
        "label": label,
    }


def split(outages: list[float], rebinds: list[float] | None = None, dispatch: list[float] | None = None,
          wake_late: list[float] | None = None, budget_ms: float = BUDGET_MS) -> dict:
    rebinds = list(rebinds or [])
    dispatch = list(dispatch or [])
    wake_late = list(wake_late or [])
    samples = []
    for index, outage in enumerate(outages):
        samples.append(split_one(
            outage,
            rebind_ms=rebinds[index] if index < len(rebinds) else None,
            dispatch_ms=dispatch[index] if index < len(dispatch) else None,
            wake_late_ms=wake_late[index] if index < len(wake_late) else None,
            budget_ms=budget_ms,
        ))
    worst = max(samples, key=lambda s: s["outage_ms"]) if samples else None
    over = [s for s in samples if s["over_budget"]]
    return {
        "schema": "blackbox.outage-split/1",
        "budget_ms": budget_ms,
        "n": len(samples),
        "samples": samples,
        "worst": worst,
        "over_budget": len(over),
        "labels": sorted({s["label"] for s in samples}),
        "verdict": (
            "no sample is over the budget" if not over
            else f"{len(over)} of {len(samples)} sample(s) over {budget_ms:.0f} ms; "
                 f"the worst put {worst['rebind_share_of_outage'] * 100:.1f} % of its outage in the rebind"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="attribute a rebind outage to the rebind or to everything else")
    parser.add_argument("--sample", type=Path, help="a JSON file with outages/rebinds (or a split, to re-print it)")
    parser.add_argument("--outages", help="comma-separated per-sample outage in ms")
    parser.add_argument("--rebinds", help="comma-separated per-sample rebind in ms")
    parser.add_argument("--dispatch", help="comma-separated per-sample dispatch in ms")
    parser.add_argument("--wake-late", help="comma-separated per-sample tick lateness in ms")
    parser.add_argument("--budget", type=float, default=BUDGET_MS)
    parser.add_argument("--out", type=Path, help="write the split here as JSON")
    parser.add_argument("--json", action="store_true", help="print the split as JSON")
    args = parser.parse_args(argv)

    def numbers(text: str | None) -> list[float]:
        return [float(part) for part in (text or "").split(",") if part.strip()]

    if args.sample:
        payload = json.loads(args.sample.read_text())
        result = payload if payload.get("schema") == "blackbox.outage-split/1" else split(
            numbers(",".join(str(v) for v in payload["outages_ms"])),
            numbers(",".join(str(v) for v in payload.get("rebinds_ms", []))),
            numbers(",".join(str(v) for v in payload.get("dispatch_ms", []))) if payload.get("dispatch_ms") else None,
            numbers(",".join(str(v) for v in payload["wake_late_ms"])) if payload.get("wake_late_ms") else None,
            payload.get("budget_ms", args.budget),
        )
    else:
        outages = numbers(args.outages)
        if not outages:
            parser.error("give --sample or --outages")
        result = split(outages, numbers(args.rebinds), numbers(args.dispatch), numbers(args.wake_late), args.budget)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"budget {result['budget_ms']:.0f} ms · {result['n']} sample(s)")
        for index, sample in enumerate(result["samples"]):
            print(f"  {index}: outage {sample['outage_ms']:8.3f} · rebind {sample['rebind_ms']:7.3f} "
                  f"({sample['rebind_share_of_outage'] * 100:5.1f} %) · unexplained {sample['unexplained_ms']:8.3f} "
                  f"· {sample['label']}")
        print(f"  {result['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
