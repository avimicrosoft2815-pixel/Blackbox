# Review pack — capture (this is not a gate file, and carries no verdict)

**Re-read at `1466f43`, 2026-11-21.** Every command in this pack was re-run against that tree (the one this file now
ships in): 43 tests, all pass, exit 0. No row changed and no row was added; a re-run is not a review.

Rows from `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` §24 (Capture) and §5 of the gate protocol. `docs/gates/README.md`
decides what a gate file is; this is the builder's evidence for one area, unsigned.

## Rows, code and commands

| Row (verbatim) | Where it lives | The command | What this machine saw on 2026-11-20 |
|---|---|---|---|
| unmodified `openai` **and** `anthropic` scripts recorded with zero code changes | `src/blackbox/cap_sdk/` (seams A/B), `tests/_day14_child.py`, `tests/_day14_child_anthropic.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_day14_kill.py -q` | **6 collected, all pass** (POSIX: `SIGKILL` does not exist on Windows, where the file skips by name) |
| streaming: partial chunks captured, `MID_STREAM` classified with byte counts | `src/blackbox/cap_sdk/tee.py`, `src/blackbox/cap_sdk/sink.py`, `src/blackbox/rec/recovery.py` | same command, plus `pytest tests/test_stream_termination.py -q` | passes; every `partial_stream_bytes` is compared against the mock provider's own tally, never against a constant |
| fail-open: a deliberately broken recorder leaves the app's output and exceptions identical | `src/blackbox/rec/` (`BBX-E130` refusal path), `tests/test_failopen.py`, `tests/test_capture_failopen.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_failopen.py tests/test_capture_failopen.py -q` | **8 + 4 collected, all pass**; three real breakages (refused config, keeper raising per publish, tape truncated by 512 B) are each re-run through the same comparison |
| the seam ladder A→B→C, each proven by a forced failure | `src/blackbox/cap_sdk/preflight.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_preflight.py -q` | 9 pass: forcing A picks B and records why; forcing all three leaves the recorder installed-but-not-capturing with three reasons and no exception |
| §15.1 overhead rows (disabled, content off/hash, provider-latency Δ, startup) | `bench/bench_capture.py` → `bench/results/2026-11-15-capture-overhead.json` | `PYTHONPATH=src .venv/bin/python bench/bench_capture.py --help`, then re-run it on the reviewer's host | quoted in `docs/gates/G-Capture.md` §15.1 from the artefact itself; the artefact records the hardware it ran on |

## What a reviewer should decide, and how

1. **Is the "zero code changes" claim literally true?** Read `tests/_day14_child.py` and `tests/_day14_child_anthropic.py`.
   Both are ordinary SDK programs; the recorder is installed around them, and the only BlackBox line is the deliberate
   kill. Then run `pytest tests/test_attach_sdks.py -q` for the version matrix: every row is dated or marked
   **unverified**.
2. **Does the fail-open suite fail if the recorder is not broken?** The refusal case asserts `enabled: False` and the error
   code in the reason, which is what stops it passing vacuously.
3. **Are the §15.1 numbers from the artefact or from prose?** Rule 4 in `docs/gates/README.md`: they must come from
   `bench/results/*.json` with the path. `docs/gates/G-Capture.md` does that; check it by opening the JSON.

## What this machine cannot do

* **A real provider.** No API keys here; the mock provider and the published SDKs are the gate, which the plan allows.
* **Windows.** The `SIGKILL` rows skip by name there (there is no `SIGKILL`), and CI runs the rest on Windows and macOS.
* **A quiet host.** The overhead rows are labelled with the machine in their own JSON; `docs/RELEASE-BLOCKERS.md` B6 is
  the row that needs one.

## The evidence that is easiest to check first

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_day14_kill.py tests/test_failopen.py tests/test_preflight.py -q
PYTHONPATH=src .venv/bin/python -m blackbox doctor --capture     # says whether a seam would install on this machine, and why not
```
