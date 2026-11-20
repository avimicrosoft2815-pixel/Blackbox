# G-Capture — 2026-11-16 — trunk `HEAD` — verdict: **NOT PASSED — candidate, awaiting independent review**

This file exists so that the gate has a shape before it has a verdict. The plan's protocol is: the builder declares a
candidate commit, a reviewer clones it fresh, runs the commands and re-reads every must-be-true row, then writes the
verdict. Nothing here is signed, and the rows below say what is proven today and what is not, in those words.

`docs/STATUS.md` §Labelled carries the same fact: **no gate row in this file counts as independent review.** The
builder has run every command below, from the pushed tree, and a second reader has not.

**2026-11-19 — builder-side evidence refreshed; no verdict changed.** Several cells below described a build that no
longer exists (the 60-secret row said "P3-11 does not exist", the anthropic row said "no zero-change child yet", and all
five §15.1 rows said unmeasured). Those cells now cite what is in the tree, with the file or the JSON path. Every
verdict in this file is still **pending** and the signature is still empty: a builder may add evidence, never a tick
(`docs/gates/README.md` rules 1–2).

## Must-be-true rows (AGENT_FINAL.md §5, verbatim) → evidence → ✓/✗/pending

| Row (verbatim) | Evidence | Verdict |
|---|---|---|
| unmodified `openai` and `anthropic` scripts recorded with **zero code changes** | `tests/_day14_child.py` is an `openai` script with a client, a `messages` list and `chat.completions.create(stream=True)` as documented; the recorder is installed around it, not woven into it. Captured by seam A through `httpx2`. The **anthropic** half now exists: `tests/_day14_child_anthropic.py` is an unmodified `anthropic` script (`messages.create(stream=True)`, iterated as the SDK documents) whose only BlackBox lines are the install and the deliberate kill, and `tests/test_day14_kill.py::test_an_unmodified_anthropic_stream_keeps_its_in_flight_call_across_sigkill` is the same `SIGKILL` experiment run against it. `tests/test_day14_kill.py` passes 6 of 6 here (POSIX only; skipped on Windows, where there is no `SIGKILL`) | ✓ openai / **✓ anthropic, builder run — awaiting a reviewer** |
| MID_STREAM interruption with chunk and byte counts | `tests/test_day14_kill.py::test_an_unmodified_openai_stream_keeps_its_in_flight_call_across_sigkill`: real `SIGKILL` at 5 chunks of 40; recovery reports `INTERRUPTED_MID_STREAM` with `chunks ≥ 1`, `partial_stream_bytes > 0`, and **both no larger than the mock provider's own tally of what it pushed down the socket** | ✓ |
| P3-14 identical app output and exceptions | `tests/test_failopen.py` (8): 100 calls bare vs recorded → identical return value and identical stdout; an agent that raises at call 37 → same exception type *and* message; then three real breakages (a configuration the writer refuses with `BBX-E130` on every OS, a keeper raising on every publish, a tape truncated by 512 bytes) each re-run the whole comparison, plus a data directory that is a file on POSIX. The refusal case asserts `enabled: False` and the error code in the reason, so it cannot pass against a recorder that was never broken; defect 26 is why that case is not claimed on Windows | ✓ |
| 60-secret corpus absent from every sink that exists so far | Redaction is P3-11 and exists. `tests/_secrets.py` builds 60 secrets over 10 kinds; `tests/test_redaction.py` (139 tests) drives them through the pipeline, including every split offset of a secret and a real tape (`test_no_secret_from_the_corpus_reaches_a_real_tape`). **Every sink is now covered too**: `tests/test_redaction_every_sink.py` writes the whole corpus through the real sink onto a real tape, recovers a capsule, and greps all nine artefacts the product renders from it — the capsule's own bytes, `otlp/spans.json`, `otlp/spans.pb`, `events/events.jsonl`, `graph.json`, `manifest.json`, the viewer page, the diagnosis report and the repair brief — for all 60 secrets, with 60 redactions asserted so a clean artefact cannot be an empty one. Its third test is the control: with `BLACKBOX_REDACTION=off` the same fixture puts all 60 into the capsule, which is what makes the green run mean "redaction removed them" rather than "nothing was written". Also checked: `hash` mode leaves neither the secret nor an unkeyed digest of it | **pending** — builder evidence, awaiting a reviewer |
| seams A→B→C each proven by a forced-failure test | `tests/test_preflight.py` (9): forcing A picks B and records why; forcing A and B picks C; forcing all three leaves the recorder installed-but-not-capturing with three reasons and no exception; an install that raises does not escape | ✓ |
| day-14 rule (§4) | the row above; and the control `test_the_stream_that_is_not_killed_is_recorded_whole` records the whole stream, ending with a `MODEL_RESPONSE` whose byte count equals the sum of its `MODEL_CHUNK` payloads | ✓ for seam A + POSIX; **not testable here** for real providers (no keys) — the mock is the gate, as the plan allows |

## §15.1 rows this gate must measure

All five quoted from `bench/results/2026-11-15-capture-overhead.json` (`results.ext4`), ext4 column, on the hardware
recorded in the artefact — never from memory or from STATUS (rule 4).

| Row | Status |
|---|---|
| Disabled | **measured for capture**: overhead p50 **0.12 µs**, p95 0.151, p99 0.162, n 5 000 against a 1 µs p50 / 3 µs p99 budget (`results.ext4.disabled.overhead_us`) |
| content=off | **measured**: enqueue p50 **1.424 µs**, p99 2.09, n 5 000, 1 KiB SSE body, against an 8 / 40 µs budget (`results.ext4.enqueue_off.us`) |
| content=hash | **measured**: enqueue p50 **10.953 µs**, p99 31.79, n 5 000, against a 25 / 250 µs budget (`results.ext4.enqueue_hash.us`). A keyword-bearing body costs 138.712 µs p50, p99 242.294, and has no p50 budget — it is reported (`results.ext4.enqueue_hash_keyword.us`) |
| Provider-call latency Δ p95 on a 700 ms streamed call | **measured**: n 20 each, bare p95 698.87 ms vs recorded 691.77 ms → Δ p95 **−1.02 %** against a 3 % budget (`results.ext4.provider_latency`) |
| Startup `install()` | **measured**: n 20, p50 **2.9 ms**, p95 3.477, p99 3.89, from nothing including `RUN_START` and the keeper, against a 25 ms budget (`results.ext4.startup.ms`) |

Every evidence row above is ✓ and every §15.1 row this gate names has a measured number in the tree. What is *not*
here is a reviewer: the file is still unsigned, the §15.1 sample above is Linux-only, and the "60 secrets" row is
covered by tests rather than by an audit of a shipped capsule from a second reader. The honest summary: **the gate's
evidence now exists; the gate's verdict does not, and it is not the builder's to write.**

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

1. ~~P3-14 fail-open suite~~ — done, `tests/test_failopen.py`.
2. ~~P3-11's redaction pipeline plus the 60-secret corpus, asserted absent from every sink that exists at that time~~ —
   done as evidence: `tests/test_redaction.py`, `tests/test_redaction_every_sink.py`.
3. ~~The anthropic half of the zero-code-change row: an `anthropic` child, killed mid-stream like the openai one~~ —
   `tests/_day14_child_anthropic.py` + `tests/test_day14_kill.py`.
4. ~~The four missing §15.1 rows, each with a committed JSON under `bench/results/`~~ — all five are measured in
   `bench/results/2026-11-15-capture-overhead.json`.
5. A reviewer's signature below, from a different model family or a fresh context, with the commands they ran.

## Signature

_(none — unsigned candidate)_
