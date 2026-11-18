# Release blockers, with the evidence that puts them here

Written on branch `arena/4e84d6ee-blackbox` (from `06b32a5`, the merge of PR #17). Every entry below was **reproduced on
this branch**, and each carries the command or the artifact that shows it. Nothing here is a guess, an old note carried
forward, or a claim about a machine that was not available: where a claim is unmeasured, the entry says so and says what
would measure it.

Order is by what a real user hits first, not by ticket number. **B1 was fixed on this branch** (defects 66 and 67) after
the list was first written, then **B2 and B3** (defects 68 and 69); the ids are kept stable so a comment, a defect entry
or `docs/STATE.json` that names "B4" still points at the same thing. What remains open is **B4, B5, B6 and B8** — two of
which only the release machine can settle (B6) and one of which is the owner's call on graph edges (B4).

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

**The unknown-convention half is answered now (defect 68).** A stream whose provider this recorder does not recognise is
still never marked `partial` — that remains the rule, because a generic `text/event-stream` has no terminator in its
specification and inventing one would be a guess about somebody else's protocol. What changed is that the absence of a
convention is no longer *indistinguishable from a verified ending*: the ending is recorded in three states, and the third
one says so.

| recorded `ending` | meaning | `partial` | what a report says |
|---|---|---|---|
| `terminated` | a marker from this provider's own convention was seen | `False` | the stream finished (with the provider named) |
| `unterminated` | the convention is known and its marker is missing | `True` | **observed**: the stream ended before the provider finished it |
| `unverified` | no convention is known for this path | `False`, and no claim either way | **cannot tell**: whether the stream finished is not recorded |

Supported conventions, each a row in `TERMINATORS` and each tested by provider: OpenAI chat/legacy `data: [DONE]`,
OpenAI Responses `response.completed`, Anthropic `message_stop`, Gemini `"finishReason"`, Ollama `"done": true` (chat and
generate). A marker from *another* provider's convention does not count, and a non-streaming JSON body is not a stream.
`tests/test_stream_termination.py` (23) and one new `diagnose` test (38 in that file) hold it.

**What is still the owner's call.** Whether a `partial` call should also change the **run status** from `recovered` to
something a user reads as "incomplete"; today it is a finding in the report, not a status.

---

## B2 — A run whose calls were never recorded reports success and shows an empty capsule — FIXED on this branch

**What a user hits.** They export `OPENAI_BASE_URL` (a gateway, a corporate proxy, or just their own test server) and run
`blackbox run -- python app.py`. The launcher **adopts** a base URL the user set instead of overwriting it — correct, and
documented — so the traffic never reaches the recorder. The run prints `recovered … root …`, `blackbox ls` lists a
capsule, `diagnose` writes a report, and the capsule contains five frames: `RUN_START`, two `CHAIN`, `RUN_END`,
`SEG_END`. Nothing about the call the user wanted explained.

**Evidence (before and after, same run):**

```console
$ blackbox ls
CAPSULE             RUN                 STATUS                   EVENTS  SIZE
c_1f5b6b5267f2172a  r_e072267e38ca3fc3  recovered/PROCESS_DEATH  5       9.8 KiB   # the empty tape
c_9ea910f9c2de3ca1  r_e072267e38ca3fc3  recovered/PROCESS_DEATH  11      14.5 KiB   # the app's
```

before: `capture={'coverage': 'FULL', 'providers': []}` and the report said *completeness `full`*, with no mention that
nothing was recorded. After defect 65: `capture={'coverage': 'NONE', 'model_calls': 0}`, and `diagnose` prints

```
- no model call was recorded in this capsule, so nothing here describes a request or a response
```

with the cause and the fix (`blackbox run --force-proxy` names every variable it displaces).

**Fixed (this branch).** Two halves, because the trap had two exits:

* **The launcher says it.** After the summary line, when a run recovered capsules, had a proxy, and recorded **zero model
  calls**, `blackbox run` prints one sentence and no more. Measured on a real CLI run (`blackbox run --data-dir … sh -c
  'echo hi'`):

  ```
  blackbox: this run recorded no model call — the 1 capsule of run r_d188348b679e0c11 holds no request and no
  response. If this command talks to a model API, its base URL was not the recorder's (a base URL your environment or
  code sets is adopted as an upstream rather than overwritten), and `blackbox run --force-proxy` points every base-URL
  variable at the recorder and names each one it displaces. A command that makes no model call is fine; this line
  describes the run, not a failure.
  ```

  and the same command with a client that reaches the recorder prints nothing (`model_calls: 1`). The count comes from
  each capsule's own manifest (`RunResult.captured`), not from the proxy's counters, because `blackbox ls` will show
  those capsules to the user tomorrow and two answers to the same question is the thing this project keeps refusing.
* **The exit status is the child's.** The warning is a sentence on the run's output stream; it does not change the exit
  code, because the child's exit code belongs to the child and a recorder that fails a successful command is a recorder
  people uninstall. That was the decision the blocker asked for and it is taken here.

**Tests.** `tests/test_launcher_capture_warning.py` (5): a run that made no model call says so and does not read as an
error; the sentence names the cause and the flag that fixes it; a run that reached the recorder is not warned about; no
warning without a proxy; and the singular/plural wording is asserted so "1 capsule … holds" cannot regress. End-to-end,
on this box, through the installed CLI: warning present for `sh -c 'echo hi'`, absent for a `urllib` client through the
recorder.

---

## B3 — Concurrency had no policy at the descriptor ceiling — DECIDED and FIXED on this branch

**What a user hits.** A parallel batch (an agent fanning out 200 calls, or 400 with retries) against a slow upstream. The
listener stops accepting, the child's requests wait with no error, and asyncio logs a traceback per attempt that nobody
reads.

**Measured before** (`ulimit -n` soft 1024, hard 524288, 2 vCPUs; client and prober in their own processes, so the
numbers are the proxy's): at 450 concurrent in-flight requests the proxy's table was full — `socket.accept() out of
system resource` — and **a new request during the burst timed out after 4 s with no answer**. Raising the soft limit to
what the kernel already grants (`proxy/limits.py`, to 65536) moved the cliff: the same burst held 1366 descriptors and
the probe was served in 2 ms. That half is not a decision, and it is still the first thing the proxy does.

**The decision, and why this one.** Of the three options the blocker named, **backpressure** — with an explicit ceiling
above it — is what this branch implements, because the proxy's promise is *pass-through*: a recorder that refuses a
request the provider would have answered has broken the thing it exists for. The rules, all in `proxy/server.py`:

| situation | answer | cost |
|---|---|---|
| request in flight already equals `MAX_INFLIGHT` (200) | the request **waits** for a slot; its socket stays open | the wait is counted (`stats.queued`) and its length recorded |
| the wait exceeds `QUEUE_WAIT_S` (60 s) | `503` + `x-blackbox-error: queue-timeout: …` | never leaves a request parked forever |
| more than `MAX_CONNECTIONS` (512) clients held open | `503` + `x-blackbox-error: connection-limit: …` in milliseconds | the one case the recorder cannot serve, so it says so |

Why **200** and **512**, rather than a flag: the caps are derived from a descriptor budget, not a preference. One
descriptor per held connection plus one per in-flight request is the whole model, so the worst case is
`512 + 200 + 96` (the 96 counts the listener, the idle pool at `2 * routes`, the tape, and stdio) = **808**, under the
smallest default soft limit this project meets (1024 on Linux and macOS) — which means the caps hold *even on a host
where `limits.py` could not raise anything*, Windows included. `tests/test_proxy_backpressure.py` asserts that
arithmetic so the numbers cannot drift apart from the reason for them. There is deliberately no `--max-connections` yet:
a knob whose safe range is one budget interval invites the exact failure this replaces, and adding it is a decision with
a validation story of its own.

**Measured after** (`bench/bench_connection_limit.py` → `bench/results/2026-11-16-b3-connection-limit.json`; same 2-vCPU
Linux sandbox, origin sleeping 300 ms per request, soft limit raised the way a launcher raises it):

| measurement | result |
|---|---|
| 450 concurrent requests | **450 of 450 answered 200**, 0 refused, 444 queued, longest queue wait 887 ms, origin peak 192 in flight |
| a probe issued during that burst | **200**, in 1053.8 ms — it waited ~0.9 s for a slot instead of timing out after 4 s with nothing |
| 96 clients beyond `MAX_CONNECTIONS` | **96 of 96 answered `503`** with `connection-limit: at most 512 connections are held at once`, median **4.4 ms**, max 7.7 ms |

**What a user reads.** A run that hit either rule says so once, after the summary — the same discipline as B2's sentence:
`the recorder answered 2 requests itself instead of passing them through (connection-limit: 2); 3 requests waited for a
slot at the 200-request ceiling, the longest 812 ms`, followed by the note that the numbers are a descriptor budget.
`ProxyStats` carries `queued`, `max_queue_wait_ms` and `refused_reasons` so the sentence is derived, not guessed.

**Tests.** `tests/test_proxy_backpressure.py` (5), with the ceilings turned down so a test *is* a real burst at a real
ceiling: six requests against `max_inflight=2` are all served, two at a time, none refused; a connection past
`max_connections=2` gets its `503` in under two seconds; a wait past a 0.3 s bound is answered, not stalled; a client
that connects and says nothing past the limit is told and closed; and the descriptor arithmetic above. `ProxyServer` takes
the three numbers as parameters so the behaviour is testable without 512 sockets; the defaults are the documented ones.

**Still the owner's call.** Whether the caps should be configurable, and whether a `503` the recorder issued should be
counted in the *capsule* (today it is in `stats` and in the run's sentence, not in a frame). Neither blocks: both
defaults are safe and both are said out loud.

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
