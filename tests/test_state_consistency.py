"""C31 — `docs/STATE.json` cannot drift from the ledger, the git history, or the bench JSONs.

`STATE.json` had gone stale in the one way that matters: it said `next_ticket: P1-10` for four tickets after
P1-10's neighbourhood was finished, and its `bench_baseline` still carried the de22daa-era tmpfs numbers, so the
10 % smoke rule in Part B §6 was comparing against `rss_mib: 21.51` — a regression to 20 MiB would have passed.

Nothing checked it, so it drifted. These tests are the check:

* the ticket sets agree with `docs/progress.md` **in both directions** (a ticked row with a stale STATE and a
  STATE that invents a tick are both failures);
* `last_green_commit` is an ancestor of `HEAD` (a state file that points at the future is not resumable);
* every `bench_baseline` key resolves through its own `sources` selector in its own JSON, at the precision the key
  is written with, and the smoke run fails when a key is missing or 10 % worse;
* every ticket id named in STATE exists in the plan's table (a renamed or invented id is a reject).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import bench_smoke

PLAN = ROOT / "docs" / "BLACKBOX_FINAL_ARCHITECTURE_v1.1.md"
PROGRESS = ROOT / "docs" / "progress.md"
STATE_PATH = ROOT / "docs" / "STATE.json"


def _state() -> dict:
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _progress_status() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in PROGRESS.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 8:
            continue
        m = re.fullmatch(r"`(P\d[A-Z]?-\d+[a-z]?)`", cells[0])
        if m:
            out[m.group(1)] = cells[2].strip("*")
    return out


def _plan_ids() -> list[str]:
    ids: list[str] = []
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*(P\d[A-Z]?-\d+[a-z]?)\s*\|", line)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def _state_buckets(state: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"done": [], "wip": [], "todo": []}
    for lane, entry in state["lane_status"].items():
        for bucket in out:
            for ticket in entry.get(bucket, []):
                assert ticket not in out["done"] + out["wip"] + out["todo"], \
                    f"{ticket} is listed twice in STATE.json ({lane})"
                out[bucket].append(ticket)
    return out


def test_state_and_progress_agree_in_both_directions() -> None:
    state_buckets = _state_buckets(_state())
    progress = _progress_status()
    state_done = set(state_buckets["done"])
    progress_done = {t for t, s in progress.items() if s == "done"}
    assert state_done == progress_done, (
        f"ticked in progress.md but not done in STATE: {sorted(progress_done - state_done)}; "
        f"done in STATE but not ticked in progress.md: {sorted(state_done - progress_done)}")
    state_wip = set(state_buckets["wip"])
    progress_wip = {t for t, s in progress.items() if s == "wip"}
    assert state_wip == progress_wip, (
        f"wip differs: progress-only {sorted(progress_wip - state_wip)}, "
        f"state-only {sorted(state_wip - progress_wip)}")
    state_todo = set(state_buckets["todo"])
    progress_other = {t for t, s in progress.items() if s not in ("done", "wip")}
    assert progress_other <= state_todo, f"tickets missing from STATE that progress lists: " \
                                         f"{sorted(progress_other - state_todo)}"


def test_every_state_ticket_exists_in_the_plan() -> None:
    plan = set(_plan_ids())
    state_buckets = _state_buckets(_state())
    named = set(state_buckets["done"]) | set(state_buckets["wip"]) | set(state_buckets["todo"])
    assert named, "STATE.json names no tickets at all"
    assert not (named - plan), f"invented or renamed ticket ids in STATE.json: {sorted(named - plan)}"


def test_last_green_commit_is_an_ancestor_of_head() -> None:
    sha = _state()["last_green_commit"]
    assert re.fullmatch(r"[0-9a-f]{7,40}", sha), sha
    proc = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"last_green_commit {sha} is not an ancestor of HEAD"


def test_next_ticket_is_a_real_ticket_that_is_not_done() -> None:
    state = _state()
    assert state["next_ticket"] in set(_plan_ids()), state["next_ticket"]
    assert state["next_ticket"] not in set(_state_buckets(state)["done"]), \
        "STATE points the resume at a ticket that is already ticked"


def test_every_baseline_key_resolves_at_its_stored_precision() -> None:
    """A baseline number is only a baseline if its own JSON still says the same thing."""
    state = _state()
    baseline = state["bench_baseline"]
    for key, value in baseline["keys"].items():
        source = baseline["sources"][key]
        doc = json.loads((ROOT / source["json"]).read_text(encoding="utf-8"))
        resolved = bench_smoke.resolve(doc, source["selector"])
        digits = len(repr(float(value)).split(".")[-1]) if isinstance(value, float) else 0
        assert round(float(resolved), digits) == pytest.approx(float(value)), \
            f"{key}: STATE says {value}, {source['json']} {source['selector']} says {resolved}"


def test_the_baseline_is_the_ext4_gate_column_not_tmpfs() -> None:
    state = _state()
    assert state["bench_baseline"]["gate_column"] == "ext4"
    for key, source in state["bench_baseline"]["sources"].items():
        assert "tmpfs" not in source["selector"], f"{key} reads a tmpfs column: {source}"


def test_bench_smoke_passes_on_the_committed_jsons() -> None:
    assert bench_smoke.check(_state()) == []


def test_bench_smoke_fails_when_provenance_is_missing_or_wrong() -> None:
    """A baseline key nobody can point at is a baseline nobody can check (C31's whole point)."""
    state = _state()
    del state["bench_baseline"]["sources"]["events_per_s"]
    assert any("events_per_s" in p and "provenance" in p for p in bench_smoke.check(state))

    state = _state()                                    # a source that no longer exists in its JSON
    state["bench_baseline"]["sources"]["recovery_ms_per_10k_frames"]["selector"] = "results.ext4.recovery.nope"
    problems = bench_smoke.check(state)
    assert any("recovery_ms_per_10k_frames" in p and "no " in p for p in problems), problems


def test_the_smoke_script_exits_zero_on_the_committed_tree() -> None:
    """`make smoke` is the build-level check; running it here means CI fails when the baseline drifts."""
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "bench_smoke.py")],
                          capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_bench_smoke_fails_on_a_ten_percent_regression() -> None:
    state = _state()
    source = state["bench_baseline"]["sources"]["full_event_p50_us"]
    doc = json.loads((ROOT / source["json"]).read_text(encoding="utf-8"))
    baseline = state["bench_baseline"]["keys"]["full_event_p50_us"]
    row = bench_smoke.resolve(doc, source["selector"])
    assert row > 0
    scaled = baseline * 1.11
    state["bench_baseline"]["keys"]["full_event_p50_us"] = baseline
    docs = {source["json"]: {"results": {"ext4": {"rows": [
        {"gate": True, "p50_us": scaled, "p99_us": bench_smoke.resolve(
            doc, state["bench_baseline"]["sources"]["full_event_p99_us"]["selector"]),
         "events_per_s": bench_smoke.resolve(
            doc, state["bench_baseline"]["sources"]["events_per_s"]["selector"])}]}}}}
    problems = bench_smoke.check(state, docs)
    assert any("full_event_p50_us" in p and "11.0 %" in p for p in problems), problems


def test_throughput_regression_is_measured_in_its_own_direction() -> None:
    """Faster is better for ev/s; the same 10 % rule applies, with the sign flipped."""
    state = _state()
    source = state["bench_baseline"]["sources"]["events_per_s"]
    doc = json.loads((ROOT / source["json"]).read_text(encoding="utf-8"))
    slower = bench_smoke.resolve(doc, source["selector"]) * 0.85
    docs = {source["json"]: {"results": {"ext4": {"rows": [
        {"gate": True,
         "p50_us": bench_smoke.resolve(doc, state["bench_baseline"]["sources"]["full_event_p50_us"]["selector"]),
         "p99_us": bench_smoke.resolve(doc, state["bench_baseline"]["sources"]["full_event_p99_us"]["selector"]),
         "events_per_s": slower}]}, }}, }
    problems = bench_smoke.check(state, docs)
    assert any("events_per_s" in p and "worse" in p for p in problems), problems


def test_an_unknown_direction_is_a_failure_not_a_skip() -> None:
    state = _state()
    state["bench_baseline"]["keys"]["brand_new_metric"] = 1.0
    state["bench_baseline"]["sources"]["brand_new_metric"] = {
        "json": "bench/results/2026-11-14-frame.json", "selector": "results"}
    problems = bench_smoke.check(state)
    assert any("unknown direction" in p for p in problems), problems
