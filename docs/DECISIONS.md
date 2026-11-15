# Decisions (append-only)

Plan defaults are taken unless a row here says otherwise. Each row names the plan section it interprets.

| # | date | decision | plan ref | why |
|---|---|---|---|---|
| D-001 | 2026-11-14 | `requires-python >= 3.11`; build backend setuptools; console script deferred to P4-05 | P0-01 | 3.10 is EOL Oct 2026; `tomllib` avoids a dependency; hatchling would require an install |
| D-002 | 2026-11-14 | Dev-only tools: `pytest 8.3.4`, `hypothesis 6.122.3`, `ruff 0.17.5`, `mypy 2.4.1` are installed in `.venv` only (C6); the shipped package has zero runtime dependencies and `pyproject.toml` declares none | §3 | `make fmt` runs `ruff check` (0 findings) and `mypy` (**0 findings**, `strict = true` with nothing disabled since `P1-02b`) |
| D-003 | 2026-11-14 | F8 re-scoped: keeper pre-creation of `<n+1>.seg` is the default; the synchronous roll path is measured per OS | §8.6 | owner instruction |
| D-004 | 2026-11-14 | Event-kind and error-code registries live in `constants.py` / `errors.py`; only the lead edits them | §8.4, §19 | contract files |
| D-005 | 2026-11-14 | The `head` file is `mmap`'d once at `open()`; publish is `struct.pack_into` into the alternate slot | §8.2 step 7 | "no syscall at all (page cache)" is the plan's own text |
| D-006 | 2026-11-14 | A magic that is shorter than its field is padded with `0x00` and frozen in hex in `docs/format.md` | §8.3 | A7 |
| D-007 | 2026-11-14 | **Format freeze, not a default:** the only checksum in format v2 is CRC-32/ISO-HDLC (`zlib.crc32`, check value `0xCBF43926`) | §8.3, §8.7, P1-01 | pure-Python CRC32C measured 110.5 us/KiB vs 0.58 us/KiB and would break the §15.1 event budget alone; `meta.flags` bit 2 is reserved and a reader rejects a tape that sets it (`BBX-E221`). Recorded as DV-005 / ADR-004 |
| D-008 | 2026-11-14 | Default group-commit threshold is 16 KiB (`DEFAULT_BATCH_BYTES`) | §8.2, §15.1 | amplification 1.231x at 16 KiB vs 1.843x with a commit per event; the §15.1 row is ≤1.25x |
| D-009 | 2026-11-14 | The progress ledger is generated from the plan by `tools/gen_progress.py` | A1 | a ledger that can drift from the plan is worse than none |
| D-010 | 2026-11-14 | **The gate column is ext4**, not tmpfs (C1): every bench runs the same protocol on the repository disk and, labelled, on `/dev/shm` | §15.1 | `fdatasync` is a no-op on tmpfs, so a kernel-mode number measured there is not a number about any device; the JSON keeps both columns and the gate reads `ext4` |
| D-011 | 2026-11-14 | The group-commit window is bounded by bytes, time, priority or `flush()`, and the keeper thread drains it | §8.2, §10.3 | measured window maxima: 4.45 ms at 1 ev/s, 4.999 ms at 1 000 ev/s, 3.85 ms unbounded, 121 events/publish unbounded (`bench/results/2026-11-14-writer-throughput.json`) |
| D-012 | 2026-11-14 | The `head` file is created atomically and is rebuildable from the segments | §8.2 | a head is a hint, not a source of truth: a kill during head creation left `segments exist but the head is unreadable` (kill seed 120); `repair.rebuild_head` reconstructs it, and a rebuild continues from the highest readable gen |
| D-013 | 2026-11-14 | **P1-19 verdict: keep the mmap'd active segment** (interleaved A/B, Stage-2 item 8) — `write()`-append measured 1.19–1.22x the mmap p50 at 10 k/40 k ev/s in two runs, above the 20 % reopen threshold | §8.1, §8.6 | `bench/results/2026-11-14-writer-ab.json`; ADR-001 §4 records both runs **and** the caveat that the effect's size is not stable here (a third run inverted one rate), so the exact ratio is labelled `requires hardware` |
| D-014 | 2026-11-14 | A writer that cannot allocate its first segment is a **degraded sink**, not an exception | §8.6, S17 | the app keeps running with byte-identical output and exit 0; `writer.recording` is the one honest flag, and nothing is published, so recovery never sees a half-created tape |
| D-015 | 2026-11-14 | **Store: `pwrite`-append, not mmap** (supersedes D-013/ADR-001) | P1-19 under the C17 rule; ADR-005 | median p50 ratio 1.059 / 1.065 at 10 k / 40 k ev/s, `pwrite` p99 72.0 / 68.4 µs → inside the 20 % rule, so the plan's tie-break applies; per event the `pwrite` path is ~6 % slower and that is the accepted cost |
| D-016 | 2026-11-14 | **F2 (retention), decided here:** retention deletes whole sealed segments only; the active segment is never a victim, even when the byte budget says it must be. When the budget cannot be met without it, `apply` returns `under_budget=False` and says so | §8.10 (I1), P1-09b | an open run is never silently truncated: truncating the segment a writer is appending to would delete frames the app believes are committed, with no frame to declare them |
| D-017 | 2026-11-14 | **F3 (retention), decided here:** a `GAP` carries the chain value it was committed at (`prev`), and `scanner.verify_chain` requires that value to be on the chain it walks. A hole is therefore declared, not discovered | §8.10 (I9), P1-09b | "a retention `GAP` is a declared hole, not a break" is only testable if the declaration has a position in the chain; without `prev` the walk could not tell a tombstone from a missing link |
| D-018 | 2026-11-14 | Tombstone segments are **pinned**, and merged: a `GAP` frame's segment is not pruned while it is the only copy of that declaration, and a later pass that deletes a contiguous pruned prefix commits one merged `GAP` and removes the older tombstone segments in the same pass | §8.10, P1-09b | the randomized test found the alternative: a second pass deleted the segment holding the first `GAP`, so seqs 1–13 vanished with no declaration anywhere. Tombstones must outlive the data they describe, but must not accumulate |
| D-019 | 2026-11-14 | **Blob durability has one owner, and it is not the calling thread:** `process` syncs nothing, `kernel` has the keeper sync the blob and its directory in the same tick before the tape sync that covers the referencing frame, `paranoid` syncs before the referencing frame is appended. Invariant: *no durable frame references a non-durable blob* | §7.1 write path, §15.1 64 KiB row (C32) | the calling thread fsynced a blob per 64 KiB event, which put two syncs of the user's thread on a path whose `process` mode promises nothing against a power cut — "durable" there meant durable against a kill, and a kill never loses the page cache. Moving the sync to the keeper is also the only way the order can be *made* to hold: the thread that publishes is the only thread that can sync the blob first. Measured after the change: p50 226.864 µs / p99 461.281 µs for a 64 KiB `content=full` event on ext4 (`bench/results/2026-11-15-payload-blob.json`), 0 blob syncs on the calling thread in `process`, 1 024 in `kernel` — all on the keeper |
| D-020 | 2026-11-15 | **The capture layer is the one declared exception to "no network in `src/`"** (P0-08/P0-13), and the exception is a list that the tests check, not a loosened rule | P0-08, P0-13, P3-05a/b, P3-13MP; `tests/_capture_policy.py` | a seam's job is to sit inside an HTTP client the application already built, and the gate needs a real socket on loopback, so `src/blackbox/cap_sdk/` must be allowed an HTTP library and a listening server while the core (tape, recovery, capsule, replay, CLI) stays stdlib-only and egress-free. Implemented as: four file names in `_capture_policy.py`; `httpx` permitted only **inside a function body** (so `import blackbox` still works with httpx absent, enforced by `test_httpx_is_never_imported_at_module_scope`); `socket`/`http.server` permitted only in `mock_provider.py`; URL literals permitted only in `mock_provider.py` and only `http://127.0.0.1`; the mock provider asserted to bind an ephemeral loopback port and to contain no wildcard bind. `httpx==0.28.1` is pinned in its own optional extra, `capture`, and CI installs `.[dev,capture]`, so the integration is exercised instead of silently skipped — while `dev` stays the two test tools and `[project].dependencies` stays empty — the runtime dependency list stays empty |
| D-021 | 2026-11-15 | **Two plans number `P3-13` differently; the mock provider is tracked as `P3-13MP` so neither ticket can be ticked by the other's evidence** | AGENT_FINAL §L2, `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` §18.5, `docs/STATE.json` | `AGENT_FINAL.md`'s L2 list makes P3-13 the mock provider (required by G-Capture); the in-repo plan's §18.5 makes P3-13 `rec/buffer.py`, the bounded queue. Both are real work. Rather than pick one meaning — which would let a green mock-provider commit mark an unbuilt queue "done" — the mock provider carries the suffix and the buffer keeps P3-13. `STATE.json` lists both, separately, and neither inherits the other's commit |
| D-022 | 2026-11-15 | **A seam that cannot see part of a call marks it `partial`; it never writes zeroes and never estimates** | P3-05a, P3-05d, D-020 | the recording contract in `cap_sdk/base.py` says a seam either captures the bytes or does not capture. Seam A records through two httpx hooks — a request hook that stamps the clock before the request goes out, and a response hook that wraps the still-unread stream — so it sees every chunk and its `first_byte_ms`/`total_ms` are end-to-end rather than measured from the moment the headers arrived (which would have understated every latency row). When the body is already gone by the time the hook runs (a replayed response, a hook added late) the call is recorded with `partial=True` and no byte count: a reader must never have to guess whether a `0` means *nothing arrived* or *nothing was visible*. No seam retries, replays or reissues a call to fill a hole |
| D-023 | 2026-11-15 | **Seam C's unit is a parsed stream event, not a byte**, and it says so on every record it writes | P3-05c, D-022, G-Capture | the plan asks seam C to wrap SDK methods; once the SDK has decoded SSE, the byte count available to a wrapper is the size of the SDK's own objects, which is a number about this library and not about the wire. So seam C counts events into `chunks_received`, leaves `bytes_received` at 0, and marks every call `partial=True`. Consequently: a capsule captured by seam C can answer "how many events arrived" and cannot answer "how many bytes arrived", and any gate row that needs bytes must name a seam that watched a socket (A or B). `SUPPORTED.md` carries the same sentence, and the preflight's C attempt is recorded as the last resort it is |
| D-024 | 2026-11-15 | **A recorded "chunk" is a chunk *frame*, and the coalescing parameters travel with the run** | P3-06, G-Capture, D-023 | a 2 000-token stream is 2 000 socket reads; one frame per read would make the tape twice the payload and a recovery scan spend its time on 60-byte frames, so chunks are grouped until **8 KiB or 100 ms** and each group is one `MODEL_CHUNK` frame whose payload is the raw bytes, unmodified. Consequences, stated rather than implied: the byte count stays exact (payloads concatenate back to the body — that is a test), `chunks_received` counts frames rather than reads, and both numbers (`chunks_events`, `chunks_raw`) plus the coalescing parameters are in the response summary and the request frame, so a capsule can never be read as claiming a raw-read count it does not have. `kill -9` costs at most the one group that had not flushed, which is bounded by 8 KiB; the unfinished-call test asserts that bound rather than a total |
| D-025 | 2026-11-16 | **A seam patches every known client module, and the tee takes its base class from the stream's own module** | P3-05a/b, G-Capture, D-020 | the published `openai` 3.51.0 and `anthropic` 2.8.0 speak **`httpx2`**, not `httpx`. A seam that patched only `httpx.Client` installed, reported `installed`, and captured nothing at all for both SDKs — the worst failure a recorder can have, because the run looks recorded. Seams A and B now patch `Client`/`AsyncClient`/`HTTPTransport`/`AsyncHTTPTransport` in **every** module that imports (`httpx`, `httpx2`), the handle's `detail["modules"]` lists what was covered so the decision is auditable, and `tee.py` builds its `SyncByteStream`/`AsyncByteStream` base from the module that produced the stream, because the client's `isinstance` assertion is against its own class. Seam C remains the answer for a client library this code does not know, and the preflight's order is unchanged. Found by the day-14 child, not by a unit test: the seams were green against `httpx` the whole time |
| D-027 | 2026-11-17 | **Fail-open is a property of each callback, enforced in one wrapper, and it never records an exception message** | P0 at `ee210ae`, §4, §8.6 | the independent reviewer's repro: with seam A installed, a receiver whose `receive()` raised propagated out of `iter_bytes()` and killed the host. The guard now lives in `cap_sdk/failopen.py` (`FailOpenReceiver`), and the seams hand their receiver through it, because a per-seam `try` is what left this hole: seam A's hook had one, so `open_call` was protected and the per-chunk path was untested, and seam B's `open_call` had none at all. Three rules: only `Exception` is caught (`CancelledError`, `KeyboardInterrupt` are not recorder failures and swallowing them hangs a cancelled task); every failure is counted, with a bounded sample of `method:ExceptionType`; and the exception *message* is discarded, because a message can carry the model output this layer exists to protect. `open_call` returns a marked `partial=True` call with an empty fingerprint — unknown, never "the same as empty" |
| D-028 | 2026-11-17 | **A stream the seam closed early is `partial`, and a chunk count is only ever compared with the reads the application made** | D-023, C40, P0 §"verify" | two corrections found by testing the claim instead of the code. (1) A cancelled or abandoned stream used to record `status: 200` and no marker, which reads as a completed call; the tee now sets `partial = True` when it closes without reaching the end of the inner iterator, because the bytes are real and the call is not. (2) An expectation of "N chunks in, N chunks out" is a TCP fact, not an invariant — a six-chunk SSE stream arrives as four or six reads depending on the machine — so every chunk assertion compares the recorder's count with the number of reads the application itself made in the same run. |
| D-029 | 2026-11-18 | **Redaction is a policy in front of the tape, on by default, and it holds only what is unfinished** | P3-11, G-Capture, G-Sec, DV-013, architecture §12.2 / C4 | the redactor sits between the seam and the sink, so what is written is already clean: a secret written and then hidden is on the disk, in the capsule and in somebody's backup. Two design questions had no obvious answer and are now decided by measurement. (1) *Where does it hold?* A first version held a fixed 4 KiB tail and released only what it could prove was not the start of a secret. That is correct and it loses a whole short stream to a `kill -9`: the day-14 test, whose child dies after five chunks, recovered `MODEL_REQUEST` and no chunks at all. The release rule is now *hold only what is unfinished* — a `PENDING` identifier whose body is still arriving, a complete match that ends exactly at the edge of what has arrived, at most 16 bytes of a bare token run — clamped by `WINDOW` 4096 for memory. Prose, JSON and SSE framing are released on the read that brought them, so a hard kill costs the last unfinished token. (2) *What is the default?* Redaction is on: `TapeSink(emit)` with no argument builds `Redactor.from_config()`, `BLACKBOX_REDACTION` defaults to `full`, and a misspelt value falls back to `full` because a typo must not be a privacy decision; `off` exists and is recorded. Consequences stated rather than implied: a secret longer than `WINDOW` can still be split; the patterns are the ones somebody wrote, so the 60-shape corpus proves those 60 and `SUPPORTED.md` does not say "safe"; `hash` mode is an **unkeyed** sha256 prefix, not the architecture's keyed per-machine HMAC (DV-013); a redactor that raises fails **closed** — `[REDACTION_FAILED]` replaces the content, the event keeps its place in the stream, and `redaction.failures` counts it; and the summary now reports reads (`chunks_raw`) separately from what the tape stored (`chunks_stored`, `bytes_stored`), because redaction merges and drops segments and neither number may be read as a fingerprint of the other |
| D-030 | 2026-11-18 | **The content mode is honoured by the sink, its default is the plan's `hash`, and a chunk payload in `hash`/`off` mode is a header that declares the byte count** | P3-11c, §15.1, Appendix A, D-029, defect 30 | until this change `constants.DEFAULT_CONTENT` said `hash` and `TapeSink` wrote the body anyway: a documented default the code ignored, which is worse than no default because a user reads `content=hash` and believes the prompt is not on the disk. The sink now takes `content` (`full`/`hash`/`off`, from the argument, then `BLACKBOX_CONTENT`, then Appendix A's `hash`; an unknown value raises rather than guessing) and writes: in `full` the raw bytes as before; in `hash` `[bbx-hash sha256=<64 hex> bytes=<n>]`; in `off` `[bbx-off bytes=<n>]`. The count travels in the payload because `recovery` must answer "was the call cut short" from frames alone, and a tape that cannot say how many bytes arrived cannot answer it — `chunk_bytes()` is that rule, shared by the sink and `rec.recovery`. Redaction is skipped entirely in `off` mode: there is no content to redact, and paying 10 µs a read to redact text that is then discarded is how a recorder becomes the slowest thing in the request. Consequences stated: the byte count survives every mode but the *body* does not, so a `hash` capsule cannot be re-read for content; the gate tests that compare payload bytes with what the provider pushed ask for `full` explicitly, which is the honest way to test byte fidelity; and redaction now costs **1.4 µs** (`off`), **11 µs** (ordinary SSE) or **139 µs** (a 1 KiB body containing `token`, the worst case, still under the 1 ms hard fail) per read — measured in `bench/results/2026-11-15-capture-overhead.json` |
| D-031 | 2026-11-18 | **`hash` mode is keyed by a per-machine key in one file, the capsule names the key by id, and a missing key is recorded as unkeyed** | P3-11d, DV-013, DV-014, §12.2 stage 7, D8 | an unkeyed digest of a short secret is brute-forceable, so `[REDACTED:openai:a1b2c3d4]` was only decorative without a key. The pseudonym is now `HMAC-SHA256(key, secret)[:8]`, key = 32 bytes from `~/.blackbox/keys/redaction.key` (or `BLACKBOX_KEY_PATH`), created on first use with `O_EXCL` at mode `0600` in a `0700` directory and never rewritten — `O_EXCL` plus a read-back is the whole concurrency story: of four processes starting at once, one creates and three read what it wrote, and a test runs exactly that. The key itself never reaches a tape, a capsule or a log: a capsule carries the **key id** (`sha256(key)[:8]`), so a reader can tell two capsules were pseudonymised with the same key without being able to use the name for anything. When the key cannot be had — a read-only home, a denied path — `hash` mode falls back to the unkeyed digest **and says so** (`"keyed": false, "key_id": null` in the response summary), because a capsule that claims a keyed pseudonym it never used is worse than one that was never keyed. Rotation is deletion: new pseudonyms stop matching old ones, which is what rotation is, and the key id in each capsule says which one it used. `full` and `off` never touch the key, and a test asserts no key file appears |

## D-032 — the proxy's allowlist is the route table, and its only outbound call site takes a destination from it

*Date: 2026-11-16. Plan refs: §13, P3B-01/P3B-02, P0-08, D-020.*

**The plan default**: an egress allowlist equal to the configured upstreams; loopback-only bind with `--bind-unsafe`;
recording errors swallowed and flagged; forwarding errors answered `502`/`504` with `x-blackbox-error`.

**What was decided, and why it is a decision rather than obedience.** The plan says "egress allowlist = configured
upstreams", which is a property one can intend and fail to have. So it is made structural instead: `Routes.resolve()`
returns a `Route`, `Route.destination` is the only source of `(host, port)`, and `proxy/server.py` has **exactly one**
`open_connection` call site, handed `*route.destination`. `tests/test_no_egress.py` asserts that shape by AST and then
asserts the destination set of a real table — so "the proxy can only reach what it was configured with" is a test that
fails when the structure changes, not a sentence in a design document. Redirects are not followed for the same reason
(a `Location:` from an upstream would be a destination that is not in the table).

**The import exception, narrowed.** `proxy/` joins `cap_sdk/` as a declared network exception, for `asyncio`, `ssl`,
`socket` and `urllib.parse` — the last one because splitting a base URL is parsing, not fetching, and `urllib.request`
is still banned by the same test. A hardcoded provider URL inside `proxy/` fails `test_no_url_literals`; loopback may
appear, as it does in the mock provider.

**Upstream connections are one-request connections** (`Connection: close`), because HTTP/1.1 keep-alive pools in a
proxy are a correctness problem (a reused connection to the wrong route, a half-read body) for a benefit measured in
microseconds against a model call. The **client** side keeps its keep-alive, which is the one that matters to a
launcher-spawned SDK.

**Consequences.** `blackbox run -- node app.js` can point a base URL at this listener and the traffic cannot leave for
anywhere else; `P3B-03` (launcher integration) can therefore export base URLs without inventing a second allowlist.

## D-033 — the proxy thread is supervised on the port the child was told, and an environment variable is adopted, not overwritten

*Date: 2026-11-16. Plan refs: §13, P3B-03.*

**The plan default**: `blackbox run` starts the proxy thread, exports base-URL variables only when unset (existing
values become upstreams), and supervises the thread with a rebind ≤ 100 ms.

**Two decisions inside that sentence.** First, **"only when unset" is not politeness.** `OPENAI_BASE_URL` may already
name a router, a gateway or a local model; a recorder that overwrites it changes where the application's traffic goes,
which is the one thing a recorder may never do. So a set variable is *adopted*: its destination becomes a route (P3B-02
would otherwise refuse to reach it), and the child's value stands. A value that is not a URL (`OLLAMA_HOST=host:port`)
is adopted as a claim about the user's intent but is **not** turned into a route by guessing a scheme.

**Second, a rebind must keep the port.** The child was handed a base URL once, at spawn; a supervisor that rebinds on a
new port has fixed the listener and broken the child. So `ProxyThread.rebind()` tries the old port first and records
`port_changed` only when the port genuinely went to somebody else — and the test closes the listener behind the
watchdog's back and asserts service returns **on the same port** inside the 100 ms budget.

**Consequences.** Supervision is a number (`rebinds`, in milliseconds) rather than a claim; the launcher's second half
(P3B-03) only has to call `start()` before `Popen` and `stop()` after `wait()`.

## D-034 — the provider base URLs are data in the package, not literals in `proxy/`

*Date: 2026-11-16. Plan refs: §13, P3B-02/P3B-03, P0-08.*

**The problem**: a bare `blackbox run` has to know that `openai` means a particular host, and `tests/test_no_egress.py`
forbids URL literals in `src/` — including `proxy/`, where loopback is the only scheme allowed to appear. Weakening
that test to let four provider URLs sit in Python would have been the wrong trade: the test is what stops a fifth one
appearing somewhere else.

**The decision**: `src/blackbox/proxy/upstreams.json`, loaded by `Routes.provider_defaults()`, with `_comment` keys
skipped. It is package data (`[tool.setuptools.package-data]`), so an installed wheel has it. Three consequences worth
writing down: a reviewer reads the destination list without reading Python; a name with no default and no
`--upstream` override is an **error** rather than a missing route; and a route built from the table is still only
reachable through the table (P3B-02's allowlist is unchanged — this is where the values come from, not where the
permission comes from).

## D-032a — upstream connections are pooled after all, because the bench disagreed with the argument

*Date: 2026-11-16. Supersedes the last paragraph of D-032. Plan ref: §15.1 (first-byte passthrough ≤ 0.5 ms).*

D-032 said upstream connections would be one-request connections, on the grounds that pooling is a correctness problem
for "a benefit measured in microseconds". The bench measured it: with a fresh connection per request, first-byte
passthrough was **0.67–0.82 ms** against a 0.5 ms budget, and `TCP_NODELAY` on both sockets did not move it — the cost
is the handshake, not the scheduling. So the argument was wrong and the number was right.

**What is pooled, exactly**: idle per-route connections (at most two per route, twenty seconds), released only after a
response was read to its end — never after a truncated body, an early-close client, or an unterminated stream. A
pooled connection is a guess about the far end, so the guess is never shown to the caller: if writing to it or reading
its head fails, the exchange is **retried once on a fresh connection** before anything is written to the client, and
`pool.retries` counts it. Two tests hold that: one proves reuse (the origin sees one connection for two requests), one
proves the retry (the origin advertises keep-alive, drops the connection, and the client still gets its 200).

**Consequences**: first byte 0.42 ms (in budget, n 100), added latency p50 0.27 ms / p99 0.65 ms (n 500), 200
concurrent streams with byte-identical bodies. The rule "never write to the client before the upstream's head has been
read" is what makes the retry invisible; it was already true, and it is now load-bearing.

## D-032b — an uncertain outcome is reported, not retried: idempotent methods only

*Amended by D-032c, which removes the default most of this entry relies on: "idempotent methods only" became "none by default, one retry as an opt-in".*

*Date: 2026-11-16. Supersedes the retry half of D-032a, which a reviewer flagged as a P0. Plan refs: §13, P3B-08.*

D-032a said a dead pooled connection is "retried once on a fresh connection before anything is written to the client",
and called that invisible. It was invisible to the client and **not** invisible to the provider: the request had already
been written to the dead connection, so the retry could deliver it twice. For `POST /v1/chat/completions` that is a
second charge.

**The rule now, in two halves.** *Before* the write, a pooled connection is probed (`at_eof`, `is_closing`, `exception`)
and a dead one is discarded — nothing has been sent, so nothing can be duplicated, and the connection is replaced for
free. *After* the write, the outcome is uncertain by construction and the proxy says so: the exchange is retried only
for `GET`, `HEAD` and `OPTIONS`, whose repetition cannot produce a second effect. Everything else gets
`502 upstream-closed-uncertain`, an `x-blackbox-error` header naming the class, a body that says the request may have
been applied, and a `stats.uncertain` counter. `PUT`/`DELETE` are deliberately in the "reported" half: their repetition
is idempotent *in intent*, but the proxy cannot know whether the far end agrees.

**Why not just never pool?** Because the §15.1 first-byte row cannot be met without it (0.67–0.82 ms against a 0.5 ms
budget), and pooling is now safe by construction rather than by hope. **Why not buffer the request and replay from a
reconstruction?** Because a replay is still a second request; the honest answer to "did this take effect" is to tell the
caller, not to guess twice.

## D-032c — retries after transmission are off by default; `SAFE_METHODS` is an intention, not a guarantee

*Date: 2026-11-16. Amends D-032b, which a reviewer corrected: "GET/HEAD/OPTIONS are safe" is a claim about HTTP's intent, not
about every upstream. Plan refs: §13, P3B-08.*

D-032b retried `GET`, `HEAD` and `OPTIONS` after a transmission it could not account for, on the grounds that "repeating them
cannot duplicate an effect". That sentence is wrong as a universal, and the proxy has no way to know when it is wrong. HTTP
*says* those methods are safe; the specification's guarantee is about the **semantics a server claims**, not about the code
that actually runs. Real upstreams and real middleboxes break it all the time: a `GET /v1/models/...` that lazy-loads and
bills on first touch, a `HEAD` on an object store that counts a request or refreshes a lease, an `OPTIONS` that travels
through a gateway which logs a write per preflight, a cache-warming `GET /v1/files/{id}/content` that materialises a
download, or an old load balancer that turns an idempotent method into a retried charge. A proxy that resends is making an
assumption about somebody else's server, from the client's side of the wire, silently.

**The rule now.** After a request has been transmitted, an uncertain outcome is **reported and not retried, for every method,
by default**. The client gets `502`, `x-blackbox-error: upstream-closed-uncertain`, and a body that says the request may have
been applied; `stats.uncertain` counts it and `pool.retries` does not move. The only way to get the second attempt is to ask
for it: `--proxy-retry-transmitted` on `blackbox run`, or `retry_transmitted=True` if you drive the proxy yourself. And even
then the retry is limited to `SAFE_METHODS` (`GET`, `HEAD`, `OPTIONS`) — the flag says *"I have checked my upstreams and they
are safe for these"*, which is a statement only the caller can make. `POST`/`PUT`/`PATCH`/`DELETE` are never resent, flag or
no flag; for `POST /v1/chat/completions` that is a second charge.

**Why a flag at all, rather than deleting the capability?** Because the failure it repairs is real: a provider that closes an
idle keep-alive connection on its own schedule makes a pooled connection look dead to us and to the client as a spurious
502, and a caller talking to a well-behaved upstream should be able to buy that repair knowingly. Defaults are for the
caller who has not thought about it; the flag is for the one who has. **Why keep the name `SAFE_METHODS` if it is not a
guarantee?** Because it is the name the specification uses and the set the flag is scoped to; the docstring and the flag's
help text both say what it actually is — HTTP's assertion, which the operator is being asked to confirm.

**Tested, not just documented.** `test_a_transmitted_get_is_not_retried_by_default` (flag off: one GET reaches the upstream,
the client sees `502 upstream-closed-uncertain`); `test_retry_transmitted_retries_a_get_on_a_fresh_connection` (flag on: the
upstream sees the GET **twice**, the client sees the response); `test_retry_transmitted_still_never_resends_a_post` (flag on:
a POST is still sent **once** and still answered `502`); and the pre-write case is its own test, because a connection found
dead before the write needs no flag at all.

**Cost of the correction, measured.** None on the four §15.1 rows, and the same commit that fixed this fixed the bench. The
first-byte row had been reading 0.44–0.52 ms with PASS/FAIL alternating and was described as "marginal"; the cause was that
origin, proxy and client shared one interpreter, so the row measured a GIL hand-off. With each in its own process — the
topology the budget is about — the same code reads p50 **0.184 ms**, every 20-sample block under the 0.5 ms budget, first
byte p50 0.184 ms, RSS growth **0.0 MiB**. Budgets were not touched.
