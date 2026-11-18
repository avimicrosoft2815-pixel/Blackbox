# Release blockers, with the evidence that puts them here

Started on branch `arena/4e84d6ee-blackbox` (from `06b32a5`, the merge of PR #17) and carried into
`arena/4572f080-blackbox` (from `f4a9abb`, the merge of PR #18). Every entry below was **reproduced on
this branch**, and each carries the command or the artifact that shows it. Nothing here is a guess, an old note carried
forward, or a claim about a machine that was not available: where a claim is unmeasured, the entry says so and says what
would measure it.

Order is by what a real user hits first, not by ticket number. **B1 was fixed on this branch** (defects 66 and 67) after
the list was first written, then **B2 and B3** (defects 68 and 69); the ids are kept stable so a comment, a defect entry
or `docs/STATE.json` that names "B4" still points at the same thing. What remains open is **B4, B5 and B6** — two of
which only the release machine can settle (B6, and the macOS cells it cannot acquire) and one of which is the owner's call
on graph edges (B4). B8 was closed after the first review round, when `verify` learned to take many capsules. **B4's fact
changed on 2026-11-18**: node-level edges are recorded and measured on a real instrumented client, so what is left of B4 is
the owner's tick and the parts that are honestly still unmeasured (see B4).

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
  blackbox: this run recorded no model call — the 1 capsule of run r_a0a8950b142d52c4 holds no request and no
  response. That is not a failure by itself: a command that talks to no model API is exactly this. If this one was
  meant to talk to a model API, the base URL is one possible cause (a base URL your environment or code sets is adopted
  as an upstream rather than overwritten, so the traffic never arrives), and `blackbox run --force-proxy` points every
  base-URL variable at the recorder and names each one it displaces. A call that never left the process — a mock, or a
  client that ignores the variable — would look the same from here. This line describes the run, not a verdict on it.
  ```

  **The wording is deliberate, and it was corrected after review.** An earlier draft said *"its base URL was not the
  recorder's"*, which is a diagnosis this recorder cannot make: it writes the capsule it is handed and cannot see the
  child's network, and a command that talks to no model API produces byte-identical evidence. The sentence now offers the
  base URL as **one possible cause**, names the other one (a call that never left the process — a mock, a client that
  ignores the variable), and calls itself a description rather than a verdict. The test asserts the hedge words and
  asserts that the three certain phrasings are **absent**, so the stronger claim cannot come back by edit.

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
| requests in flight already equal `MAX_INFLIGHT` (200, or the planned number) | the request **waits** for a slot; its socket stays open | the wait is counted (`stats.queued`) and its length recorded |
| the wait exceeds `QUEUE_WAIT_S` (60 s) | `503` + `x-blackbox-error: queue-timeout: …` | never leaves a request parked forever |
| more than `MAX_CONNECTIONS` (512, or the planned number) clients held open | `503` + `x-blackbox-error: connection-limit: …` and a body line saying what to do | the one case the recorder cannot serve, so it says so |

**The caps are planned per host, not hard-coded.** One descriptor per held connection plus one per in-flight request is
the whole model, so `512 + 200 + 96 = 808` (the 96 counts the listener, the idle pool at `2 * routes`, the tape and
stdio) fits a host that allows 1024 — but a macOS shell starts at **256** and a container can have any hard limit at all,
and promising those numbers to such a host would be the same silent cliff in a smaller size. `plan_caps()` therefore reads
the budget the process actually has (`limits.fd_budget()`, *after* the raise attempt) and shrinks both caps to fit, or
says that the budget is below what the proxy needs and stops at floors (16 connections, 8 in flight):

| budget the process has | connections | in flight | what the run's sentence says |
|---|---|---|---|
| 65536 (this sandbox, after the raise) | 512 | 200 | "512 connections + 200 in flight + 96 for the process fits the 65536 descriptors this host allows" |
| 1024 (stock Linux/macOS soft limit) | 512 | 200 | "fits the 1024 descriptors this host allows" |
| 256 (a macOS shell that refused the raise) | 96 | 64 | "this host allows 256 descriptors, so the caps are 96 connections + 64 in flight + 96 for the process" |
| 200 (a hard limit nobody can pass) | 62 | 42 | same sentence, with those numbers — **measured end to end**: 8 concurrent requests, 8 served `200` |
| 64 (below the floors) | 16 | 8 | "this host allows only 64 descriptors, below the 120 the proxy needs: the caps are the floors … and the 503 at them is what protects the process" |
| 320 with **50 already open** | 104 | 70 | "this host allows 320 descriptors and 50 are already open, so the caps are 104 connections + 70 in flight + 96 for the process" |
| `None` (**Windows**: no readable budget) | 512 | 200 | "this platform publishes no descriptor budget, so 512 connections and 200 in flight are policy numbers; the 503 at the ceiling is what bounds the process" |

That last row is the platform review in one line: on Windows the arithmetic above does not exist, so the caps are stated
as policy rather than claimed as a budget, and the explicit `503` remains what bounds the process. `ProxyStats.caps`
carries all four numbers plus the sentence, so nothing about the ceilings has to be reconstructed from documentation.
`tests/test_proxy_backpressure.py` (7) asserts the whole table including Windows and the floors;
`tests/test_proxy_limits.py` (7) reads the real budget in a subprocess at 200 descriptors and asserts the plan fits.

There is deliberately no `--max-connections` yet: a knob whose safe range is one budget interval invites the exact
failure this replaces, and adding it is a decision with a validation story of its own.

**Measured after** (`bench/bench_connection_limit.py` → `bench/results/2026-11-16-b3-connection-limit.json`; same 2-vCPU
Linux sandbox, origin sleeping 300 ms per request, soft limit raised the way a launcher raises it — re-run for this
revision, so the file and the sentence below are the same measurement):

| measurement | result |
|---|---|
| 450 concurrent requests | **450 of 450 answered `200`**, 0 refused, 383 queued, longest queue wait 735 ms, origin peak 165 in flight |
| a probe issued during that burst | **`200` in 948.9 ms** — it waited for a slot instead of timing out after 4 s with nothing |
| 96 clients beyond `MAX_CONNECTIONS` | **96 of 96 answered `503`** with `connection-limit: the recorder already holds its 512 connections`, median **4.0 ms**, max 9.6 ms |

**What a user reads, and why the two are not the same sentence.** A **delay** is not a **rejection**, and a user cannot
act on them identically, so the run's sentence names them separately and never adds them together:

* `delay, nothing lost: 3 requests waited for one of the recorder's 200 in-flight slots, the longest 812 ms; every one of
  them was passed through`
* `rejection: the recorder answered 2 requests itself instead of passing them through (connection-limit: 2); those never
  reached a provider, and the client was given a 503 naming the ceiling, so a retry is the action`

followed by the ceilings and where they came from (`derived from this host's 1024-descriptor budget`, or the
no-budget-on-this-platform sentence). The request that *is* rejected gets a body a developer can act on as well as the
header a program can switch on:

```
503 connection-limit: the recorder already holds its 512 connections
this is the recorder's ceiling, not the provider's: retry when its in-flight requests finish, or run fewer at once
```

and the queue case says `the requests already in flight are slower than the recorder's queue allowance: retry, or run
fewer at once`. `ProxyStats` carries `queued`, `max_queue_wait_ms`, `refused_reasons` and `caps`, so every clause of every
sentence is derived from what happened rather than guessed from a constant.

**Overload, measured at the planned numbers rather than hand-picked ones** (`tests/test_proxy_backpressure.py`, 14):

| case | result |
|---|---|
| 80 concurrent requests at the 256-descriptor plan (96 connections / 64 in flight) | **80 of 80 answered `200`**, 0 refused, 73 queued, longest wait **50 ms**, 119 ms wall |
| 12 concurrent requests on a 320-budget host with **50 descriptors already open** | **12 of 12 answered `200`**, plan 104 / 70, **peak 87 descriptors** — under the budget |
| 450 concurrent requests on the documented pair (1024-budget host) | 450 of 450 `200`, 383 queued, longest wait 735 ms (the bench above) |
| a client that closes **while queued** for the only slot | the slot is returned and the next request is served `200`; nothing refused |
| a client that closes **at the connection ceiling**, before reading its `503` | the connection count drains to 0; the next request is served `200`; `connection-limit: 1` |
| a client that closes **mid-stream** with one in-flight slot | `client_gone` recorded; the next request is served `200` — no slot leaked |

**Limitation, stated rather than implied.** A client that disappears while *queued* cannot be noticed until its turn
comes, because the recorder is parked on the semaphore and not reading that socket. The slot is returned then, the
departure is counted, and the abandoned request **may already have reached the provider** — the recorder cannot un-send
it. Mid-stream departures are recorded as `client_gone` (§14.4a) and cost no slot. `README.md` says both in the
"what this does not do" list.

**Tests.** `tests/test_proxy_backpressure.py` (14): the ceilings turned down so a test *is* a real burst at a real
ceiling (six requests against `max_inflight=2` all served, two at a time, none refused); a connection past
`max_connections=2` getting its `503` in under two seconds **including the body line quoted above**; a wait past a 0.3 s
bound answered, not stalled; a client that connects and says nothing past the limit told and closed; the descriptor
arithmetic; the `plan_caps` table row by row; the plan's own arithmetic when the process already holds descriptors; a
server built on a small host that uses the plan and says so; and the three abrupt-disconnect cases above.
`ProxyServer` takes the numbers as parameters so the behaviour is testable without 512 sockets; left unset they are
derived, never assumed.

**Still the owner's call.** Whether the caps should be configurable, and whether a `503` the recorder issued should be
counted in the *capsule* (today it is in `stats` and in the run's sentence, not in a frame). Neither blocks: both
defaults are safe and both are said out loud.

---

## B4 — P3B-03's acceptance: node-level edges are now **measured**, the tick is still the owner's call

**The blocker's stated fact was `node_level_edges_recorded: 0`, and that number is no longer zero.** The proxy now opens a
span of its own for every request it forwards (`NODE_OPEN` … `NODE_CLOSE`, `kind: "proxy_call"`, `owner: "proxy"`, a slot
from its own band) and tags that call's frames with the slot; the join stays the one both writers wrote, the
`x-blackbox-call` id. Evidence, four real clients under `blackbox run` against a real loopback origin
(`bench/results/2026-11-18-b4-node-edges.json`):

| client | capsules, roots, one run id | `node_join` | node-level edges |
|---|---|---|---|
| `urllib` (no seam) | 2, all ok, one | `unknown` | **0** |
| Node v22.22.3 (no seam) | 1, ok, one | `unknown` | **0** |
| `openai` 3.51.0 as shipped | 2, all ok, one | `partial` | **0** |
| `openai` 3.51.0 inside `api.run` | 2, all ok, one | **`exact`** | **2** (each tape's graph lists the join from its side) |

The instrumented case's pair is app node `1` ↔ proxy span `16384`, call id `5eb5b57ced07034f`, on both tapes' graphs, with
both capsules passing their own integrity root and `graph.json` recomputed from raw frames by `capsule verify`. The
uninstrumented rows are the **negative control**: their frames carry fingerprints and timestamps, they are recorded inside
no app node, and they produce no node-level edge — nothing is joined by time or by fingerprint.

What this does **not** settle, and why the blocker stays open as the owner's decision rather than mine:

* **Node clients cannot produce a node-level edge** — `api.run` has no Node equivalent, so the Node row is `unknown` at
  node level. That is a missing feature, reported as such, not a passing row.
* **Ambiguity is refused, but only in tests** — an id recorded twice on a side yields `level: "call"`, `ambiguous`,
  `candidates: 2` and no node (`tests/test_proxy_spans.py`), and no live client produces a duplicate id without being
  told to.
* **macOS is unmeasured** for every row above.
* Shape (b) of `docs/node-edges.md` (a hook-to-seam node join) and a launcher-owned process span are still proposals.

`docs/node-edges.md` is now an implementation note with the measurement, not a proposal. The rebind half of the same
ticket passes on a request-level measurement (worst 53.98 ms, 0 of 240 over the 100 ms budget —
`bench/results/2026-11-18-rebind-instrumented.json`). Defects 75 and 76 are the two bugs measuring this uncovered.

## B5 — OTLP importers other than Phoenix: **two more measured, five still unmeasured and named**

Phoenix 21.0.0 accepts `otlp/spans.pb` (200, read back intact) and rejects `otlp/spans.json` (its receiver is
protobuf-only) — `bench/results/2026-11-16-otlp-pb-importer.json`. Measured on 2026-11-18
(`bench/results/2026-11-18-otlp-importers.json`), with the capsule's own bytes and the versions that read them:

| importer / consumer | version | setup | result |
|---|---|---|---|
| the official OTLP message definitions | `opentelemetry-proto` 1.45.2, `protobuf` 7.36.2 | `ParseFromString` on `otlp/spans.pb` | **accepted**: 3 spans, names `bbx.run` / `invoke_agent agent` / `chat gpt-4`, trace id `50444de0…`, every `parent_span_id` resolves to a span in the message |
| the same definitions, proto3-JSON | `protobuf` 7.36.2 `json_format.Parse(..., ignore_unknown_fields=False)` | `otlp/spans.json` | **accepted**: no unknown fields, 3 spans, same names — so the JSON part is the message OTel defines, not merely JSON we invented |
| an OTLP/**gRPC** `TraceService` (4317 semantics) | `grpcio` 1.85.3 + the official servicer | loopback gRPC server, capsule bytes sent through the stub | **accepted**: 3 spans, same names and trace id, `ExportTraceServiceResponse` returned |

The gRPC row closes the transport half of B5's question — the capsule's bytes are not just "parseable", they survive the
framing an exporter puts them in — but it is a receiver built from OTel's own generated code, **not** a product. Still
unmeasured, with the reason recorded in the artefact rather than an adjective:

* **OpenTelemetry Collector (otelcol)**, **Grafana Tempo**, **Jaeger** — all need a released Go binary; the download is a
  302 to `objects.githubusercontent.com`, which this sandbox cannot reach (the fetch stops at 0 bytes).
* **Langfuse** — a TypeScript server with Postgres behind it; no pip package, no image pull.
* **Phoenix's own gRPC listener on 4317** — the 2026-11-16 measurement started `phoenix serve` and used its HTTP receiver;
  the gRPC listener was not exercised.

The capsule verifies itself (exit 0) in the same run, so the bytes handed to those readers are the bytes the capsule ships.
B5 stays open: three real products and one product's second transport are still unmeasured.

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

## B8 — `verify` took exactly one capsule, and the README's habit is a glob — FIXED

`blackbox verify capsules/*.bbx` was `error: unrecognized arguments` (a shell that expands the glob) and, on a shell that
does not expand it, a path that does not exist. The README shows a single path, so this was a paper cut rather than a lie,
but it is the command a user types after `ls` shows two capsules.

**Fixed.** The positional is `nargs="*"` and the command expands a pattern itself, so the quoted form works on POSIX,
Windows and PowerShell alike. Every capsule is verified and printed; the exit code is the worst outcome of the batch
(**9** any check failed, **2** any target unreadable, **0** all verified), and a pattern that matches nothing is named
rather than silently shrinking the set. Measured on a real run's two capsules:

```
$ blackbox verify '/tmp/b8/capsules/*.bbx'
...
blackbox verify: 2 capsule(s) verified; 1 check(s) declared, not checkable from a capsule      # exit 0
$ blackbox verify /tmp/b8/capsules/*.bbx                                                       # shell-expanded, same
blackbox verify: 2 capsule(s) verified; 1 check(s) declared, not checkable from a capsule      # exit 0
```

Tests: `tests/test_verify_cli.py` (23, five new) — two capsules named at once, a quoted pattern, one tampered capsule in a
batch making the exit code 9 and naming only that one, a pattern that matches nothing (a warning beside a good file, and
exit 2 alone), and an unreadable path in a batch exiting 2. Defect 74 in `docs/defects.md`.

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
| `verify` with more than one capsule (B8) | `error: unrecognized arguments` | one or many, quoted pattern or shell-expanded, worst exit code wins | `tests/test_verify_cli.py` (+5) |

Defect entries: `docs/defects.md` 64 (hook path imports), 65 (a manifest that described a recording it had not made),
62 (the descriptor ceiling, still open for the policy half), 61 and 63 (the previous branch's teardown and probe fixes).

---

## Not blockers (known limits, already documented)

Content is hashed by default (`content_mode: hash`), so prompt and response **text** is not in the capsule unless the
user asks for it; there is no price table, so cost is `unknown_cost: true` rather than a confident zero; the repair path
is approval-gated and applies nothing without a token; Windows' symlink guarantee is check-then-write, not atomic. All
four are in the README's limits section and pinned by `tests/test_readme.py`.
