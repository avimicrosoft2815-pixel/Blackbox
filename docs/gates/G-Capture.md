# G-Capture — 2026-11-16 — trunk `HEAD` — verdict: **NOT PASSED — candidate, awaiting independent review**

This file exists so that the gate has a shape before it has a verdict. The plan's protocol is: the builder declares a
candidate commit, a reviewer clones it fresh, runs the commands and re-reads every must-be-true row, then writes the
verdict. Nothing here is signed, and the rows below say what is proven today and what is not, in those words.

`docs/STATUS.md` §Labelled carries the same fact: **no gate row in this file counts as independent review.** The
builder has run every command below, from the pushed tree, and a second reader has not.

## Must-be-true rows (AGENT_FINAL.md §5, verbatim) → evidence → ✓/✗/pending

| Row (verbatim) | Evidence | Verdict |
|---|---|---|
| unmodified `openai` and `anthropic` scripts recorded with **zero code changes** | `tests/_day14_child.py` is an `openai` script with a client, a `messages` list and `chat.completions.create(stream=True)` as documented; the recorder is installed around it, not woven into it. Captured by seam A through `httpx2`. The **anthropic** half is proven for seam C only (`tests/test_seam_sdk_methods.py`) and has no zero-change child yet | ✓ openai / **pending** anthropic |
| MID_STREAM interruption with chunk and byte counts | `tests/test_day14_kill.py::test_an_unmodified_openai_stream_keeps_its_in_flight_call_across_sigkill`: real `SIGKILL` at 5 chunks of 40; recovery reports `INTERRUPTED_MID_STREAM` with `chunks ≥ 1`, `partial_stream_bytes > 0`, and **both no larger than the mock provider's own tally of what it pushed down the socket** | ✓ |
| P3-14 identical app output and exceptions | **not written.** `tests/test_sink.py` proves fail-open for a hostile sink; nothing yet runs a 100-call agent with and without the recorder and compares stdout and exception identities | **pending** |
| 60-secret corpus absent from every sink that exists so far | **not written.** The redaction pipeline is P3-11 and does not exist; no secret has ever been written by this code because nothing writes request bodies verbatim — but "we believe so" is not a row | **pending** |
| seams A→B→C each proven by a forced-failure test | `tests/test_preflight.py` (9): forcing A picks B and records why; forcing A and B picks C; forcing all three leaves the recorder installed-but-not-capturing with three reasons and no exception; an install that raises does not escape | ✓ |
| day-14 rule (§4) | the row above; and the control `test_the_stream_that_is_not_killed_is_recorded_whole` records the whole stream, ending with a `MODEL_RESPONSE` whose byte count equals the sum of its `MODEL_CHUNK` payloads | ✓ for seam A + POSIX; **not testable here** for real providers (no keys) — the mock is the gate, as the plan allows |

## §15.1 rows this gate must measure

| Row | Status |
|---|---|
| Disabled | measured for the *decorator* path (P3-02: < 1 µs median). **Not measured for capture**: with a seam installed there is no "disabled" fast path yet, and claiming one would be a fiction |
| content=off | **not built** (redaction/P3-11 precedes it) |
| content=hash | the writer row exists (`2026-11-14-writer-throughput.json`); the *capture* variant has not been measured |
| Provider-call latency Δ p95 on a 700 ms streamed call | **not measured** |
| Startup `install()` | **not measured** |

A gate with four of its five measurement rows unmeasured cannot pass, and it is not going to be signed to look close.
The honest summary: **the mechanism the gate exists for works, and half the gate's evidence does not exist yet.**

## How a reviewer reproduces what is proven

```sh
git clone --branch arena/a781c68f-blackbox <repo> gcap && cd gcap
git fetch --unshallow                      # two ancestry tests fail on a depth-1 clone, by design
python -m venv .venv && .venv/bin/pip install -e ".[dev,capture,capture-sdks]"
PYTHONPATH=src .venv/bin/python -m pytest tests/test_day14_kill.py -v   # the day-14 rows
PYTHONPATH=src .venv/bin/python -m pytest tests/test_preflight.py -v    # the forced-failure rows
python tools/ci_status.py                  # exit 0 only if HEAD's newest completed run is green
```

Expected: 3 passed in `test_day14_kill.py` on a POSIX host (skipped on Windows — there is no `SIGKILL`), 9 passed in
`test_preflight.py`, and `ci_status` naming the newest completed run for `HEAD`.

## What would let this gate pass

1. `tests/test_failopen.py` (P3-14): 100-call agent, recorder on and off, identical stdout and identical exception
   types — then the row is ✓.
2. P3-11's redaction pipeline plus the 60-secret corpus, asserted absent from every sink that exists at that time.
3. The four missing §15.1 rows, each with a committed JSON under `bench/results/`.
4. A reviewer's signature below, from a different model family or a fresh context, with the commands they ran.

## Signature

_(none — unsigned candidate)_
