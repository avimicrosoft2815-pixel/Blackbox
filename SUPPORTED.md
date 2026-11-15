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

## Redaction (P3-11)

Redaction runs **before** anything is written: between the seam and the sink, not on the way out of the capsule. What
the tape holds is what it wrote, so there is no state in which a secret is on the disk and merely hidden.

| mode | what is written | verified |
|---|---|---|
| `full` (**the default**) | `[REDACTED:<kind>]` | 60 synthetic secrets of 10 kinds, every split offset of every one of them, 20 random chunkings, a byte-at-a-time stream, and a real tape grepped afterwards — `tests/test_redaction.py` |
| `hash` | `[REDACTED:<kind>:<sha256[:8]>]`: the same secret gives the same pseudonym, so two appearances correlate without the secret being stored | stable across processes; two different secrets never share a pseudonym. **Unkeyed** — see the limits below and DV-013 |
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
reach the tape. The corpus proves 60 shapes out of the ones somebody wrote a pattern for. `hash` mode is a bare
`sha256` prefix rather than a keyed HMAC, so a short secret's digest is brute-forceable (DV-013). A redactor that
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
