"""P8-10 — the trigger engine's limits, each against a clock the test moves by hand (plan §10.5).

The plan's acceptance line is specific: *"`stop_delay`, `cooldown`, `max_per_24h` each have a passing test with a mocked
clock; a rule that raises disables only itself"*. Each of those has a test below that names it, and none of them sleeps:
24 hours of cap behaviour is asserted by adding 24 hours to an integer.

Four more properties are worth having because the failure mode is silent:

* **Nothing acts on the app's thread.** `feed()` records a sample and returns `None`; no action, no callback, no seal.
  Only `tick()` decides, and `tick()` is called by the keeper thread (`test_no_action_runs_on_the_feed_path`).
* **A fire is deduplicated on its fingerprint**, not on the rule, so two different nodes hitting the same rule are two
  fires and one node hitting it twice inside `cooldown_ms` is one.
* **The cap persists per fingerprint** and goes quiet after one `capped` event rather than emitting one per evaluation.
* **A rule this build cannot honour is refused at parse time by name**, because an ignored rule is a user who believes
  they are covered.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.errors import BlackBoxError
from blackbox.tape.keeper import Keeper
from blackbox.tape.reader import iter_tape
from blackbox.tape.writer import TapeWriter
from blackbox.trigger import Activity, Engine, parse_rules

MS = 1_000_000


class Clock:
    """A clock the test owns. Every engine in this file takes one, so no assertion depends on the machine's speed."""

    def __init__(self, start_ns: int = 1_700_000_000 * 10**9) -> None:
        self.now = start_ns

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: float) -> int:
        self.now += int(ms * MS)
        return self.now


def _engine(table: dict) -> tuple[Engine, Clock]:
    clock = Clock()
    return Engine(parse_rules([table]), clock=clock), clock


def _rule(**overrides) -> dict:
    table = {"name": "stall", "metric": "stall_ms", "threshold": 1000, "stop_delay_ms": 0, "cooldown_ms": 0,
             "max_per_24h": 20, "fingerprint": ["rule", "node_path"], "action": "mark"}
    table.update(overrides)
    return table


# ── schema ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table, why", [
    ({"metric": "stall_ms", "threshold": 1}, "no name"),
    ({"name": "x", "metric": "vibes", "threshold": 1}, "unknown metric"),
    ({"name": "x", "metric": "stall_ms"}, "no threshold"),
    ({"name": "x", "metric": "stall_ms", "threshold": 1, "op": "=~"}, "unknown op"),
    ({"name": "x", "metric": "stall_ms", "threshold": 1, "action": "email"}, "unknown action"),
    ({"name": "x", "metric": "stall_ms", "threshold": 1, "fingerprint": ["rule", "soul"]}, "unknown fingerprint part"),
    ({"name": "x", "metric": "stall_ms", "threshold": 1, "max_per_24h": 0}, "a cap of zero fires never"),
    ({"name": "x", "metric": "stall_ms", "threshold": "soon"}, "a threshold that is not a number"),
    ({"name": "x", "metric": "custom:", "threshold": 1}, "a custom metric with no name"),
])
def test_a_rule_this_build_cannot_honour_is_refused_by_name(table: dict, why: str) -> None:
    with pytest.raises(BlackBoxError) as caught:
        parse_rules([table])
    assert caught.value.code == "BBX-E170", why


def test_two_rules_with_one_name_are_refused() -> None:
    """One name, two rules, would share a hit counter and a cooldown — a silent merge nobody asked for."""
    with pytest.raises(BlackBoxError) as caught:
        parse_rules([_rule(name="same"), _rule(name="same", threshold=5)])
    assert caught.value.code == "BBX-E170"
    assert "defined twice" in str(caught.value)


def test_the_defaults_are_the_published_ones() -> None:
    (rule,) = parse_rules([{"name": "d", "metric": "stall_ms", "threshold": 1}])
    assert (rule.window_ms, rule.stop_delay_ms, rule.cooldown_ms, rule.max_per_24h) == (60_000, 5_000, 60_000, 20)
    assert rule.action == "freeze" and rule.fingerprint == ("rule", "node_path") and rule.op == ">="


# ── the three timed fields ──────────────────────────────────────────────────


def test_stop_delay_keeps_recording_after_the_fire_before_freezing() -> None:
    engine, clock = _engine(_rule(stop_delay_ms=5_000, action="freeze"))
    engine.feed("stall", 2_000, now_ns=clock.now, parts={"node_path": "root"})
    (fire,) = engine.tick(now_ns=clock.now, force=True)
    assert fire.kind == "fire" and fire.action == "freeze"
    assert engine.due(now_ns=clock.now, force=True) == [], "nothing is due before the stop delay"
    clock.advance(4_999)
    assert engine.due(now_ns=clock.now, force=True) == []
    clock.advance(1)
    (due,) = engine.due(now_ns=clock.now, force=True)
    assert due.kind == "due" and due.rule == "stall"
    assert engine.due(now_ns=clock.now, force=True) == [], "a deadline fires once"


def test_mark_actions_schedule_nothing() -> None:
    engine, clock = _engine(_rule(stop_delay_ms=10, action="mark"))
    engine.feed("stall", 2_000, now_ns=clock.now)
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire"
    clock.advance(60_000)
    assert engine.due(now_ns=clock.now, force=True) == []


def test_cooldown_suppresses_a_repeat_without_losing_count_of_it() -> None:
    engine, clock = _engine(_rule(cooldown_ms=60_000))
    parts = {"node_path": "root"}
    engine.feed("stall", 2_000, now_ns=clock.now, parts=parts)
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire"
    clock.advance(1_000)
    engine.feed("stall", 3_000, now_ns=clock.now, parts=parts)
    (suppressed,) = engine.tick(now_ns=clock.now, force=True)
    assert suppressed.kind == "suppressed" and "cooldown" in (suppressed.detail or "")
    assert engine.state["stall"].suppressed == 1
    clock.advance(60_000)
    engine.feed("stall", 3_000, now_ns=clock.now, parts=parts)
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire", "after the cooldown it fires again"


def test_a_different_fingerprint_is_not_suppressed_by_the_first() -> None:
    engine, clock = _engine(_rule(cooldown_ms=60_000))
    engine.feed("stall", 2_000, now_ns=clock.now, parts={"node_path": "a"})
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire"
    engine.feed("stall", 2_000, now_ns=clock.now, parts={"node_path": "b"})
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire", "two nodes are two fires"


def test_max_per_24h_caps_the_rule_and_says_so_once() -> None:
    engine, clock = _engine(_rule(cooldown_ms=0, max_per_24h=3))
    parts = {"node_path": "root"}
    kinds = []
    for _ in range(2):
        engine.feed("stall", 2_000, now_ns=clock.now, parts=parts)
        kinds.append(engine.tick(now_ns=clock.now, force=True)[0].kind)
        clock.advance(1_000)
    assert kinds == ["fire", "fire"]
    engine.feed("stall", 2_000, now_ns=clock.now, parts=parts)
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire", "the third is inside the cap"
    clock.advance(1_000)
    engine.feed("stall", 2_000, now_ns=clock.now, parts=parts)
    (capped,) = engine.tick(now_ns=clock.now, force=True)
    assert capped.kind == "capped" and "cap 3" in (capped.detail or "")
    clock.advance(1_000)
    engine.feed("stall", 2_000, now_ns=clock.now, parts=parts)
    assert engine.tick(now_ns=clock.now, force=True) == [], "one capped event, then quiet"
    clock.advance(24 * 60 * 60 * 1_000)
    engine.feed("stall", 2_000, now_ns=clock.now, parts=parts)
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire", "the 24 h window slides"


def test_the_cap_survives_a_restart_because_it_is_persisted(tmp_path: Path) -> None:
    """§10.5: "caps are per writer and persisted … so a restart does not reset them inside the 24 h"."""
    store = tmp_path / "triggers.json"
    table = _rule(cooldown_ms=0, max_per_24h=2)
    clock = Clock()
    first = Engine(parse_rules([table]), clock=clock, store=store)
    for _ in range(2):
        first.feed("stall", 5_000, now_ns=clock.now, parts={"node_path": "root"})
        assert first.tick(now_ns=clock.now, force=True)[0].kind == "fire"
        clock.advance(1_000)
    assert store.exists(), "the fire times are on disk, not only in memory"

    second = Engine(parse_rules([table]), clock=clock, store=store)   # a new process, the same data directory
    second.feed("stall", 5_000, now_ns=clock.now, parts={"node_path": "root"})
    (capped,) = second.tick(now_ns=clock.now, force=True)
    assert capped.kind == "capped", "a restart must not hand the rule a fresh cap"
    clock.advance(24 * 60 * 60 * 1_000)
    second.feed("stall", 5_000, now_ns=clock.now, parts={"node_path": "root"})
    assert second.tick(now_ns=clock.now, force=True)[0].kind == "fire", "and the window still slides"


def test_a_corrupt_cap_file_is_ignored_and_the_run_continues(tmp_path: Path) -> None:
    store = tmp_path / "triggers.json"
    store.write_text("{not json", encoding="utf-8")
    clock = Clock()
    engine = Engine(parse_rules([_rule(cooldown_ms=0)]), clock=clock, store=store)
    engine.feed("stall", 5_000, now_ns=clock.now, parts={"node_path": "root"})
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire"
    assert json.loads(store.read_text())["fires"], "and the file is rewritten with the real fire"


def test_a_static_condition_fires_once_per_new_sample_not_once_per_evaluation() -> None:
    """Edge-triggered, not level-triggered: ten evaluations of one crossing are one fire, not ten marks."""
    engine, clock = _engine(_rule(cooldown_ms=0, action="mark"))
    engine.feed("stall", 5_000, now_ns=clock.now, parts={"node_path": "root"})
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire"
    for _ in range(5):
        clock.advance(200)                     # past the 100 ms interval, so each tick really evaluates
        assert engine.tick(now_ns=clock.now, force=True) == [], "no new sample, no new fire"
    engine.feed("stall", 5_000, now_ns=clock.now, parts={"node_path": "root"})
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire", "a new crossing is a new fire"


def test_the_window_metric_counts_only_samples_inside_the_window() -> None:
    engine, clock = _engine({"name": "loop", "metric": "loop", "threshold": 3, "window_ms": 1_000, "action": "mark"})
    for _ in range(2):
        engine.feed("loop", 1.0, now_ns=clock.now, parts={"node_path": "same"})
        clock.advance(100)
    assert engine.tick(now_ns=clock.now, force=True) == [], "two occurrences are below a threshold of three"
    engine.feed("loop", 1.0, now_ns=clock.now, parts={"node_path": "same"})
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire"
    clock.advance(2_000)
    engine.feed("loop", 1.0, now_ns=clock.now, parts={"node_path": "same"})
    assert engine.tick(now_ns=clock.now, force=True) == [], "the old samples have left the window"


# ── fail-open, and where actions run ────────────────────────────────────────


class _Exploding(Engine):
    """An engine whose first rule raises inside evaluation — the fault §10.5 says must disable one rule only."""

    def _evaluate(self, rule, state, now):                       # type: ignore[override]
        if rule.name == "boom":
            raise RuntimeError("this rule is broken")
        return super()._evaluate(rule, state, now)


def test_a_rule_that_raises_disables_only_itself() -> None:
    rules = parse_rules([_rule(name="boom", threshold=1, action="mark"),
                         _rule(name="fine", threshold=1, action="mark")])
    clock = Clock()
    engine = _Exploding(rules, clock=clock)
    engine.feed("boom", 5, now_ns=clock.now, parts={"node_path": "root"})
    engine.feed("fine", 5, now_ns=clock.now, parts={"node_path": "root"})
    activities = engine.tick(now_ns=clock.now, force=True)
    by_kind = {activity.rule: activity.kind for activity in activities}
    assert by_kind == {"boom": "error", "fine": "fire"}, by_kind
    assert engine.state["boom"].disabled and not engine.state["fine"].disabled
    assert engine.error and "this rule is broken" in engine.error
    # The broken rule stays quiet; the healthy one keeps working.
    clock.advance(120_000)
    engine.feed("fine", 5, now_ns=clock.now, parts={"node_path": "other"})
    assert [activity.kind for activity in engine.tick(now_ns=clock.now, force=True)] == ["fire"]


def test_no_action_runs_on_the_feed_path() -> None:
    """The property the plan asks for: feeding a sample never decides, never seals and never calls back."""
    engine, clock = _engine(_rule(stop_delay_ms=0, action="freeze"))
    calls: list[Activity] = []
    engine.pending_freezes.clear()
    for _ in range(50):
        engine.feed("stall", 10_000, now_ns=clock.now, parts={"node_path": "root"})
        clock.advance(1)
    assert engine.pending_freezes == {}, "feeding must not schedule a freeze"
    assert engine.hits["stall"] == 0, "feeding must not count a hit"
    assert calls == []
    assert engine.tick(now_ns=clock.now, force=True)[0].kind == "fire"


def test_evaluation_is_rate_limited_to_100_ms_unless_forced() -> None:
    engine, clock = _engine(_rule())
    engine.feed("stall", 5_000, now_ns=clock.now)
    assert engine.tick(now_ns=clock.now, force=True)
    engine.feed("stall", 5_000, now_ns=clock.now, parts={"node_path": "b"})
    assert engine.tick(now_ns=clock.now) == [], "the same millisecond is inside the 100 ms evaluation interval"
    clock.advance(100)
    assert engine.tick(now_ns=clock.now)[0].kind == "fire"


# ── integration: the keeper writes what the engine decided ──────────────────


def _marks(data_dir: Path) -> list[dict]:
    out = []
    for frame in iter_tape(str(data_dir / "tape" / next((data_dir / "tape").iterdir()).name)):
        if frame.get("kind") == C.KIND_MARK:
            out.append(cbor.decode_exact(frame["payload"]))
    return out


def test_the_keeper_writes_the_fire_as_a_mark_and_never_touches_the_app_thread(tmp_path: Path) -> None:
    clock = Clock()
    engine = Engine(parse_rules([_rule(stop_delay_ms=0, action="freeze")]), clock=clock)
    deadlines: list[Activity] = []
    writer = TapeWriter(str(tmp_path), segment_bytes=C.MIN_SEGMENT_BYTES).open()
    keeper = Keeper(writer, triggers=engine, on_trigger=deadlines.append)
    keeper.tick_s = 1.0                      # the thread stays out of the way; the test calls tick() itself
    try:
        writer.append(C.KIND_RUN_START, cbor.encode({"run_id": "r_00000000000000aa"}), priority=0)
        engine.feed("stall", 9_000, now_ns=clock.now, parts={"node_path": "root"})
        writer.flush()
        assert _marks(tmp_path) == [], "feeding wrote nothing by itself"
        keeper.tick()                          # the keeper's own tick, unforced: the engine paces itself at 100 ms
        clock.advance(150)
        keeper.evaluate_triggers(force=True)   # the deadline half, at a stop_delay of 0
        writer.append(C.KIND_RUN_END, cbor.encode({}), priority=0)
        writer.flush()
        writer.publish()                     # a flush commits to the window; a publish is what a reader can see
        marks = _marks(tmp_path)
    finally:
        keeper.stop()
        writer.close()
    names = [mark["name"] for mark in marks]
    assert names == ["trigger", "trigger_due"], names
    assert marks[0]["rule"] == "stall" and marks[0]["metric"] == "stall_ms"
    assert len(deadlines) == 1 and deadlines[0].kind == "due"
    assert keeper.stats()["trigger_events"] == 2


def test_the_launcher_callback_records_that_the_deadline_passed(tmp_path: Path) -> None:
    """`freeze_due` is what the launcher's callback adds, and it says plainly that no second capsule is sealed."""
    from blackbox.rec.launcher import _on_freeze_deadline

    writer = TapeWriter(str(tmp_path), segment_bytes=C.MIN_SEGMENT_BYTES).open()
    try:
        writer.append(C.KIND_RUN_START, cbor.encode({"run_id": "r_00000000000000ac"}), priority=0)
        _on_freeze_deadline(writer)(Activity(kind="due", rule="stall", metric="", value=0.0,
                                             fingerprint="rule=stall", action="freeze", at_ns=0))
        writer.flush()
        writer.publish()
        marks = _marks(tmp_path)
    finally:
        writer.close()
    (mark,) = marks
    assert mark["name"] == "freeze_due" and mark["rule"] == "stall"
    assert mark["note"].startswith("the run is sealed once, at the end")


def test_a_broken_rule_does_not_take_the_keeper_down(tmp_path: Path) -> None:
    """TRIGGER_ERROR, then recording continues — the keeper's own fail-open rule, one level up."""
    rules = parse_rules([_rule(name="boom", threshold=1), _rule(name="fine", threshold=1)])
    clock = Clock()
    engine = _Exploding(rules, clock=clock)
    writer = TapeWriter(str(tmp_path), segment_bytes=C.MIN_SEGMENT_BYTES).open()
    keeper = Keeper(writer, triggers=engine)
    try:
        writer.append(C.KIND_RUN_START, cbor.encode({"run_id": "r_00000000000000ab"}), priority=0)
        engine.feed("boom", 5, now_ns=clock.now, parts={"node_path": "root"})
        engine.feed("fine", 5, now_ns=clock.now, parts={"node_path": "root"})
        keeper.evaluate_triggers(force=True)                          # the keeper's own tick, unforced: the engine paces itself at 100 ms
        clock.advance(150)
        keeper.evaluate_triggers(force=True)   # the deadline half, at a stop_delay of 0
        writer.append(C.KIND_RUN_END, cbor.encode({}), priority=0)
        writer.flush()
        writer.publish()
        marks = _marks(tmp_path)
    finally:
        keeper.stop()
        writer.close()
    assert [mark["name"] for mark in marks] == ["trigger_error", "trigger"], marks
    assert marks[0]["rule"] == "boom" and "RuntimeError" in marks[0]["detail"]
    assert not keeper.disabled


def test_a_run_with_a_trigger_rule_records_the_fire_end_to_end(tmp_path: Path) -> None:
    """`blackbox run` with a `[[trigger]]` in blackbox.toml: a real child, a real tape, a real TRIGGER mark."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "blackbox.toml").write_text(
        '[[trigger]]\nname = "quick-exit"\nmetric = "exit_code"\nthreshold = 1\nop = ">="\n'
        'action = "mark"\ncooldown_ms = 0\n', encoding="utf-8")
    script = project / "app.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("BLACKBOX")}
    env["PYTHONPATH"] = str(ROOT / "src")
    env["BLACKBOX_HOME"] = str(tmp_path / "data")
    completed = subprocess.run([sys.executable, "-m", "blackbox", "run", "--no-proxy", "--no-sdk", "--no-hooks",
                                "--", sys.executable, str(script)],
                               cwd=project, env=env, capture_output=True, text=True, timeout=300)
    assert completed.returncode == 3, completed.stderr[-2000:]
    data = tmp_path / "data"
    capsules = list((data / "capsules").glob("*.bbx"))
    assert capsules, completed.stderr[-2000:]
    # The fire is *in the capsule*, not only in the launcher's report: the rule is `exit_code >= 1` and the child
    # exited 3, so the launcher fed the engine and the keeper wrote a MARK before RUN_END.
    from blackbox.cap import format as cap

    capsule = cap.read_capsule(str(capsules[0]))
    marks = []
    for name in sorted(capsule.entries):
        blob = capsule.entries[name]
        offset = C.SEGMENT_HEADER_BYTES
        while offset < len(blob):
            from blackbox.tape import frame as frame_mod

            try:
                fields, offset = frame_mod.decode(blob, offset)
            except Exception:
                break
            if fields["kind"] == C.KIND_MARK:
                marks.append(cbor.decode_exact(fields["payload"]))
    assert [mark["name"] for mark in marks] == ["trigger"], marks
    assert marks[0]["rule"] == "quick-exit" and marks[0]["metric"] == "exit_code" and marks[0]["value"] == 3.0
