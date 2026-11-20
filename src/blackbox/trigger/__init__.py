"""The trigger engine (§10.5, P8-10): when to freeze the evidence, and how not to become the problem.

`rules.parse_rules` turns `[[trigger]]` tables from `blackbox.toml` into `Rule`s, refusing any rule this build cannot
honour. `engine.Engine` takes those rules and a clock, is fed samples by whoever sees a metric, and returns decisions on
`tick()` — from the keeper's thread, never the app's. The keeper writes each decision as a `MARK` frame
(`name="trigger"`, `trigger_suppressed`, `trigger_capped`, `trigger_error`) and calls its `on_trigger` callback when a
`stop_delay` deadline passes.
"""
from blackbox.trigger.engine import DAY_NS, EVAL_INTERVAL_NS, Activity, Engine
from blackbox.trigger.rules import ACTIONS, METRICS, OPS, Rule, parse_rules

__all__ = ["ACTIONS", "DAY_NS", "EVAL_INTERVAL_NS", "METRICS", "OPS", "Activity", "Engine", "Rule", "parse_rules"]
