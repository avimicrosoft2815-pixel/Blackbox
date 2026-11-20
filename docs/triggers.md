# Triggers — when BlackBox freezes the evidence, and what this build actually does

A trigger is a rule that says *"this run just went wrong, keep what happens next and hand me a capsule"*. The shape is
copied from production flight recorders deliberately (Perfetto's `TriggerConfig`, dotnet-monitor's collection rules) rather
than invented: a metric, a comparison, a window, a delay after the fire, a cooldown, a per-24-hour cap, a fingerprint and
an action.

## Writing a rule

Rules live in `blackbox.toml` as `[[trigger]]` tables:

```toml
[[trigger]]
name          = "slow-step"          # required; appears in the events and the run report
metric        = "stall_ms"           # required; see the table below
threshold     = 45_000               # required
op            = ">="                 # >=, >, ==, <=, <      (default >=)
window_ms     = 60_000               # the window a counted metric is evaluated over
stop_delay_ms = 5_000                # keep recording this long after the fire
cooldown_ms   = 60_000               # silence the same fingerprint for this long
max_per_24h   = 20                   # hard cap per fingerprint per rolling 24 h
fingerprint   = ["rule", "node_path"]   # what makes two fires "the same"
action        = "freeze"             # mark, freeze, snapshot
content       = "inherit"            # inherit, hash, off
```

A rule this build cannot honour — an unknown metric, a missing threshold, an operator it does not implement, a duplicate
name, a fingerprint part it cannot compute — is **refused when the configuration is loaded**, with `BBX-E170` naming the
rule. It is not ignored: a user who believes a rule is watching and finds out months later that it never was has been
misled by the tool, not by themselves.

## The metrics

| Metric | Meaning | Where it comes from |
|---|---|---|
| `exception` | an exception escaped at a node | the capture seams |
| `exit_code` | the child process exited non-zero | **the launcher**, before it seals the run |
| `stall_ms` | no heartbeat for N ms | **the launcher's watchdog** |
| `stream_stall_ms` | no chunk on an open model stream | the capture seams |
| `loop` | the same request fingerprint seen N times in the window | the capture seams |
| `budget_usd` | cumulative cost of the run | the capture seams |
| `max_steps` | node count | the capture seams |
| `error_burst` | provider 429/5xx responses in the window | the proxy |
| `manual` | `blackbox.mark("bad_answer")` | you |
| `custom:<name>` | anything you feed with `blackbox.observe(name, value)` | you |

## What is wired in this build, and what is not

**Wired, with tests in `tests/test_triggers.py` (27):** rule parsing and refusal; every timed field
(`stop_delay_ms`, `cooldown_ms`, `max_per_24h`) against a clock the test moves by hand rather than by sleeping; the
windowed counted metrics; edge-triggering (one crossing is one fire, however often the engine evaluates); per-fingerprint
deduplication; persistence of the cap in `triggers.json` under the data directory, so a restart inside the 24 hours does
not hand a rule a fresh cap; fail-open — a rule that raises disables only itself, emits `trigger_error`, and the keeper
and the app are unaffected; and an end-to-end `blackbox run` where an `exit_code` rule fires and the `trigger` MARK is
inside the sealed capsule, not only in the launcher's report.

**Not wired, and said plainly:**

* **Only `exit_code` and `stall_ms` have producers today.** The launcher feeds both at the end of a run. The seam-side
  metrics (`exception`, `stream_stall_ms`, `loop`, `budget_usd`, `max_steps`, `error_burst`) have a working engine but no
  caller inside the capture seams yet, so a rule on one of them loads, validates, and never fires. That is a gap, not a
  default: `tests` do not claim those rules fire in a real run.
* **`freeze` does not seal a second capsule mid-run.** The deadline is scheduled, the engine reports `due`, and the
  launcher writes a `freeze_due` mark saying so — but the capsule is still built once, at the end of the run. What a
  `freeze` gives you today is a recorded fire and the events after it in the same capsule; it does not give you the
  superset-capsule-at-deadline the plan describes.
* **`snapshot`** writes the same marks as `freeze`; the stack dump, allow-listed `env.json` and resolved config that §10.5
  asks a snapshot to carry are not implemented.
* The evaluation thread is the keeper's, and the keeper is the process's; in `mode=serverless` there is no keeper across
  invocations — see `docs/serverless.md`.

## Reading a fire

Every decision the engine makes is a `MARK` frame, so a reader that knows nothing about triggers still shows it:

| `name` | Meaning |
|---|---|
| `trigger` | the rule fired |
| `trigger_suppressed` | the same fingerprint inside its cooldown; counted, no action |
| `trigger_capped` | the fingerprint reached `max_per_24h`; one event, then quiet until the window slides |
| `trigger_error` | the rule raised; that rule is disabled for the session and the payload carries the exception type |
| `trigger_due` | a `stop_delay` deadline passed |
| `freeze_due` | the launcher's response to the deadline; says in its own payload that no second capsule is sealed |

The payload carries `rule`, `metric`, `value`, `fingerprint`, `action` and a `detail` string, so `blackbox view` shows
which rule fired and on what value without the reader holding the configuration.

## Why the caps are persisted, and where

§10.5 asks for the cap to be persisted "per writer" in `run.json`. In this build it is persisted in
`<data>/triggers.json` instead, and the reason is ordering: a run's `run.json` is written when the run ends, which is
exactly the moment a cap has to have already survived. The file holds fire times per rule and fingerprint, times older
than 24 hours are trimmed on read and on write, the write is atomic, and a corrupt file is ignored rather than fatal — a
cap that cannot be written is a weaker cap, never a failed run.
