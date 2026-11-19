"""Trigger rules (§10.5, P8-10): the schema, its defaults, and the refusals.

A rule says *when to freeze the evidence*. Everything that makes that safe to have on is in this file's shape: a metric
from a closed list, an operator from a closed list, a window, a stop delay, a cooldown, a per-24-hour cap, and an action
from three. A rule that names something this build does not know is refused by name at load time — a trigger engine that
silently ignores a rule is worse than one that has none, because the user believes they are covered.

Defaults are the ones §10.5 publishes, and they are copied rather than chosen: `window_ms` 60 000, `stop_delay_ms` 5 000,
`cooldown_ms` 60 000, `max_per_24h` 20, fingerprint `[rule, node_path]`, action `freeze`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from blackbox.errors import bbx

#: The eight built-in metrics plus the manual flag. `custom:<name>` is accepted and fed by `blackbox.observe`.
METRICS = ("exception", "exit_code", "stall_ms", "stream_stall_ms", "loop", "budget_usd", "max_steps",
           "error_burst", "manual")

#: Metrics a rule may look back over a window instead of reading the latest sample.
WINDOWED = ("loop", "error_burst")

#: Metrics that are counts of events rather than a value somebody measures.
COUNTED = ("loop", "error_burst", "max_steps")

OPS = (">=", ">", "==", "<=", "<")
ACTIONS = ("mark", "freeze", "snapshot")
FINGERPRINT_PARTS = ("run", "node_path", "rule", "error_type")


@dataclass(frozen=True)
class Rule:
    """One `[[trigger]]` table. Frozen so the engine cannot be "helped" by a caller changing a rule mid-run."""

    name: str
    metric: str
    threshold: float
    op: str = ">="
    window_ms: int = 60_000
    stop_delay_ms: int = 5_000
    cooldown_ms: int = 60_000
    max_per_24h: int = 20
    fingerprint: tuple[str, ...] = ("rule", "node_path")
    action: str = "freeze"
    content: str = "inherit"
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def key(self, parts: dict[str, str]) -> str:
        """The fingerprint a fire is deduplicated on (§10.5: "what makes two fires the same")."""
        return "|".join(f"{part}={parts_get(parts, part)}" for part in self.fingerprint)

    def matches(self, value: float) -> bool:
        return {
            ">=": value >= self.threshold, ">": value > self.threshold, "==": value == self.threshold,
            "<=": value <= self.threshold, "<": value < self.threshold,
        }[self.op]


def parts_get(parts: dict[str, str], part: str) -> str:
    return parts.get(part, "")


def parse_rules(tables: list[dict[str, Any]] | None) -> list[Rule]:
    """Turn `[[trigger]]` tables into rules, refusing anything this build cannot honour — by name, with a code."""
    rules: list[Rule] = []
    seen: set[str] = set()
    for index, table in enumerate(tables or []):
        if not isinstance(table, dict):
            raise bbx("BBX-E170", f"trigger #{index + 1} is not a table")
        name = str(table.get("name") or "").strip()
        if not name:
            raise bbx("BBX-E170", f"trigger #{index + 1} has no name")
        if name in seen:
            # Two rules with one name would share a `trigger_hits` counter and a cooldown, silently.
            raise bbx("BBX-E170", f"trigger {name!r} is defined twice")
        seen.add(name)
        metric = str(table.get("metric") or "").strip()
        if not metric.startswith("custom:") and metric not in METRICS:
            raise bbx("BBX-E170", f"trigger {name!r}: metric {metric!r} is not one of {METRICS} or custom:<name>")
        if metric.startswith("custom:") and len(metric) == len("custom:"):
            raise bbx("BBX-E170", f"trigger {name!r}: custom metric needs a name after 'custom:'")
        if "threshold" not in table:
            raise bbx("BBX-E170", f"trigger {name!r}: threshold is required")
        op = str(table.get("op", ">="))
        if op not in OPS:
            raise bbx("BBX-E170", f"trigger {name!r}: op {op!r} is not one of {OPS}")
        action = str(table.get("action", "freeze"))
        if action not in ACTIONS:
            raise bbx("BBX-E170", f"trigger {name!r}: action {action!r} is not one of {ACTIONS}")
        content = str(table.get("content", "inherit"))
        if content not in ("inherit", "hash", "off"):
            raise bbx("BBX-E170", f"trigger {name!r}: content {content!r} is not inherit/hash/off")
        fingerprint = table.get("fingerprint", ["rule", "node_path"])
        if isinstance(fingerprint, str):
            fingerprint = [fingerprint]
        if not isinstance(fingerprint, list) or not fingerprint:
            raise bbx("BBX-E170", f"trigger {name!r}: fingerprint must be a non-empty list of {FINGERPRINT_PARTS}")
        unknown = [part for part in fingerprint if part not in FINGERPRINT_PARTS]
        if unknown:
            raise bbx("BBX-E170", f"trigger {name!r}: fingerprint part(s) {unknown} unknown")
        rules.append(Rule(
            name=name, metric=metric, threshold=_number(table["threshold"], name, "threshold"), op=op,
            window_ms=_positive(table, "window_ms", 60_000, name, allow_zero=True),
            stop_delay_ms=_positive(table, "stop_delay_ms", 5_000, name, allow_zero=True),
            cooldown_ms=_positive(table, "cooldown_ms", 60_000, name, allow_zero=True),
            max_per_24h=_positive(table, "max_per_24h", 20, name, allow_zero=False),
            fingerprint=tuple(str(part) for part in fingerprint), action=action, content=content,
            raw=dict(table)))
    return rules


def _number(value: Any, name: str, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise bbx("BBX-E170", f"trigger {name!r}: {field_name} must be a number, not {type(value).__name__}")
    return float(value)


def _positive(table: dict[str, Any], key: str, default: int, name: str, *, allow_zero: bool) -> int:
    if key not in table:
        return default
    value = table[key]
    number = _number(value, name, key)
    if number != int(number):
        raise bbx("BBX-E170", f"trigger {name!r}: {key} must be a whole number of ms")
    if number < 0 or (number == 0 and not allow_zero):
        raise bbx("BBX-E170", f"trigger {name!r}: {key} must be {'>= 0' if allow_zero else '> 0'}")
    return int(number)
