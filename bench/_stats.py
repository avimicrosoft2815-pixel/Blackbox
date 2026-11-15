"""Nearest-rank percentiles, shared by every bench (C16's lesson, applied everywhere by C37).

`statistics.quantiles(times, n=100)` interpolates between observed values and, with few samples, returns numbers
*outside* the sample range in a way that is easy to miss: `bench_recovery.py` published `p99_ms 122.28` next to
`max_ms 121.93`, and the tmpfs column published `p99 138.96 > max 125.95`. A p99 above the maximum is impossible, and
it sat in a committed results file that STATUS quoted.

The rule here is one rule for every bench file: nearest rank on one sorted array, so

    min <= p50 <= p95 <= p99 <= max

holds by construction, and `n` is printed beside the percentiles because a "p99" of seven samples *is* the maximum —
which is fine as long as the row says so.
"""
from __future__ import annotations

import math


def nearest_rank(ordered: list[float], q: float) -> float:
    """The observed value at ceil(q * n) rank, never interpolated and never outside the sample."""
    if not ordered:
        return 0.0
    rank = max(1, min(len(ordered), math.ceil(q * len(ordered))))
    return ordered[rank - 1]


def summary(samples: list[float], *, digits: int = 3, unit: str = "") -> dict:
    """`{n, min, p50, p95, p99, max, mean}` from one sorted pass. Monotone by construction, same input -> same output."""
    ordered = sorted(samples)
    if not ordered:
        return {"n": 0}
    out = {
        "n": len(ordered),
        "min": round(ordered[0], digits),
        "p50": round(nearest_rank(ordered, 0.50), digits),
        "p95": round(nearest_rank(ordered, 0.95), digits),
        "p99": round(nearest_rank(ordered, 0.99), digits),
        "max": round(ordered[-1], digits),
        "mean": round(sum(ordered) / len(ordered), digits),
    }
    if unit:
        out["unit"] = unit
    # A small-sample warning travels with the number instead of being remembered by the reader.
    if len(ordered) < 20:
        out["note"] = f"n={len(ordered)}: p95/p99 are observed maxima at this sample size, not distribution tails"
    return out
