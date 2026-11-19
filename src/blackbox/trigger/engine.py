"""The trigger engine (§10.5, P8-10): windows, stop delay, cooldown, the 24-hour cap, and fail-open.

Three properties, in the order they matter:

1. **Nothing acts on the app's thread.** `feed()` only appends a sample to a deque; every decision, every counter and every
   callback happens in `tick()`, which the keeper calls from its own thread. The property test asserts exactly this: a
   rule whose threshold is crossed by `feed()` produces no action until `tick()` runs.
2. **A rule that raises disables only itself.** `tick()` catches per rule, records `Activity(kind="error")`, marks that
   rule disabled for the session, and carries on with the rest. The app is never involved and recording continues.
3. **Every field has a test with a mocked clock.** `Engine(rules, clock=...)` takes the time from a callable, so
   `stop_delay_ms`, `cooldown_ms` and `max_per_24h` are asserted against a clock the test moves by hand rather than by
   sleeping — a cap test that sleeps 24 hours is a cap test nobody runs.

The actions are returned, not performed: `Activity` is what the keeper writes into the tape (`MARK` with
`mark="trigger"`) and what a freeze deadline is scheduled from. The engine holds no writer and no capsule, which is why it
can be tested without either.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blackbox.trigger.rules import COUNTED, Rule

DAY_NS = 24 * 60 * 60 * 1_000_000_000
#: §10.5: "evaluates rules at most every 100 ms".
EVAL_INTERVAL_NS = 100_000_000


@dataclass
class Activity:
    """Something the engine decided. The keeper turns each one into a frame and, for freeze, a deadline."""

    kind: str                       # "fire" | "suppressed" | "capped" | "error" | "due"
    rule: str
    metric: str
    value: float
    fingerprint: str
    action: str
    at_ns: int
    detail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _State:
    """One rule's counters. Mutable and private; a rule is frozen, its state is not."""

    samples: deque[tuple[int, float, str]] = field(default_factory=deque)   # (t_ns, value, fingerprint)
    fires: dict[str, deque[int]] = field(default_factory=dict)            # fingerprint -> fire times (24 h)
    last_fire: dict[str, int] = field(default_factory=dict)
    suppressed: int = 0
    capped_for: set[str] = field(default_factory=set)
    fired_for: dict[str, int] = field(default_factory=dict)    # fingerprint -> the sample timestamp it last acted on
    errors: int = 0
    disabled: bool = False


class Engine:
    """Rules in, samples out, decisions on tick. Time comes from `clock` (nanoseconds)."""

    def __init__(self, rules: Iterable[Rule], *, clock: Callable[[], int] = time.monotonic_ns,
                 store: str | os.PathLike[str] | None = None) -> None:
        self.rules: tuple[Rule, ...] = tuple(rules)
        self.clock = clock
        # §10.5: "caps are per writer and persisted in `run.json` so a restart does not reset them inside the 24 h".
        # This build persists them one level up — in `triggers.json` under the data directory — because that is the
        # lifetime a cap actually needs: a run's own `run.json` is written after the run ends, which is exactly the
        # moment a cap must already have survived. The file holds fire times per rule and fingerprint, 24 h are trimmed
        # on read and on write, and a corrupt file is ignored rather than fatal (a trigger must not stop a run).
        self.store = None if store is None else Path(store)
        self.state: dict[str, _State] = {rule.name: _State() for rule in self.rules}
        self.hits: dict[str, int] = {rule.name: 0 for rule in self.rules}
        self._last_eval_ns = -(EVAL_INTERVAL_NS * 2)
        self.pending_freezes: dict[str, int] = {}          # fingerprint -> deadline_ns
        self.error: str | None = None
        if self.store is not None:
            self._load()

    # ── samples ─────────────────────────────────────────────────────────────
    def feed(self, rule: str, value: float, *, now_ns: int | None = None,
             parts: dict[str, str] | None = None) -> None:
        """Record one sample for one rule. Never fires, never raises: this is the app/k-adjacent path."""
        state = self.state.get(rule)
        if state is None or state.disabled:
            return
        now = self.clock() if now_ns is None else now_ns
        spec = self._rule(rule)
        if spec is None:
            return
        key = spec.key({**(parts or {}), "rule": rule})
        state.samples.append((now, value, key))
        horizon = now - spec.window_ms * 1_000_000
        while state.samples and state.samples[0][0] < horizon:
            state.samples.popleft()                   # the ring counter §10.5 asks for, one deque per rule

    def observe_all(self, metric: str, value: float, *, now_ns: int | None = None,
                    parts: dict[str, str] | None = None) -> None:
        """Feed every rule that watches this metric — what a producer calls without knowing the rules."""
        for rule in self.rules:
            if rule.metric == metric:
                self.feed(rule.name, value, now_ns=now_ns, parts=parts)

    # ── decisions ───────────────────────────────────────────────────────────
    def tick(self, *, now_ns: int | None = None, force: bool = False) -> list[Activity]:
        """Evaluate every rule, at most every 100 ms unless `force`. Returns what should happen, in rule order."""
        now = self.clock() if now_ns is None else now_ns
        if not force and now - self._last_eval_ns < EVAL_INTERVAL_NS:
            return []
        self._last_eval_ns = now
        out: list[Activity] = []
        for rule in self.rules:
            state = self.state[rule.name]
            if state.disabled:
                continue
            try:
                out.extend(self._evaluate(rule, state, now))
            except Exception as exc:                  # fail-open: this rule only, and it says so
                state.disabled = True
                state.errors += 1
                self.error = f"{rule.name}: {type(exc).__name__}: {exc}"
                out.append(Activity(kind="error", rule=rule.name, metric=rule.metric, value=0.0, fingerprint="",
                                    action=rule.action, at_ns=now, detail=f"{type(exc).__name__}: {exc}"))
        return out

    def due(self, *, now_ns: int | None = None, force: bool = False) -> list[Activity]:
        """Freeze deadlines that have passed — the `stop_delay` half, which keeps the aftermath inside the capsule."""
        now = self.clock() if now_ns is None else now_ns
        if not force and now - self._last_eval_ns < EVAL_INTERVAL_NS:
            return []
        out: list[Activity] = []
        for key, deadline in sorted(self.pending_freezes.items()):
            if deadline <= now:
                rule_name, _, fingerprint = key.partition(":")
                out.append(Activity(kind="due", rule=rule_name, metric="", value=0.0, fingerprint=fingerprint,
                                    action="freeze", at_ns=now, detail="stop delay elapsed"))
        for activity in out:
            self.pending_freezes.pop(f"{activity.rule}:{activity.fingerprint}", None)
        return out

    def _evaluate(self, rule: Rule, state: _State, now: int) -> list[Activity]:
        value, key, sample_ns = self._sample(rule, state, now)
        if value is None or not rule.matches(value):
            return []
        # Edge-triggered: a rule acts when a *new* sample crosses it, not on every evaluation while a condition holds.
        # Without this, a 100 ms evaluation interval turns one long stall into ten fires per second and the cooldown is
        # the only thing standing between a user and a tape full of trigger marks.
        if state.fired_for.get(key) == sample_ns:
            return []
        fires = state.fires.setdefault(key, deque())
        while fires and fires[0] <= now - DAY_NS:
            fires.popleft()
        if len(fires) >= rule.max_per_24h:
            if key in state.capped_for:
                return []                              # "goes quiet until the window slides"
            state.capped_for.add(key)
            return [Activity(kind="capped", rule=rule.name, metric=rule.metric, value=value, fingerprint=key,
                             action=rule.action, at_ns=now,
                             detail=f"{len(fires)} fires in 24 h, cap {rule.max_per_24h}")]
        last = state.last_fire.get(key)
        if last is not None and now - last < rule.cooldown_ms * 1_000_000:
            state.suppressed += 1
            return [Activity(kind="suppressed", rule=rule.name, metric=rule.metric, value=value, fingerprint=key,
                             action=rule.action, at_ns=now,
                             detail=f"within cooldown ({rule.cooldown_ms} ms)")]
        fires.append(now)
        state.last_fire[key] = now
        state.fired_for[key] = sample_ns
        self._save()
        state.capped_for.discard(key)                  # a new fire after the cap slid is not still capped
        self.hits[rule.name] += 1
        if rule.action in ("freeze", "snapshot"):
            self.pending_freezes[f"{rule.name}:{key}"] = now + rule.stop_delay_ms * 1_000_000
        return [Activity(kind="fire", rule=rule.name, metric=rule.metric, value=value, fingerprint=key,
                         action=rule.action, at_ns=now,
                         detail=f"{rule.metric} {rule.op} {rule.threshold:g} -> {value:g}")]

    def _sample(self, rule: Rule, state: _State, now: int) -> tuple[float | None, str, int]:
        empty = (None, rule.key({"rule": rule.name}), -1)
        if not state.samples:
            return empty
        horizon = now - rule.window_ms * 1_000_000
        window = [sample for sample in state.samples if sample[0] >= horizon]
        if not window:
            return empty
        latest = window[-1]
        if rule.metric in COUNTED:
            return float(sum(1 for sample in window if sample[2] == latest[2])), latest[2], latest[0]
        return latest[1], latest[2], latest[0]

    def _rule(self, name: str) -> Rule | None:
        for rule in self.rules:
            if rule.name == name:
                return rule
        return None

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        assert self.store is not None
        try:
            document = json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                                     # absent or corrupt: start with empty caps, say nothing
        now = self.clock()
        for rule_name, per_key in (document.get("fires") or {}).items():
            state = self.state.get(rule_name)
            if state is None or not isinstance(per_key, dict):
                continue
            for key, times in per_key.items():
                kept = deque(sorted(int(t) for t in times if int(t) > now - DAY_NS))
                if kept:
                    state.fires[key] = kept
                    state.last_fire[key] = kept[-1]

    def _save(self) -> None:
        if self.store is None:
            return
        now = self.clock()
        fires = {name: {key: [t for t in times if t > now - DAY_NS] for key, times in state.fires.items()}
                 for name, state in self.state.items()}
        fires = {rule: per_key for rule, per_key in fires.items() if per_key}
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.store.with_suffix(self.store.suffix + ".tmp")
            temporary.write_text(json.dumps({"schema": "blackbox.triggers/1", "fires": fires}, sort_keys=True) + "\n",
                                 encoding="utf-8")
            temporary.replace(self.store)
        except OSError:
            return                                     # a cap that cannot be written is a weaker cap, not a failed run

    # ── introspection ───────────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        return {
            "rules": [{"name": rule.name, "metric": rule.metric, "op": rule.op, "threshold": rule.threshold,
                       "action": rule.action, "hits": self.hits.get(rule.name, 0),
                       "suppressed": self.state[rule.name].suppressed,
                       "errors": self.state[rule.name].errors,
                       "disabled": self.state[rule.name].disabled} for rule in self.rules],
            "pending_freezes": sorted(self.pending_freezes),
            "error": self.error,
        }
