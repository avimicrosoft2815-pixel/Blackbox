"""Bench smoke against `docs/STATE.json` (C31, Part B §6).

The regression rule needs a *number with a provenance*: every baseline key names the JSON it came from and the
path inside it, and this script resolves that path rather than trusting a hand-copied figure. Three failures are
fatal, and each one is a defect that would otherwise pass silently:

* a key in `bench_baseline.keys` with no `sources` entry, or a source that does not resolve in its JSON — a
  baseline nobody can point at is a baseline nobody can check;
* a key this script does not know the *direction* of (a latency that may rise is not a rule);
* a fresh run worse than the baseline by more than `REGRESSION_LIMIT` in that direction.

Usage:
    python tools/bench_smoke.py                      # committed JSONs: consistency, 0 % change expected
    python tools/bench_smoke.py --json results.json   # fresh run: regression check
    python tools/bench_smoke.py --json bench/results/2026-11-14-writer-throughput.json \
                               --json bench/results/2026-11-14-recovery.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "docs" / "STATE.json"
REGRESSION_LIMIT = 0.10          # Part B §6: a smoke run may not be 10 % worse than the baseline

# Which way is worse. A key that is not here is a build failure, not a silent skip: a new row needs a decision.
WORSE_IF_HIGHER = {"full_event_p50_us", "full_event_p99_us", "recovery_ms_per_10k_frames",
                   "rss_above_baseline_mib", "amplification_16kib", "amplification_per_event",
                   "payload_64kib_p50_us", "payload_64kib_p99_us"}
WORSE_IF_LOWER = {"events_per_s"}


def _item(container, token: str):
    """One selector step: `name`, `name[key=value]` (first array element whose key equals value)."""
    if "[" not in token:
        return container[token]
    name, rest = token.split("[", 1)
    key, wanted = rest.rstrip("]").split("=", 1)
    for element in container[name]:
        value = element.get(key)
        if str(value) == wanted or (isinstance(value, bool) and str(value).lower() == wanted.lower()):
            return element
    raise KeyError(f"{name}[{key}={wanted}] matched no element")


def resolve(document, selector: str):
    """Resolve a dotted selector against a parsed JSON document. Raises KeyError with the failing step."""
    current = document
    for token in selector.split("."):
        current = _item(current, token)
    return current


def load_state(path: Path | None = None) -> dict:
    return json.loads((path or STATE).read_text(encoding="utf-8"))


def check(state: dict, documents: dict[str, dict] | None = None,
          *, limit: float = REGRESSION_LIMIT) -> list[str]:
    """Every problem the smoke run found, as sentences a reviewer can act on. Empty list = pass."""
    problems: list[str] = []
    baseline = state.get("bench_baseline") or {}
    keys = baseline.get("keys") or {}
    sources = baseline.get("sources") or {}
    if not keys:
        return ["bench_baseline.keys is empty: the smoke run would compare against nothing"]
    docs = dict(documents or {})
    for key, want in keys.items():
        source = sources.get(key)
        if not source:
            problems.append(f"{key}: no provenance in bench_baseline.sources")
            continue
        if key not in WORSE_IF_HIGHER and key not in WORSE_IF_LOWER:
            problems.append(f"{key}: unknown direction — add it to WORSE_IF_HIGHER or WORSE_IF_LOWER")
            continue
        path = str(source.get("json"))
        if path not in docs:
            try:
                docs[path] = json.loads((ROOT / path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                problems.append(f"{key}: cannot read {path}: {exc}")
                continue
        try:
            got = resolve(docs[path], str(source.get("selector")))
        except (KeyError, TypeError, IndexError) as exc:
            problems.append(f"{key}: {path} has no {source.get('selector')!r} ({exc})")
            continue
        got = float(got)
        want_f = float(want)
        worse = (got - want_f) / abs(want_f) if want_f else 0.0
        if key in WORSE_IF_LOWER:
            worse = -worse
        if worse > limit:
            problems.append(f"{key}: {got:g} is {worse * 100:.1f} % worse than the baseline {want_f:g} "
                            f"(limit {limit * 100:.0f} %) [{path} {source.get('selector')}]")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="bench smoke against docs/STATE.json")
    ap.add_argument("--state", type=Path, default=STATE)
    ap.add_argument("--json", action="append", default=[], metavar="PATH",
                    help="a fresh bench JSON; may be given once per file (the newest per source wins)")
    ap.add_argument("--limit", type=float, default=REGRESSION_LIMIT)
    args = ap.parse_args(argv)
    state = load_state(args.state)
    docs: dict[str, dict] = {}
    for raw in args.json:
        path = Path(raw)
        docs[path.as_posix()] = json.loads(path.read_text(encoding="utf-8"))
        rel = path.as_posix()
        if not rel.startswith("./"):
            docs[rel] = docs[path.as_posix()]
    problems = check(state, docs, limit=args.limit)
    for line in problems:
        print(f"bench-smoke: {line}", file=sys.stderr)
    if problems:
        return 1
    print(f"bench-smoke: {len(state['bench_baseline']['keys'])} keys within {args.limit * 100:.0f} % "
          f"of {state['bench_baseline'].get('gate_column', 'ext4')} baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
