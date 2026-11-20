# Release candidate — what was built, what was measured today, and what is still somebody else's to do

**Verdict: internal preview, not a release candidate.** The product is `0.1.0.dev0`, no tag, no published package, and no
gate signed by an independent reviewer. What follows is the evidence-backed split the release checklist asks for, written
on 2026-11-20 from this checkout (`arena/5a927240-blackbox`, cut from `4ed431f`, the merge of PR #20).

Machine: Debian 12 (bookworm) x86_64, 2 vCPU, no display, no GTK/WebKit, no Windows host, no Go toolchain, no root,
Python 3.11.2 with `pytest` 8.3.4, `hypothesis` 6.122.3, `httpx` 0.28.1, `openai` 3.51.0, `anthropic` 2.8.0.

## 1. Development work completed today

| What | Evidence |
|---|---|
| **The real-user journey runs from a wheel in a fresh venv, end to end** — install, attach an unmodified `openai` app, capture, list, verify, diagnose, view, approve a repair with its token, run the authorised verification | `bench/results/2026-11-20-journey.json` — all seven steps, one pass, no fixtures |
| **The `.deb` builds here and runs from the extracted tree** — 373 entries, a launcher, the icon, the MIME type, the vendored window backend | `dist/blackbox_0.1.0.dev0_amd64.deb`; `tests/test_desktop_package.py` (9) |
| **The Linux desktop journey's automated half ran** — record, list, verify, diagnose; the window steps are recorded as *not attempted* because there is no display | `bench/results/2026-11-20-desktop-journey-automated.json` |
| **`blackbox doctor` exists** — six probes (environment, capture, tape, hooks, storage, locks), `--json`, exit 0/8 | `src/blackbox/cli/cmd_doctor.py`, `tests/test_error_advice.py` |
| **Six error remediations that named commands this build did not have are corrected**, and a test keeps it that way | `src/blackbox/errors.py`, `docs/errors.md` (regenerated), defect 111 |
| **Proxy response framing fixed** — a body with no length and no chunking was answered with `Transfer-Encoding: chunked` over raw bytes; `curl` refused the stream | defect 109, `tests/proxy/test_response_framing.py` (5) |
| **A derived capsule part built from the wall clock no longer fails its own check** — `verify` exits 9 with `BBX-E412` on a correctly sealed capsule, before the fix | defect 110, `tests/test_attach_curl_kill.py` (2) |
| **§24a's `run -- curl …` + `kill -9` row closed** — the in-flight call survives, byte counts never exceed what the origin pushed, and the capsule verifies | `tests/test_attach_curl_kill.py` |
| **§24a's 10 000 streamed-response byte-identity row measured** — 10 000 of 10 000 identical, 2 195 560 bytes, p50 1.15 ms per response, plus a control that isolates a single corrupted response | `bench/results/2026-11-20-stream-identity.json`, `…-control.json`, `bench/bench_stream_identity.py` |
| **Defect 108 read from its own recorded sample** — the 686.2 ms outlier contains **0 ms** of rebind work; a tool now splits every sample into rebind / dispatch / tick / unexplained, and the loop's own callback exceptions are recorded rather than only printed | `tools/outage_split.py`, `bench/results/2026-11-20-defect-108-split.json`, `tests/test_outage_split.py` (7) |
| **User-facing documents a beginner needs** — `troubleshooting.md`, `privacy.md`, `limitations.md`, an index in `docs/README.md` keyed to what the reader is trying to do | this `docs/` directory |
| **The full suite is green on this machine after every change**: 1 556 collected, 0 failed | `PYTHONPATH=src .venv/bin/python -m pytest tests -q` |

## 2. Checks that still need a real Linux desktop or a Windows machine

| Check | Why it cannot run here | What would settle it |
|---|---|---|
| The window opens, the setup page lists what it found, the library lists a capsule, a run opens, *check* recomputes, *diagnose* renders, the window closes cleanly | no display, no `python3-gi`, no `gir1.2-webkit2-4.1`; the code path is exercised against a fake webview and the pages are fetched over loopback | `sh tools/desktop_journey.sh` on a Debian/Ubuntu desktop, answers to six questions, the JSON pasted into `docs/desktop.md` |
| `apt install ./blackbox_0.1.0.dev0_amd64.deb`, then `blackbox desktop` | no root in this sandbox; the package is built and extracted, not installed | the same command on a machine you may install on |
| Windows: build `BlackBox-Setup-<version>.exe` with Inno Setup, install on Windows 10 and 11, launch, record with a real SDK client, open the capsule, uninstall, check the recordings are still there | no Windows host, no `iscc`, no `wine`, no PowerShell, no certificate | `tools/package/windows/README.md` is the hand-off; then move the row in `docs/desktop.md` with the installer's hash |
| Signing (so SmartScreen does not warn) | no certificate exists | the owner's decision and key |

## 3. Checks that need a quiet host or elapsed time

| Check | State today | What would settle it |
|---|---|---|
| `ingest` cold start ≤ 40 ms p95 | 33.0 / **44.5** / 53.9 ms on this 2-vCPU sandbox; the CLI's own imports are 32 ms of it | `python bench/bench_ingest_cold_start.py --n 60 --where bench-box` on the bench box |
| Rebind budget p95 on a quiet machine | the rebind itself is sub-millisecond; the outliers are the machine's scheduling (defect 108) | a quiet runner, or a Windows cell that fails again with the new record |
| The nightly: 2 000 randomised `kill -9` trials **on a schedule** | ran once as a `workflow_dispatch`, 0 violations | a scheduled week of runs |
| Three OTLP products (Collector, Tempo, Jaeger) and Phoenix's gRPC listener | their downloads are blocked from this sandbox; `opentelemetry-proto`, proto3-JSON and a gRPC `TraceService` are measured | an environment that can fetch those binaries; `bench/bench_otlp_importers.py` has the protocol |

## 4. Work that needs people

| Check | Why it is a person |
|---|---|
| **An independent reviewer for `G-Capture`** | the builder may add evidence, never a tick (`docs/gates/README.md`). `docs/gates/G-Capture.md` is a candidate with every row's evidence named and a reproduction block; it is **unsigned** |
| `G-Attach`, `G-Capsule`, `G-Index`, `G-Graph`, `G-Replay`, `G-Ops`, `G-Sec`, `G-Release` | nine gates are `pending` in `docs/STATE.json`; each needs a reviewer, a fresh clone and the commands in `docs/gates/README.md` |
| The five-minute install measured on **three strangers** (§24 usability) | only a stranger can say whether it took five minutes |
| The licence | `pyproject.toml` says Apache-2.0 in its metadata; there is **no LICENSE file** and adding one is the owner's decision, not the builder's |
| Publishing: a tag, a PyPI release, a signed installer | the owner's decisions; nothing here is tagged or published |

## 5. What a user can do with this build today

* Install it from a wheel into a fresh environment and record a real `openai` or `anthropic` program without touching the
  program's code; `curl`, Node and any HTTP client are recorded through the proxy.
* Recover a capsule from a process that was killed mid-stream, verify it, read the diagnosis, and open the offline page.
* Ask for a repair, review the sheet, apply it with the token, and run the tests the proposal suggested — each step
  refused until the previous one is explicit.
* Be told, in one sentence and with the flag that fixes it, when a run recorded no model call.
* Run `blackbox doctor` when something does not work and get a table of what is fine, what is not, and what could not be
  checked.
* Read `docs/limitations.md` and know which of the above has been seen by a human and which has been seen by a test.

## How to reproduce this page

```sh
git clone --branch arena/5a927240-blackbox <repo> && cd Blackbox
python -m venv .venv && .venv/bin/pip install -e ".[dev,capture,capture-sdks]"
PYTHONPATH=src .venv/bin/python -m pytest tests -q                 # 1 556 collected
.venv/bin/python bench/journey.py --out /tmp/journey.json          # the seven-step journey, fresh venv
sh tools/package/linux/build_deb.sh --out dist                     # the package (network once, for pywebview)
PATH="$PWD/.venv/bin:$PATH" sh tools/desktop_journey.sh --automated-only --out /tmp/desktop.json
.venv/bin/python bench/bench_stream_identity.py --n 10000          # about 15 s here
.venv/bin/python tools/outage_split.py --sample /tmp/defect108.json
```

Nothing in this page is a promise about a machine that was not available. The two sentences that matter:
**no gate is signed**, and **the desktop window has not been seen on a real desktop**.
