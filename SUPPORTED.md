# SUPPORTED — what is verified, on what, and what is only claimed

Every row here answers one question: *has this been run, on this version, on this date?* A row without a verified
version says **unverified**, in that word, and a row that cannot be checked here says **not testable here**. The
architecture's rule for this file is that a reader must be able to tell the difference without asking anyone.

## Capture seams (P3-05a / P3-05b / P3-05d)

`blackbox` records a model call through the first seam that installs, in the order **A → B → C**, and writes the name
of the winner into the run's metadata as `capture_seam`. Seams A and B are built; seam C is not, so the preflight
currently ends at B and a two-seam failure means *not capturing*, recorded rather than papered over.

| seam | module | what it patches | needs | verified | notes |
|---|---|---|---|---|---|
| A — client hooks | `cap_sdk/seam_httpx_hooks.py` | `Client.__init__` / `AsyncClient.__init__` in **every** importable client module: `httpx` **and `httpx2`** (adds a request hook and a response hook) | a client library | **httpx 0.28.1 and httpx2 2.16.0, Linux, 2026-11-16** | the response hook wraps the still-unread stream with the shared tee, so A sees every chunk. Clients built *before* install are not captured — the preflight records that instead of pretending |
| B — transport tee | `cap_sdk/seam_httpx_transport.py` | a client's `_transport`, or `HTTPTransport` / `AsyncHTTPTransport` in every importable client module | a client library | **httpx 0.28.1 and httpx2 2.16.0, Linux, 2026-11-16** | works when the SDK owns the client. Bytes forwarded unchanged: sha256-compared against an unmodified client in `tests/test_seam_transport.py` |
| C — SDK methods | `cap_sdk/seam_sdk_methods.py` | `openai` `chat.completions.create`, `embeddings.create`, `responses.create` and `anthropic` `messages.create` (sync and async classes) | the SDKs | **openai 3.51.0 + anthropic 2.8.0, Linux, 2026-11-15** | the fallback for an SDK that hides its client. **Counts parsed stream events, not wire bytes**: every call it records is `partial=True` with `bytes_received = 0`, and no claim about bytes may be made from it (D-023) |

| seam | verified on | unverified |
|---|---|---|
| C | openai 3.51.0, anthropic 2.8.0 | any other version or any SDK this table does not name. Every target is looked up by module, class and attribute: what is missing is skipped and listed in the handle's `detail["skipped"]`, never guessed. An SDK version whose shape differs will therefore *not* install, which the preflight records |
| A | httpx 0.28.1 + httpx2 2.16.0 (sync and async) | httpx 0.27.x, 0.26.x, and every future version: the seam's capability depends on httpx firing response hooks before the body is read, which is a timing fact, not an API contract. `tests/test_seam_hooks.py` fails loudly if a version moves it, rather than writing zero-byte calls |
| B | httpx 0.28.1 | anything that stops exposing `HTTPTransport.handle_request`, or a client whose transport is not reachable as `_transport`. `client=` install is the supported path; the class patch is the fallback |

**Which library the SDKs actually use, checked rather than assumed:** `openai` 3.51.0 and `anthropic` 2.8.0 build on
`httpx2` 2.16.0. That is why both seams install over every importable client module and why the `detail["modules"]`
field exists — a seam that covers `httpx` only reports `installed` and captures nothing for either SDK (defect 23).

The async path is **tested**: `tests/test_seam_async.py` runs both seams against `httpx.AsyncClient` under
`asyncio.run` (stdlib), asserting chunk count, byte total and sha256 equality with an unseamed client. Writing those
four tests found two faults that the code review had missed — `AsyncClient` *awaits* its hooks, and it builds an
`AsyncHTTPTransport` — so the async row is now a claim with a test behind it rather than a caveat (defect 20).

## Content modes (§15.1, D-030)

What the tape is allowed to keep of the model's own text. Chosen by `BLACKBOX_CONTENT` (or the `content=` argument),
default **`hash`** — Appendix A's default, and the one the code actually uses since defect 30 was found.

| mode | chunk payload | the body is | verified |
|---|---|---|---|
| `full` | the raw bytes | on the tape, byte for byte | `tests/test_content_modes.py`, `tests/test_sink.py`, the day-14 controls |
| `hash` | `[bbx-hash sha256=… bytes=N]` | not on the tape; each group's digest recomputes from the body | `tests/test_content_modes.py` |
| `off` | `[bbx-off bytes=N]` | not on the tape, and redaction is skipped because nothing is kept | `tests/test_content_modes.py` |

The byte count travels in the payload, so `recovery` can still answer "how much arrived before the process died" in
every mode — the interrupted-call classification and its `partial_stream_bytes` do not depend on the body being kept.

**Cost per 1 KiB read, measured on ext4** (`bench/results/2026-11-15-capture-overhead.json`): `off` **1.4 µs**, `hash`
on ordinary SSE **11 µs**, `hash` on a body containing `token` **139 µs** (the worst case, because that literal starts
the `assignment` pattern; still under §15.1's 1 ms hard fail). A misspelt mode raises instead of guessing.

## Redaction (P3-11)

Redaction runs **before** anything is written: between the seam and the sink, not on the way out of the capsule. What
the tape holds is what it wrote, so there is no state in which a secret is on the disk and merely hidden.

| mode | what is written | verified |
|---|---|---|
| `full` (**the default**) | `[REDACTED:<kind>]` | 60 synthetic secrets of 10 kinds, every split offset of every one of them, 20 random chunkings, a byte-at-a-time stream, and a real tape grepped afterwards — `tests/test_redaction.py` |
| `hash` | `[REDACTED:<kind>:<HMAC-SHA256[:8]>]`: the same secret gives the same pseudonym, so two appearances correlate without the secret being stored | **keyed** by `~/.blackbox/keys/redaction.key` (32 bytes, 0600, created once with `O_EXCL`, `BLACKBOX_KEY_PATH` overrides); stable across processes; two different secrets never share a pseudonym; the capsule records the **key id**, never the key. When the key cannot be had the digest is unkeyed and the summary says `"keyed": false` |
| `off` | the content, unchanged | the test asserts it is really off rather than quietly redacting |

The policy is chosen by `BLACKBOX_REDACTION` (`full` when unset) and narrowed by `BLACKBOX_REDACTION_KINDS`. A
misspelt mode is `full`, not `off`: a typo is not a privacy decision.

| kind | shape |
|---|---|
| `private_key` | a `-----BEGIN … PRIVATE KEY-----` block through its matching `-----END …-----` |
| `anthropic`, `openai` | `sk-ant-…`, `sk-…` |
| `aws` | `AKIA…` / `ASIA…` |
| `github` | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` |
| `slack` | `xox[baprs]-…` |
| `jwt` | three base64url segments beginning `eyJ` |
| `bearer` | `Bearer <token>` |
| `assignment` | `api_key` / `secret` / `token` / `password` / `access_key` `[:=] <value>` |
| `email` | an address with a dot-separated domain |

**What this is not.** It is pattern matching, and it is not called "safe" anywhere: a credential in a bespoke format, a
secret inside a binary blob, or a value longer than `WINDOW` (4 096 bytes) that straddles a release point can still
reach the tape. The corpus proves 60 shapes out of the ones somebody wrote a pattern for. `hash` mode is keyed
(HMAC-SHA256 under a per-machine key, D-031); rotate by deleting the key, which stops new pseudonyms matching old ones.
Mask offsets are **not** built: the placeholder replaces the whole value, so a viewer cannot yet point at where a key
was (DV-014). A redactor that
raises does not pass content through: `[REDACTION_FAILED]` replaces it, the event keeps its place in the stream, and
`redaction.failures` in the response summary counts how often that happened.

**Cost, measured rather than assumed.** 1 MiB of ordinary SSE through the default policy takes **≈ 20 ms** on the
development host, and the test that bounds it (`test_a_megabyte_of_ordinary_sse_costs_less_than_the_call_it_records`)
fails at one second. The first version of the email pattern cost **41 ms per 4 KiB read** — more than the call it was
recording — which is why the test exists (defect 29). What it holds is an unfinished credential, not a fixed tail: a
`kill -9` costs the last unfinished token.

## The mock provider (P3-13MP)

`cap_sdk/mock_provider.py` is a loopback HTTP server for the gate, not a provider emulator.

| dialect | paths | verified |
|---|---|---|
| OpenAI style | `POST /v1/chat/completions` (SSE `data:` frames, `[DONE]`) | **2026-11-15**, `tests/test_mock_provider.py` |
| Anthropic style | `POST /v1/messages` (SSE `event:`/`data:` frames, `message_stop`) | **2026-11-15**, same |
| scenario knobs | chunk count, inter-chunk delay, first-byte delay, truncation, mid-stream error, non-200 status, payload size | **2026-11-15**, same |

It binds `127.0.0.1:0` and nothing else: a test asserts the source contains no wildcard bind. It is **not** evidence
about any real provider — no row in this file that says "verified" means "the real API behaves like this".

## Runtime dependencies

`pip install blackbox-recorder` installs **nothing**. `httpx` is the optional `capture` extra
(`pip install blackbox-recorder[capture]`), imported inside functions only, and `tests/test_stdlib_only.py` fails if
it ever appears at module scope. Seam C's tests use the published SDKs, so they live in a third extra,
`blackbox-recorder[capture-sdks]` — a fixture that imitates an SDK can only prove the seam agrees with my idea of the
SDK, and the day-14 rule is about scripts nobody edited. CI installs `.[dev,capture,capture-sdks]` so the count in
`docs/STATUS.md` means the same thing on a runner as it does here. Python `>=3.11`.

## What is still pending in this table

D-020 through D-022 are recorded in `docs/DECISIONS.md`: the capture layer's declared network exception, the
`P3-13`/`P3-13MP` numbering reconciliation, and the `partial` marker. `P3-05d` requires `capture_seam` in `run.json`
and the manifest; the decision object and its `as_dict()` exist and are tested
(`tests/test_preflight.py::test_the_decision_survives_being_written_to_a_manifest`), but nothing writes a `run.json`
until P3-06 lands the sink, so that half of the Done-when is **pending**, not done.

## The proxy (P3B-01/P3B-02)

`blackbox` can sit between an application and a provider, so an SDK written in any language can be recorded without a
code change — point its base-URL variable at the proxy. **The launcher integration that does that for you (P3B-03) is
not built yet**; the core and the routing are.

```python
from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import ProxyServer
# one default upstream; the table is the egress allowlist
routes = Routes([Route(name="openai", base="https://api.openai.com")], default="openai")
```

* **Listens on loopback only** (`127.0.0.1`, `::1`, `localhost`). A non-loopback bind raises `BBX-E143` unless
  `bind_unsafe=True` is said out loud.
* **Routing order**, and nothing else: the `x-blackbox-upstream` header names a route (an unknown name is
  `BBX-E140`, never a fallback) → the longest path prefix wins and is stripped (`/openai/v1/…` → `…/v1/…`) → the single
  default route → otherwise `502 upstream-not-allowed`.
* **The configured upstreams are the allowlist.** A request can only leave for a host in the table; redirects are not
  followed. `tests/test_no_egress.py` asserts the shape (one outbound call site, handed `route.destination`) and the
  destination set.
* **Passthrough is byte-exact**: what a client receives from a streamed response is sha256-identical to what the
  upstream sent, and is relayed still chunked. `Expect: 100-continue`, keep-alive, hop-by-hop headers and
  `x-blackbox-*` stripping are handled; upstream failures are `502`/`504` with `x-blackbox-error`.
* **Measured** (§15.1, `bench/results/2026-11-15-proxy.json`): added latency p50 0.165 ms / p99 0.471 ms (n 500, paired
  against the same request without the proxy); first-byte passthrough 0.184 ms (n 100, budget 0.5 ms, every 20-sample block
  under it); 200 concurrent streams, no errors, every body identical; 0.0 MiB RSS growth at 50 req/s for 10 s. Origin, proxy
  and measuring client each run in their own process, because sharing an interpreter makes these rows measure the GIL
  instead of the proxy (D-032c). Upstream connections are pooled per route (D-032a) and a pooled connection found dead
  *before* a request is written is replaced for free.
* **An uncertain upstream outcome is reported, never retried — by default for every method.** If a connection dies *after*
  the request went out, the client is answered `502` with `x-blackbox-error: upstream-closed-uncertain` and a body saying the
  request may have been applied; `stats.uncertain` counts it. `--proxy-retry-transmitted` opts into exactly one retry, only
  for `GET`/`HEAD`/`OPTIONS`, and it is a statement the operator makes about their own upstreams — HTTP only *intends* those
  methods to be safe, and a `GET` that bills or warms a cache is a real thing. `POST`/`PUT`/`PATCH`/`DELETE` are never resent,
  flag or no flag, so a duplicated effect is never the recorder's surprise (D-032b, corrected by D-032c).
* **The run id crosses the process boundary on one header, and only behind a proxy.** `x-blackbox-run: <run>/<node>` is
  injected by the SDK **only** when `BLACKBOX_PROXY=1` (which only `blackbox run` sets), so an unproxied app's requests are
  never modified; the proxy reads it, records the run on the call, and strips every `x-blackbox-*` header before the
  upstream. Links are then `exact` (same run id), `declared` (you said so) or `inferred` (±2 s and a fingerprint, evidence
  and a confidence below 1.0) — and an inference never merges runs (D-036). The resolver runs at seal time and puts the
  records in the capsule as `links.json`, never into the tape it is describing (D-038); `blackbox link <a> <b>` records a
  `declared` pair in both data directories, idempotently, and refuses to link two tapes that do not name their runs.
* **A disconnect is classified, and the class is on the tape.** `INTERRUPTED_CLIENT` (the app died, the model kept
  talking), `INTERRUPTED_UPSTREAM` (the provider died, the app saw a truncated stream) and `INTERRUPTED_PROXY` (the recorder
  died — derived from an open call plus a dead writer lease, because no frame can say it). Each carries the bytes each side
  actually moved, so the tokens a dead client never received are visible as the cost they are (D-035).
* **Recording is a tap, and the tap can fail**: a sink that raises costs frames, never a request
  (`stats.record_errors` counts them).
* **TLS**: `ssl.create_default_context()` upstream; a route built with `verify_tls=False` (the plan's
  `--insecure-upstream`) is the only way to skip verification, and `Routes.verify_all()` undoes it.
* **The variable plan** (`proxy/supervisor.py`) decides what the child sees: an unset base-URL variable is pointed at
  the proxy; a **set** one is left exactly as it is and becomes an upstream, so the recorder never moves traffic the
  user put somewhere on purpose. `ProxyThread` runs the listener on its own thread with a watchdog that rebinds a dead
  listener **on the port the child was told**.
* **`blackbox run` starts it for you** (P3B-03): the child sees `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` /
  `OLLAMA_HOST` / `GOOGLE_GEMINI_BASE_URL` pointing at the proxy plus `BLACKBOX_PROXY=1`, unless the variable was
  already set — in which case it is left exactly as it was and its destination becomes a route. `--no-proxy` turns the
  listener off, `--upstream NAME=URL[@/prefix]` adds or replaces a route, `--insecure-upstream NAME` skips verification
  for one, `--proxy-port` pins the port. The provider base URLs themselves live in `proxy/upstreams.json` (D-034).
* **A coding agent's hooks are a first-class vantage point (P3B-05, P3B-07).** `blackbox hook install --claude-code
  [--project|--user|--settings <path>]` adds a marked block to the `hooks` object of Claude Code's settings, and
  `hook remove` gives the file back **byte-identically** when nothing else touched it — if you edited your settings while
  it was installed, your edit wins and only our block is stripped. An unknown schema is refused (`BBX-E151`) before a
  byte or a backup is written, and `hook status` never writes. Each hook runs `blackbox ingest --tool claude-code --event
  <name>`, which reads the tool's JSON on stdin and writes **one atomic file** into the inbox: it exits 0 whatever
  happens, because a hook that fails is a hook that breaks *your* prompt. The fold maps all ten of §11.9's Claude Code
  events to frames — prompts and tool arguments leave hashes and fingerprints, never content — pairs
  `PreToolUse`/`PostToolUse` on a node slot that survives the process, and writes an unmapped event as
  `MARK{tool_event}` instead of dropping it (D-039). Under `blackbox run` the launcher folds the inbox every 250 ms into
  a tape of its own (vantage 3, same run id), one last time at exit, and the recovered capsule then carries an **`exact`
  link** between that tape and the launcher's — two processes, one timeline, with the class of the claim written down
  (D-040). `blackbox ingest` cold start measures p50 32.0 ms / p95 40.2 ms end
  to end in the development sandbox; the bench box's number is still owed.
* **The same adapter exists for Codex, and it tells you it is guessing (P3B-06, D-041).** `blackbox hook install
  --codex [--project|--user|--settings <path>]` installs the same marked block into `.codex/settings.json`; the twelve
  Codex events (`session.start`, `turn.start`, `tool.call`, `notify`, …) map to the same frames — an unknown event name
  is `MARK{tool_event, raw_name}`, and a recorded session folds into paired node slots closing innermost-first. What is
  **not** verified is Codex's own settings location and the shape of its hook block: no documentation for it could be
  read from this sandbox. So the belief is printed rather than filed away — `hook status --codex` says
  `NOT verified` and emits `"schema_verified": false`, `install --codex` writes the assumption to stderr, and the
  `P3B-06` row in `docs/STATUS.md` carries the same label. The cost of a wrong guess is bounded: an unknown shape is
  refused with `BBX-E151` before a byte or a backup is written, `--settings <path>` points the adapter at the real file,
  and the event mapping is independent of where the file lives. Install/remove round-trips byte-identically, and your
  own hooks sitting beside ours survive both directions.
* **The attach matrix, with the version on every row (P3B-09).** `docs/attach.md` answers "can I attach this to what
  I already use" one row per surface: the base-URL variables `blackbox run` exports, sixteen clients, and the two
  coding-agent hook adapters. Every verdict is a version **and a date** or the word **unverified**, and
  `tests/test_attach_doc.py` fails the suite if a row loses either. Four rows were *run* here — `openai` 3.51.0,
  `anthropic` 2.8.0, `curl` 7.88.1 and `node` v22.22.3, each reaching the documented path on a provider-shaped origin
  through the exported value — and running them found defect 39: the flat export dropped the `/v1` the OpenAI SDK does
  not append, so a proxied call would have asked the provider for a path it does not serve (D-042). `OLLAMA_HOST` and
  `GOOGLE_GEMINI_BASE_URL` are labelled unverified with the doubt written down.
* **Not built yet**: the bench of §15.1's proxy rows (P3B-08), confirmation of Codex's real hook schema (it has no
  Codex install to compare against here), the bench-box cold-start row for `blackbox ingest`, and the rows of
  `docs/attach.md` that need a client this sandbox does not have.
