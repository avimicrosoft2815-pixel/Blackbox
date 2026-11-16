# ADR-006 — the rebind budget is an *outage*, measured on every sample

**Status:** accepted 2026-11-16 · **Affects:** §11.8, P3B-03 (the supervision clause), defect 40 · **Evidence:**
`bench/results/2026-11-16-rebind-a.json` and `-b.json` via `bench/bench_rebind.py` · **Reopens if** a macOS/Windows cell or the bench box
disagrees with the idle column (§6)

## 1. Decision

§11.8 says: *"The launcher supervises the proxy as a thread; if it dies, the listener is re-bound within 100 ms."*

The subject of that sentence is **the listener coming back**, not the function that brings it back. So the row is
measured as an **outage**:

```
outage_ms = (a listener is serving again on the same port) − (the listener stopped serving)
```

That quantity contains two costs, and the bench reports both so neither can be quoted as the other:

| component | what it is | measured (idle, n=120 over two runs) |
|---|---|---|
| detection | the supervisor's tick — a watchdog can only notice on a tick | `outage − rebind`, p50 ≈ 11 ms |
| rebind | `ProxyThread.rebinds[i]`: inside `rebind()`, from the lock to the listener serving | p50 0.80–0.82 ms, max 2.90 ms (two runs) |

Two consequences, both binding:

1. **The gate is per sample.** Every watchdog-triggered outage must be inside `REBIND_BUDGET_MS`, in both columns. A
   median or a p95 is not the requirement: a child does not get the median.
2. **The default supervision tick is 20 ms** (`SUPERVISE_MS` in `src/blackbox/proxy/supervisor.py`), down from 50 ms.
   The tick is a design parameter that consumes the budget, so a budget met by accident of arithmetic is not met. See §4.

## 2. What was wrong with the previous evidence

Defect 40 is the history. The row was originally asserted from a **single** wall-clock sample, and one shared macOS
runner spent 107.9 ms on it. The response was a **median of five rebinds** — of which one was the watchdog's and four
were manual `rebind()` calls — with the five printed in the failure message. That is a statistic about a different
experiment in three separate ways:

* it measured `rebind()` and called it the budget, so the tick — the cost that actually dominates — was not in the
  number at all;
* four of five samples were produced by the test calling `rebind()` directly, which no child ever experiences;
* five samples of a distribution whose worst case is the thing being promised is a sample of the wrong statistic.

The honest correction is to make the *measurement* match the *sentence*, and to make the design leave enough room that
the sentence survives a machine we do not control.

## 3. The measurement (2026-11-16, this container: 2 vCPU, x86_64, Linux 6.1.158, CPython 3.11.2)

`bench/bench_rebind.py --n 60`, 60 watchdog-triggered breaks per column, phase randomised per trial, every sample kept
in the JSON. Run **twice**, and both runs are committed, because a single run of a timing bench is a sample of a
distribution and picking the better of two would be the median-of-five mistake in a new costume.

| run | column | n | min | p50 | p95 | p99 | max | over 100 ms | port moved |
|---|---|---|---|---|---|---|---|---|---|
| a | idle — **gate** | 60 | 1.73 | 12.39 | 20.76 | 21.86 | **21.86** | 0 | 0 |
| b | idle — **gate** | 60 | 1.70 | 11.58 | 20.53 | 21.38 | **21.38** | 0 | 0 |
| a | loaded, one busy process per CPU | 60 | 1.25 | 11.52 | 20.63 | 21.47 | **21.47** | 0 | 0 |
| b | loaded, one busy process per CPU | 60 | 1.46 | 11.77 | 20.44 | 20.59 | **20.59** | 0 | 0 |
| a | rebind only, idle | 60 | 0.55 | 0.82 | 1.17 | 2.05 | 2.05 | — | — |
| b | rebind only, idle | 60 | 0.59 | 0.80 | 1.02 | 2.90 | 2.90 | — | — |
| a | rebind only, loaded | 60 | 0.41 | 0.59 | 0.81 | 0.84 | 0.84 | — | — |
| b | rebind only, loaded | 60 | 0.29 | 0.51 | 0.76 | 0.89 | 0.89 | — | — |

Gate row: **PASS** in both runs, `n = 240` samples total, `over_budget_samples = 0`, `port_moved = 0`, worst sample
**21.86 ms** against 100 ms. The two idle runs differ by 0.5 ms at the max and the loaded column is no worse than idle —
the bound is a property of the tick plus a sub-millisecond rebind, not of which run was measured.

**A note on the first run of this bench, because it is the reason the numbers above are trustworthy.** With `--n 40` and
no phase randomisation, and the tick still at 50 ms, the distribution read `min 50.35 / p50 50.95 / max 51.88` — 50 ms
*minimum*, which is impossible for a uniformly-phased outage. The cause was structural: trials ran back-to-back, each
one ending exactly at the tick that repaired it, so the next break landed immediately after a tick and every sample paid
a full tick. That run is a **maximum**, not a distribution, and quoting its p50 as "typical" would have been the same
class of error as quoting a median. The bench now sleeps a seeded random fraction of a tick before each break
(`phase_seed` in the JSON), so the samples cover the whole phase space; the max over them is the number the budget must
survive and the min shows the rebind cost when detection is already imminent.

## 4. Why the tick moved from 50 ms to 20 ms

Budget arithmetic, using the measured rebind (max 2.90 ms) and treating detection as the tick it is:

| tick | worst-case outage | margin under 100 ms | left for a stalled thread |
|---|---|---|---|
| 50 ms | 51.9 ms | 1.9× | 48 ms |
| **20 ms** | **21.9 ms** | **4.6×** | **78 ms** |

The 107.9 ms sample in defect 40 was a thread that lost ~100 ms to something outside this process. A 50 ms tick leaves
48 ms for that; a 20 ms tick leaves 78 ms. Nothing else in the design gets cheaper by using a coarser tick: the tick is
one `is_serving()` attribute read, 50 times a second, on a thread that is otherwise blocked in `Event.wait()` and
holding no lock (`Event.wait` releases the GIL). A coarser tick buys nothing and spends the only margin the row has.

This is an amendment to a design default, so it is recorded as a decision (D-043) as well. No wire format, error code,
CLI verb or file name changes; `supervise_ms` remains a constructor argument for callers that want to measure a different
tick.

## 5. Alternatives rejected

| Alternative | Why it lost |
|---|---|
| Keep the median of five | Defect 40's fix. It answers "is the typical rebind fast" when the promise is "the listener is back within 100 ms". It also mixed 4 manual rebinds into a watchdog row |
| Assert the p95 of many samples | Same error with more arithmetic: a p95 is a statement about a population, and the plan's bound is about the one outage a child lived through |
| Retry until a sample lands inside the budget | Optimising the measurement, not the system. Also the exact move the task directive forbids |
| Widen the budget to whatever the runner does | Turns a product promise into a description of CI hardware |
| Move supervision out of process (a real supervisor process, `SIGCHLD`/`kqueue`) | A separate process can watch the proxy but cannot put a *listener* back inside a Python app's interpreter; the socket also has to keep the port the child was told. Out of scope, and it would not fix the GIL limitation in §6 either |
| Raise the thread's priority / pin a core | Needs privilege the product does not have and would not have on macOS or in a container |

## 6. Limitations this ADR does **not** remove

Written down because a budget with unstated preconditions is a claim, not a measurement:

1. **One machine.** 2 vCPU x86_64 Linux container, two runs of 120 samples on 2026-11-16. The macOS arm64 cell that produced 107.9 ms
   cannot be re-run from here; when CI next runs, that cell becomes the second data point and this ADR is amended with
   it rather than being assumed to have predicted it.
2. **The bound assumes the process is scheduled at all.** If a container is frozen (cgroup throttle, swap storm,
   suspend) for more than ~78 ms, the tick cannot fire and the outage exceeds the budget. No in-process watchdog can fix
   that; the 4.7× margin is what makes it unlikely rather than impossible, and this is the residual risk the row carries.
3. **The GIL.** When the proxy is supervised inside the *application's* interpreter (the SDK attach path), a C extension
   that holds the GIL for longer than the tick delays detection by that long. Pure-Python code does not: the tick's
   `Event.wait` and the supervision loop release the GIL, and the rebind itself runs on the proxy's own event loop.
   An application that blocks the GIL for 100 ms will miss the budget, and nothing in this design can promise otherwise.
4. **The measurement has its own granularity** (0.5 ms polling, recorded as `poll_s` in the JSON); it cannot resolve a
   smaller outage than that, which is far below every number quoted here.
5. **A rebind that loses the port is a different failure** and is counted separately (`port_moved`): zero in both
   columns, but a port stolen between the close and the bind would move a child's traffic, and that is a bug whatever the
   latency.

## 7. First CI data point outside this container (2026-11-16)

Run `43880930567` on `66830f2` ran the whole suite, so §6's first limitation got an answer — a partial one, and it is
recorded as partial:

| cell | result |
|---|---|
| `py3.11 / macos-latest` | **pass** — the suite, including `test_every_watchdog_rebind_lands_inside_the_budget_not_the_median_of_them`, which asserts **every** watchdog sample of 8 breaks against `REBIND_BUDGET_MS` |
| `py3.12 / macos-latest`, `py3.13 / macos-latest` | **cancelled with no runner assigned and no step run** — no evidence either way |
| ubuntu 3.11/3.12/3.13 | pass |
| windows 3.11/3.12/3.13 | failed, on two of the *new launcher tests*, not on this row (defect 43) |

So the bound is no longer a Linux-only claim: one macOS arm64 cell passes the per-sample assertion that this ADR's
§1 defines. What it still is **not**: a distribution. The test prints its eight samples only when it fails, so a pass
says "every sample was under 100 ms on that runner", not "the worst sample was 21 ms there". The macOS numbers quoted
in §3 remain Linux numbers, and this table says which cell produced which claim — no more.

## 8. What the matrix says now (2026-11-16, runs 43888826452 and 43889007689)

The Windows cells are green for the first time: `py3.11`, `py3.12` and `py3.13 / windows-latest` all pass the whole
suite, which includes this ADR's `test_every_watchdog_rebind_lands_inside_the_budget_not_the_median_of_them`. So the
per-sample assertion (§1) now holds on two operating systems and three interpreters each, not one kernel.

What that still does not buy, stated plainly because §6 asked for it:

* **the macOS cells did not run.** All three were cancelled with no runner assigned and no step executed. The macOS
  evidence is still the single `py3.11` pass in run 43880930567. Three green Windows cells do not repair that gap, and
  this ADR will not claim "macOS/Windows green" from a run where the macOS jobs never started.
* **a pass is a bound, not a distribution.** The test prints its eight samples only when it fails, so a green cell says
  "every sample was under 100 ms on that runner", not what the slowest one was. The numbers in §3 remain Linux numbers.
* **the scheduling assumption is unchanged.** A hosted runner that can stall a thread for longer than the budget would
  fail this test rather than hide the failure — that is the point of asserting every sample — but "it failed" on a
  shared CI runner would still need to be read as a fact about that runner, not about a user's machine. §6's first
  limitation (one machine, one container) is now weaker; the rest stand.
