# Review pack — recovery (this is not a gate file, and carries no verdict) — unsigned builder evidence

**Re-read at `1466f43`, 2026-11-21.** Every command in this pack was re-run against that tree (the one this file now
ships in): 110 tests, all pass, exit 0. No row changed and no row was added; a re-run is not a review.

Recovery is where a recorder either keeps its promises or loses them quietly. The three questions a reviewer can settle
here are: **does a killed run come back with what it had**, **is recovery idempotent** (ten attempts, one capsule), and
**does a damaged tape produce a documented code instead of a traceback**.

## Rows, code and commands

| Row | Where it lives | The command | What this machine saw on 2026-11-20 |
|---|---|---|---|
| A run killed with `SIGKILL` mid-stream comes back with its in-flight call | `src/blackbox/rec/recovery.py`, `tests/test_day14_kill.py`, `tests/test_attach_curl_kill.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_day14_kill.py tests/test_attach_curl_kill.py -q` | 6 + 2 collected, all pass (POSIX; `SIGKILL` does not exist on Windows) |
| Idempotent: ten recoveries of one tape make one capsule with one sha256; six concurrent processes elect one winner | `tests/test_recovery_identity.py`, `src/blackbox/rec/recovery.py` (claim file) | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_recovery_identity.py -q` | **3 collected, all pass** |
| A capsule from a killed run passes its own check | `blackbox verify`, `src/blackbox/cap/integrity.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_verify_cli.py -q` | **23 collected, all pass** |
| Torn frame, bad CRC, zeroed head, corrupt meta → the documented code | `tests/test_fault_codes.py`, `docs/errors.md` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_fault_codes.py -q` | passes; each code is required to be in `errors.REGISTRY` **and** in `docs/errors.md` |
| Retention is a declared hole, not a silent one: a `GAP` verifies, and a dry run writes nothing | `src/blackbox/tape/retention.py`, `tests/test_retention.py`, `tests/test_retention_dry_run.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_retention.py tests/test_retention_dry_run.py -q` | 6 collected in the dry-run file, all pass |
| Recovery from a capsule whose derived parts drifted is refused with `BBX-E412` | `src/blackbox/rec/` derived checks | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_capsule.py tests/test_content_modes.py -q` | passes; this is the check that found defect 110 (a sentence built from the clock) |
| The kill matrix S1–S18 across modes and segment sizes | `tests/kill/kill_matrix.py`, `docs/kill-matrix.md` | `PYTHONPATH=src .venv/bin/python tests/kill/kill_matrix.py --trials 40` (short run; the recorded matrix is 500) | recorded numbers live in `docs/STATUS.md` and the matrix document; the 2 000-trial randomised run is CI (`workflow_dispatch`) |
| The rebind budget (a listener that dies comes back inside it) | `src/blackbox/proxy/supervisor.py`, `tests/test_proxy_supervisor.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_proxy_supervisor.py tests/test_supervisor_loop_errors.py -q` | passes here; the budget is asserted per rebind, not on the median. Defect 108 (an outlier on a loaded runner) is **open** and its entry says what would settle it |
| The proxy's own record survives the run | `src/blackbox/rec/launcher.py` (`proxy_loop` mark) | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_launcher_proxy.py -q` | passes; the tape carries rebinds, port history and the loop's exceptions |

## What a reviewer should decide

1. **Is "keeps what it had" the same as "keeps what the client received"?** The tests compare the capsule's counts against
   the *provider's* tally, never against a constant: the tape may hold less than the origin pushed and must never hold
   more. Look for that comparison in `tests/test_day14_kill.py`.
2. **Concurrency.** Six processes recovering one tape is the honest test; a lock that only works in one process is not a
   lock. `tests/test_recovery_identity.py` starts real processes.
3. **The budget being asserted is the budget the plan names.** `REBIND_BUDGET_MS` in `src/blackbox/proxy/supervisor.py`;
   a test that asserted a larger number would pass and would not be evidence.
4. **What defect 108 leaves open.** One Windows CI cell has seen a 686 ms outlier with 0.0 ms of it inside the rebind; the
   split tool is `tools/outage_split.py` and the artefact is `bench/results/2026-11-20-defect-108-split.json`. The budget
   was not raised, and a passing rerun is not a diagnosis.

## What this machine cannot do

* Windows: the `SIGKILL` rows skip by name there, and the Windows kill path is CI-only.
* A quiet host: the rebind p95 rows are labelled `on_the_bench_box: false` in their own JSON.
* A scheduled week of kill trials: the 2 000-trial run happened once, as a dispatch; the plan asks for a week of them.

## The command

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_recovery_identity.py tests/test_fault_codes.py tests/test_verify_cli.py -q
```
