# Known limitations — the things this build does not do, and how you can tell

This is the list a person should read before trusting anything else. Each row says what is missing, what it costs you, and
where the evidence for the statement lives. A row marked **unmeasured** is not a hedge: it means the machine that could
settle it was not available, and `docs/RELEASE-BLOCKERS.md` names what would settle it.

## Platform

| Limitation | What it means for you | Evidence |
|---|---|---|
| The **desktop window has never been launched on a real Linux desktop** | the code path is driven with a fake webview in `tests/test_desktop.py`; the window itself needs a machine with `python3-gi` + `gir1.2-webkit2-4.1` | `docs/desktop.md` matrix; `bench/results/2026-11-20-desktop-journey-automated.json` (the automated half ran here) |
| **No Windows installer has been built or run** | `tools/package/windows/blackbox.iss` is a definition that would compile and is unsigned; the journey (install → record → open → uninstall) has not happened | `docs/RELEASE-BLOCKERS.md` B9; `tests/test_windows_definition.py` (11 definition checks, not an installer) |
| **macOS is experimental and untested** | CI runs the suite on `macos-latest`; nobody has used the app on a Mac | `docs/compatibility.md` |
| **The window needs a distribution package for GTK** | `pip install` alone does not give you a window on Debian/Ubuntu; the CLI and every page still work | `blackbox desktop --selftest` names what is missing |

## Capture

| Limitation | What it means | Evidence |
|---|---|---|
| **Node clients have a proxy route but no node API** | a Node app's model traffic is recorded at the proxy's vantage; the node tree your code opens is not, so node-level edges are `unknown` for every Node client | `docs/node-edges.md`, `bench/results/2026-11-18-p3b03-acceptance.json` |
| **An SDK version this build does not name is not patched** | the preflight records it as skipped rather than guessing; the proxy still records the call | `docs/attach.md` (every row dated or **unverified**), `tests/test_attach_sdks.py` |
| **A client built before the recorder installs is not captured by the SDK seam** | the preflight says so instead of reporting a capture it did not make | `docs/compatibility.md`, `tests/test_preflight.py` |
| **Seam C counts parsed stream events, not wire bytes** | every call it records is `partial=True` with `bytes_received = 0`; no byte claim may be made from it | `SUPPORTED.md` (D-023) |
| **An unrecognised provider is reported as unrecognised** | model names and endings are not guessed | `tests/test_stream_termination.py`, `docs/errors.md` |

## Evidence and diagnosis

| Limitation | What it means | Evidence |
|---|---|---|
| **A body with no length and no chunking is `unverified`** | a stream that ends at connection close cannot be told apart from a cut; the frame says `unverified` and names the ambiguity | `docs/format.md`, defect 109's entry |
| **`verify` can only recompute what is derived** | `capture_vantage` is declared and printed as *declared, not checkable* rather than counted as a pass | `blackbox verify` output, `docs/RELEASE-BLOCKERS.md` |
| **The diagnosis never claims a cause it did not observe** | findings carry `observed`/`likely`/`possible`, their alternatives, and what would settle them | `src/blackbox/diagnose/` (each finding carries its evidence and its alternatives), `tests/test_diagnose.py` |
| **A repair is never applied automatically** | `apply` needs the token that `preview` printed, bound to the proposal's bytes and each target file's hash; running the suggested tests is a separate command | `docs/cli.md`, `tests/test_repair_workflow.py` |

## Measurements

| Limitation | What it means | Evidence |
|---|---|---|
| **The cold-start row misses its budget on this sandbox** | p95 44.5 ms against a 40 ms gate, and 32 ms of it is the bare interpreter plus the CLI's own imports; the row needs a quiet host | `bench/results/2026-11-19-ingest-cold-start.json` (`row_satisfied_here: false`) |
| **The rebind numbers are local** | no CI runner contributes a sample; a shared runner cannot answer a p95 | `docs/RELEASE-BLOCKERS.md` B6 |
| **The 10 000-response byte-identity row is one machine, one shape** | loopback, HTTP/1.1, four chunks per response; not TLS, not HTTP/2, not a real provider's timing | `bench/results/2026-11-20-stream-identity.json` |
| **Three OTLP products remain unmeasured** | OpenTelemetry Collector, Grafana Tempo and Jaeger need a Go binary whose download this sandbox cannot reach; Phoenix's gRPC listener is also unmeasured | `docs/RELEASE-BLOCKERS.md` B5 |

## Process

| Limitation | What it means |
|---|---|
| **No gate is signed by an independent reviewer** | `G-A` and `G-Promise` were signed by the builder in a fresh clone, which `docs/STATUS.md` labels *requires humans*; `G-Capture` is an unsigned candidate |
| **The product is pre-alpha** | `0.1.0.dev0`, no tag, no published package, and nothing in this repository says release-ready |
| **Three of the plan's §24 rows need people** | a five-minute install measured on three strangers, a second reader for the gates, and the licence decision |

If a limitation you hit is not here, that is a documentation defect — `docs/troubleshooting.md` says what to attach to a
report, and `docs/defects.md` shows how the last hundred were recorded: reproduced, or unmeasured and named.
