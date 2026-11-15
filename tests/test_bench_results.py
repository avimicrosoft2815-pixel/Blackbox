"""C37 — every committed bench result is arithmetically possible, and says how many samples it has.

`bench/results/2026-11-14-recovery.json` published `p99_ms 122.28` beside `max_ms 121.93`, and `p99 138.96` beside
`max 125.95` on tmpfs: a p99 above the maximum cannot exist, and it sat in a file STATUS quotes. The cause was
`statistics.quantiles`, which interpolates between order statistics; the fix is one nearest-rank implementation
(`bench/_stats.py`) used by every bench file, and this test is what keeps a future bench honest.

Two rules, both cheap to state and both impossible to satisfy by accident:

* wherever a result object carries `min`/`p50`/`p95`/`p99`/`max`, they are monotone and inside the sample range;
* wherever it carries percentiles, it carries the sample size `n` — because a p99 of seven samples *is* the maximum,
  and the row has to say so rather than imply a distribution it never measured.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import _stats  # the bench directory is on sys.path above

RESULTS = ROOT / "bench" / "results"
KEYS = ("min", "p50", "p95", "p99", "max")


def _walk(node, path=""):
    """Yield (path, dict) for every dict in the document, so nested rows are checked too."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")


def _number(row: dict, key: str) -> float | None:
    for candidate in (key, f"{key}_us", f"{key}_ms"):
        if candidate in row and isinstance(row[candidate], (int, float)):
            return float(row[candidate])
    return None


def _result_files() -> list[Path]:
    return sorted(RESULTS.glob("*.json"))


def _active_results() -> list[tuple[Path, dict]]:
    """A file that declares itself superseded still has to say *why*; only then is it skipped by the range checks."""
    out = []
    for path in _result_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        if "superseded_by" in doc:
            assert doc.get("percentile_method"), f"{path.name} is superseded but never says what was wrong with it"
            continue
        out.append((path, doc))
    return out


def test_every_result_file_is_one_json_document() -> None:
    """`p1-09b-retention.json` is not (extra data after the first document), and nothing noticed for a week."""
    broken = []
    for path in _result_files():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            broken.append(f"{path.name}: {exc}")
    assert broken == [], broken


def test_percentiles_are_monotone_and_inside_the_sample() -> None:
    offenders = []
    for path, document in _active_results():
        for where, row in _walk(document):
            values = {key: _number(row, key) for key in KEYS}
            if sum(v is not None for v in values.values()) < 3:
                continue
            present = [(key, values[key]) for key in KEYS if values[key] is not None]
            for (a_key, a), (b_key, b) in itertools.pairwise(present):
                if a > b + 1e-9:
                    offenders.append(f"{path.name}{where}: {a_key}={a} > {b_key}={b}")
    assert offenders == [], "a percentile outside its own sample range: " + "; ".join(offenders)


def test_percentiles_come_with_a_sample_size() -> None:
    offenders = []
    for path, document in _active_results():
        for where, row in _walk(document):
            if _number(row, "p99") is None:
                continue
            has_n = any(k in row for k in ("n", "events", "rounds", "trials", "frames", "blocks"))
            if not has_n:
                offenders.append(f"{path.name}{where}")
    assert offenders == [], "percentiles without a sample size: " + "; ".join(offenders)


def test_the_shared_statistics_helper_is_monotone_and_never_extrapolates() -> None:
    for n in (1, 2, 3, 7, 30, 1000):
        samples = [float(i) for i in range(n)]
        out = _stats.summary(samples)
        assert out["min"] <= out["p50"] <= out["p95"] <= out["p99"] <= out["max"], (n, out)
        assert out["min"] == 0.0 and out["max"] == n - 1
        assert out["n"] == n
        if n < 20:
            assert "note" in out, f"{n} samples must carry the small-sample note"


def test_nearest_rank_is_the_observed_value() -> None:
    ordered = [10.0, 20.0, 30.0]
    assert _stats.nearest_rank(ordered, 0.5) == 20.0
    assert _stats.nearest_rank(ordered, 0.99) == 30.0        # ceil(0.99*3)=3 -> the maximum, as it must be
    assert _stats.nearest_rank(ordered, 0.0) == 10.0
