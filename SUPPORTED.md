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
| A — client hooks | `cap_sdk/seam_httpx_hooks.py` | `httpx.Client.__init__`, `httpx.AsyncClient.__init__` (adds a request hook and a response hook) | httpx | **httpx 0.28.1, Linux, 2026-11-15** | the response hook wraps the still-unread stream with the shared tee, so A sees every chunk. Clients built *before* install are not captured — the preflight records that instead of pretending |
| B — transport tee | `cap_sdk/seam_httpx_transport.py` | a client's `_transport`, or `httpx.HTTPTransport.handle_request` | httpx | **httpx 0.28.1, Linux, 2026-11-15** | works when the SDK owns the client. Bytes forwarded unchanged: sha256-compared against an unmodified client in `tests/test_seam_transport.py` |
| C — SDK methods | `cap_sdk/seam_sdk_methods.py` | `openai` chat/responses/embeddings, `anthropic` messages | the SDKs | **not built** | P3-05c. Until it exists, an SDK that ignores `event_hooks` *and* hides its transport is not captured at all, and the preflight says so |

| seam | verified on | unverified |
|---|---|---|
| A | httpx 0.28.1 | httpx 0.27.x, 0.26.x, and every future version: the seam's capability depends on httpx firing response hooks before the body is read, which is a timing fact, not an API contract. `tests/test_seam_hooks.py` fails loudly if a version moves it, rather than writing zero-byte calls |
| B | httpx 0.28.1 | anything that stops exposing `HTTPTransport.handle_request`, or a client whose transport is not reachable as `_transport`. `client=` install is the supported path; the class patch is the fallback |

The async path (`handle_async_request`, `AsyncClient`) is implemented for both seams but **not exercised by a test
here** — no async HTTP test harness is installed in this environment. That is a gap, not a claim; it is listed in
`docs/defects.md`.

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
it ever appears at module scope. Python `>=3.11`.

## What is still pending in this table

D-020 through D-022 are recorded in `docs/DECISIONS.md`: the capture layer's declared network exception, the
`P3-13`/`P3-13MP` numbering reconciliation, and the `partial` marker. `P3-05d` requires `capture_seam` in `run.json`
and the manifest; the decision object and its `as_dict()` exist and are tested
(`tests/test_preflight.py::test_the_decision_survives_being_written_to_a_manifest`), but nothing writes a `run.json`
until P3-06 lands the sink, so that half of the Done-when is **pending**, not done.
