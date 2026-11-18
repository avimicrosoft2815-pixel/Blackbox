# Release blockers, with the evidence that puts them here

Written on branch `arena/4e84d6ee-blackbox` (from `06b32a5`, the merge of PR #17). Every entry below was **reproduced on
this branch**, and each carries the command or the artifact that shows it. Nothing here is a guess, an old note carried
forward, or a claim about a machine that was not available: where a claim is unmeasured, the entry says so and says what
would measure it.

Order is by what a real user hits first, not by ticket number. **B1 was fixed on this branch** (defects 66 and 67) after
the list was first written; the ids are kept stable so a comment, a defect entry or `docs/STATE.json` that names "B4"
still points at the same thing.

---

## B1 — A model stream the provider cut short is recorded as **complete** — FIXED on this branch, one policy question left

**What a user hits.** Their streaming call stops after three chunks. The SDK sees a clean end of body and raises
nothing; the script exits 0 and prints a short answer. They open a capsule to find out why. The capsule says the call
completed.

**Reproduction** (mock provider truncated after 3 of 6 chunks, real `openai` client, wheel install):

```console
$ blackbox run -- python app_stream.py
chunk token ... chunk token ... chunk token          # 3 of 6, then a clean EOF: exit 0
$ blackbox diagnose capsules/ --out incident.md
Findings by strength: 1 observed.
No finding reached `observed` or `likely` for an **interruption**: nothing in these frames indicates that anything
was cut short.
```

and the frames themselves:

```
MODEL_REQUEST  {'bbx.model.stream': True, 'bbx.model.partial': False, ...}
MODEL_CHUNK    {'bbx.chunk.bytes': 92}
MODEL_RESPONSE {'bbx.model.chunks_raw': 2, 'bbx.model.partial': False, ...}
```

**Cause, located.** `src/blackbox/cap_sdk/tee.py`, `TeeStream.__iter__`: a clean `StopIteration` calls
`_finish(completed=True)`, so `partial` stays `False` no matter what the SSE body did or did not contain. A stream that
ends without OpenAI's `data: [DONE]` or Anthropic's `message_stop` is byte-identical, from the seam's point of view, to
one that finished. The proxy-side relay has the same blind spot for a chunked body that ends without its terminator.

**Why it blocks a release.** This is the headline promise — *"a stream that was cut short is recorded as cut short"* —
and it is the one failure mode a user cannot see for themselves. `test_day14_kill.py` proves it for a **killed process**
(the tail is torn, `RUN_END` is missing) but not for a **truncated upstream**, which is the ordinary case in the wild
(provider timeout, gateway 502 mid-stream, dropped connection).

**Fixed (defects 66 and 67).** `TerminatorWatch` in `cap_sdk/tee.py` keeps the last 128 bytes of the stream, knows the two
conventions it can vouch for (`[DONE]` for `/chat/completions` and `/completions`, `message_stop` for `/messages`), and on
a clean end **without** the marker sets `partial = True` and records why in `ModelCall.end_reason`. `diagnose` now emits
an **observed** finding for a response whose own `partial` flag is set, quoting that reason, and the report's headline
reads *"The evidence shows that a model stream ended before the provider finished it."* Same reproduction afterwards:

```
MODEL_RESPONSE  {'partial': True, 'end_reason': 'the stream ended without the provider terminator ([DONE]); the body was
                 closed by the other end, not finished', ...}
report: Findings by strength: 2 observed.
### 2. the response stream at node 0 ended before the provider finished it
```

It also surfaced a second defect on the way (67): `diagnose/evidence._decode` read only CBOR, so **every model frame
reached the rules as an empty dict** and the timeline printed `MODEL_REQUEST — 312 B` with no provider, path, model or
`partial`. It now tries JSON first, as `cap/export_jsonl` always has, and the timeline shows
`provider=127.0.0.1; model=gpt-4o-mini; path=/v1/chat/completions; stream=True; partial=False; capture_seam=httpx-hooks`.

**What is still the owner's call.** (1) A *generic* `text/event-stream` with an unknown convention is deliberately not
marked — the specification has no terminator, so marking one would be a guess at somebody else's protocol. (2) Whether a
`partial` call should also change the **run status** from `recovered` to something a user reads as "incomplete"; today it
is a finding in the report, not a status.

---

## B2 — A run whose calls were never recorded reports success and shows an empty capsule

**What a user hits.** They export `OPENAI_BASE_URL` (a gateway, a corporate proxy, or just their own test server) and run
`blackbox run -- python app.py`. The launcher **adopts** a base URL the user set instead of overwriting it — correct, and
documented — so the traffic never reaches the recorder. The run prints `recovered … root …`, `blackbox ls` lists a
capsule, `diagnose` writes a report, and the capsule contains five frames: `RUN_START`, two `CHAIN`, `RUN_END`,
`SEG_END`. Nothing about the call the user wanted explained.

**Evidence (before and after this branch's fix, same run):**

```console
$ blackbox ls
CAPSULE             RUN                 STATUS                   EVENTS  SIZE
c_1f5b6b5267f2172a  r_e072267e38ca3fc3  recovered/PROCESS_DEATH  5       9.8 KiB   # the empty tape
c_9ea910f9c2de3ca1  r_e072267e38ca3fc3  recovered/PROCESS_DEATH  11      14.5 KiB   # the app's
```

before: `capture={'coverage': 'FULL', 'providers': []}` and the report said *completeness `full`*, with no mention that
nothing was recorded. After (defect 65, this branch): `capture={'coverage': 'NONE', 'model_calls': 0}`, and `diagnose`
now prints

```
- no model call was recorded in this capsule, so nothing here describes a request or a response
```

with the cause and the fix (`blackbox run --force-proxy` names every variable it displaces).

**Still blocking:** the **launcher** is silent. A run that recorded zero model calls while the proxy was up exits 0 and
prints `recovered`, exactly like a run that recorded a hundred. A one-line warning at exit — *"no model call reached the
recorder in this run; if your app sets its own base URL, `--force-proxy` points it here"* — turns the trap into a
message. That is a small change to `rec/launcher.py` and it needs a decision about whether a zero-capture run should
also change its exit status (recommended: no — the child's exit code is the child's, and the warning belongs on stderr).

---

## B3 — Concurrency is bounded by the descriptor table, and nothing decides what happens at the bound

**What a user hits.** A parallel batch (an agent fanning out 200 calls, or 400 with retries) against a slow upstream. The
listener stops accepting, the child's requests wait with no error, and asyncio logs a traceback per attempt that nobody
reads.

**Measured on this box** (`ulimit -n` soft 1024, hard 524288, 2 vCPUs; client and prober in their own processes, so the
numbers are the proxy's):

| state | 450 concurrent in-flight requests | a new request during the burst |
|---|---|---|
| before | proxy's table full: `socket.accept() out of system resource`, `/proc/self/fd` unreadable | **timed out after 4 s**, no answer |
| after (soft limit raised to 65536) | 1366 descriptors held, all 450 kept open | **served in 2 ms** |

**Fixed on this branch — the part that is not a decision.** `src/blackbox/proxy/limits.py` raises the process's soft
`RLIMIT_NOFILE` to what the kernel already grants (bounded by the hard limit, capped at 65536, no-op on Windows), on
proxy start, recorded as `ProxyThread.fd_limit`. Tests: `tests/test_proxy_limits.py` (4) prove the raise, the Windows
answer (`None`), the arithmetic (a 40-connection burst against a deliberately tiny 64-descriptor budget), and the target.

**Still blocking — the part that is a decision.** There is no cap and no backpressure policy. Raising the limit moves
the cliff; it does not remove it, and the cliff is *silent*: a child that is queued behind the limit cannot tell that
from a slow provider. The owner must choose one:

1. **backpressure (recommended)** — an `asyncio.Semaphore` of `--max-connections` in `ProxyServer._handle`, so beyond
   the cap the client waits with its own socket open and nothing is refused. Peak descriptors become `clients + cap`
   instead of `2 × clients`; the capsule records the wait. No request the upstream would have served is lost.
2. **explicit refusal** — 503 with `x-blackbox-error` past the cap. Cheaper to reason about, but a recorder that refuses
   a request the provider would have answered has broken the promise the proxy exists for; it also needs a new event so
   the capsule says it happened.
3. **nothing, but say it** — leave the cliff, and make `diagnose`/`run` report the descriptor ceiling and whether the run
   came near it.

---

## B4 — P3B-03's acceptance: 0 node-level edges

Unchanged and unsoftened. A shared trace id is not an edge; the capsule's `graph.json` records nodes and statuses but
**no node-to-node edges**, so the "why did this step fail" question cannot be answered from the graph. Evidence:
`bench/results/2026-11-16-p3b03-acceptance.json`. The rebind half of the same ticket now passes on a request-level
measurement (worst 53.98 ms, 0 of 240 over the 100 ms budget — `bench/results/2026-11-18-rebind-instrumented.json`), and
one half is not the ticket.

## B5 — OTLP importers other than Phoenix are untested

Phoenix 21.0.0 accepts `otlp/spans.pb` (200, read back intact) and rejects `otlp/spans.json` (its receiver is
protobuf-only). Langfuse, the OpenTelemetry Collector's OTLP/JSON receiver and Phoenix's gRPC port 4317 have never been
exercised, and none of them can be installed offline here. Evidence: `bench/results/2026-11-16-otlp-pb-importer.json`.

## B6 — Two rows that only the release machine can settle

* **P3B-07's bench-box cold start.** The gate is ≤ 40 ms p95 on the bench box. This branch measures **p50 30.3 / p95 38.3
  ms** for `blackbox ingest` on a 2-vCPU Linux sandbox after defect 64, from a wheel install — inside the gate, on the
  wrong machine. The row stays owed until it is run on the named box.
* **The rebind numbers are local.** No CI runner contributes a rebind sample, and none will: `bench/bench_rebind.py`
  needs one busy process per CPU and a quiet loop, which a shared runner cannot give. macOS is now measured *for the
  test suite* (ten of ten on `ad3a783`, `f46dac1`, `66fc174`), never *for the bench*.

## B7 — `diagnose capsules/` picked the newest capsule, which may be the wrong one of the run's two — FIXED

Every `blackbox run` of a Python child leaves **two** capsules with the **same run id**: the child's tape and the SDK
helper tape (`… and 1 more tape (sdk)`). They are distinguished only by event count (11 vs 5 in the run above). The
directory form of `diagnose` — the form the README shows — takes the newest file, so the report can describe the
five-frame helper tape and say nothing about the call. It happened to pick the app capsule in both reproductions here,
which is luck, not a rule.

**Fixed.** `diagnose <dir>` now picks *the newest run's capsule that has something to say*: inside the newest run's
capsules the most model calls wins, then the most events, and it prints what it passed over and why —

```
reading capsules/c_372e….bbx (1 model call(s), 11 event(s); passed over capsules/c_5a96….bbx: 0 model call(s), 5
event(s) — name a capsule explicitly to diagnose a different one)
```

A capsule outside the newest run is never preferred, so "the newest run" still decides *which run*; the choice inside it
is now about content rather than mtime. Test: `test_a_directory_with_a_run_s_two_capsules_picks_the_one_with_the_model_calls`
(the helper tape is written last, so mtime alone would pick it).

## B8 — `verify` takes exactly one capsule, and the README's habit is a glob

`blackbox verify capsules/*.bbx` is `error: unrecognized arguments`. The README shows a single path, so this is a paper
cut rather than a lie, but it is the command a user types after `ls` shows two capsules. Either accept many capsules or
say so in `--help`.

---

## Fixed on this branch, with the numbers

| what | before | after | test |
|---|---|---|---|
| `diagnose <dir>` with a run's two capsules (B7) | newest file wins (the 5-frame helper tape) | the capsule with the model calls, with what was passed over named | `tests/test_diagnose.py` |
| a stream truncated by the provider (defects 66/67) | `partial: False`, "nothing was cut short", timeline with no model attributes | `partial: True` + `end_reason`, an observed finding, headline names it | `tests/test_stream_termination.py` (11), `tests/test_diagnose.py` (+4) |
| `blackbox ingest` cold start (the hook path, per tool call) | p50 **100.8**, p95 108.8 ms | p50 **30.3**, p95 **38.3** ms | `test_the_ingest_command_costs_the_agent_tens_of_milliseconds` (marginal cost, not a runner's mood), `test_the_ingest_path_imports_only_what_it_uses` |
| 450 concurrent requests (descriptor ceiling) | accept fails, in-flight probe **4 s timeout** | all served, probe **2 ms** | `tests/test_proxy_limits.py` (4) |
| an empty capsule's manifest | `coverage: FULL`, completeness `full`, silence | `coverage: NONE`, `model_calls: 0`, an observed finding + the `--force-proxy` hint | `tests/test_diagnose.py`, `tests/test_capsule.py` |
| `redaction.content_mode` | `full` (a lie) for a capsule whose frames say `hash` | `hash` / `mixed` / `unknown`, derived from the frames | same |

Defect entries: `docs/defects.md` 64 (hook path imports), 65 (a manifest that described a recording it had not made),
62 (the descriptor ceiling, still open for the policy half), 61 and 63 (the previous branch's teardown and probe fixes).

---

## Not blockers (known limits, already documented)

Content is hashed by default (`content_mode: hash`), so prompt and response **text** is not in the capsule unless the
user asks for it; there is no price table, so cost is `unknown_cost: true` rather than a confident zero; the repair path
is approval-gated and applies nothing without a token; Windows' symlink guarantee is check-then-write, not atomic. All
four are in the README's limits section and pinned by `tests/test_readme.py`.
