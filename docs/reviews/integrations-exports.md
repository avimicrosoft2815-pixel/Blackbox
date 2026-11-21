# Review pack — integrations and exports (this is not a gate file, and carries no verdict) — unsigned builder evidence

**Re-read at `1466f43`, 2026-11-21.** Every command in this pack was re-run against that tree (the one this file now
ships in): 51 tests, all pass, exit 0. No row changed and no row was added; a re-run is not a review.

Two different promises live here, and they are easy to conflate:

* **Exports** — the capsule's own `otlp/spans.{json,pb}` and `events/events.jsonl` are derived parts, regenerated
  byte-identically from raw frames, and a drift is a broken capsule (`BBX-E412`).
* **Importers** — whether other people's tools can read those files. That is a property of *their* software, measured
  product by product, with versions, and only where the software could be installed here.

## Rows, code and commands

| Row | Where it lives | The command | What this machine saw on 2026-11-20 |
|---|---|---|---|
| `events/events.jsonl` and `otlp/spans.json` regenerate byte-identically; drift ⇒ `BBX-E412` | `src/blackbox/cap/derived.py`, `export_jsonl.py`, `export_otlp.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_export_jsonl.py tests/test_export_otlp.py tests/test_export_otlp_pb.py -q` | **12 + 10 + 11 collected, all pass** |
| The OTLP protobuf parses as the official messages, and a gRPC `TraceService` accepts it | `bench/bench_otlp_importers.py`, `bench/results/2026-11-18-otlp-importers.json` | `PYTHONPATH=src .venv/bin/python bench/bench_otlp_importers.py --help`, then re-run | recorded: `opentelemetry-proto` 1.45.2 / `protobuf` 7.36.2 parse 3 spans with every `parent_span_id` resolving; OTel's own servicer over `grpcio` 1.85.3 returns `ExportTraceServiceResponse` |
| Phoenix's HTTP receiver reads the capsule's bytes | `bench/results/2026-11-16-otlp-importer.json` | — | `arize-phoenix` 21.0.0 answered 200 to `otlp/spans.pb` and read back 3 spans with the trace id intact; the same receiver answers **415** to the JSON encoding, which is why the capsule ships both |
| Importers that could not be installed here | `docs/STATUS.md` §Labelled (P4-14) | — | Langfuse (its SDK is an exporter, and the server needs TypeScript plus Postgres), the OpenTelemetry Collector, Tempo and Jaeger (Go binaries behind `objects.githubusercontent.com`, which this sandbox cannot reach: the fetch stops at 0 bytes), Phoenix's gRPC port 4317 |
| The proxy's header contract: `x-blackbox-run` is injected only under the launcher, and stripped before the upstream | `src/blackbox/cap_sdk/headers.py`, `tests/test_headers.py` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_headers.py -q` | passes |
| The SDK matrix and the client matrix: `openai`, `anthropic`, `curl`, Node, unmodified | `tests/test_attach_sdks.py`, `tests/proxy/test_client_matrix.py`, `docs/attach.md` | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_attach_sdks.py tests/proxy/test_client_matrix.py -q` | passes; every row in `docs/attach.md` is dated or marked **unverified** |
| Byte identity through the proxy over 10 000 streamed responses | `bench/bench_stream_identity.py` → `bench/results/2026-11-20-stream-identity.json` | `PYTHONPATH=src .venv/bin/python bench/bench_stream_identity.py --n 1000` | recorded: 10 000 of 10 000 identical, 2 195 560 bytes, p50 1.151 ms; the control reports exactly the one corrupted response |

## What a reviewer should decide

1. **Is a universal-compatibility claim anywhere?** It must not be. `docs/attach.md` and `docs/privacy.md` should say what
   was measured, with versions, and mark the rest unverified. A reviewer finding "works with any LLM" or equivalent has
   found a finding.
2. **Are the exports derived or authored?** Regenerating them from frames and comparing bytes is the test; a hand-written
   export would pass a schema check and fail this one.
3. **Is the importer list honest about its gaps?** Three products could not be installed here. The pack names the exact
   blocker; a reviewer with network access can settle them in an hour with `bench/bench_otlp_importers.py`.
4. **Does the strip happen before the upstream sees it?** `tests/test_headers.py` asserts against a mock provider that
   records what arrived, not against our intention.

## What this machine cannot do

* Install the four blocked importers (see above).
* Test TLS or HTTP/2 through the proxy: the rig is loopback HTTP/1.1, and the stream-identity artefact says so in its own
  `what_this_measures` field.
* Measure a hosted product's behaviour (rate limits, retries) without keys.

## The command

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_export_jsonl.py tests/test_export_otlp.py tests/test_export_otlp_pb.py tests/test_headers.py -q
```
