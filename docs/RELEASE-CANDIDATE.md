# Release candidate — what was built, what was measured today, and what is still somebody else's to do

**Verdict: internal preview, not a release candidate.** The product is `0.1.0.dev0`, no tag, no published package, and no
gate signed by an independent reviewer. What follows is the evidence-backed split the release checklist asks for, written
on 2026-11-20 from this checkout (`arena/86af3334-blackbox`, cut from `03c781a`, the merge of PR #21) at `133b0c3`, whose
CI run `44809947917` is green on all eleven suite cells — three operating systems × three Pythons, macOS included — plus
`docs` and `go-reader`.

Machine: Debian 12 (bookworm) x86_64, 2 vCPU, no display, no GTK/WebKit, no Windows host, no Go toolchain, no root,
Python 3.11.2 with `pytest` 8.3.4, `hypothesis` 6.122.3, `httpx` 0.28.1, `openai` 3.51.0, `anthropic` 2.8.0.

## 1. Development work completed today

| What | Evidence |
|---|---|
| **The real-user journey runs from a wheel in a fresh venv, end to end** — install, attach an unmodified `openai` app, capture, list, verify, diagnose, view, approve a repair with its token, run the authorised verification | `bench/results/2026-11-21-journey.json` — all seven steps, one pass, no fixtures; rerun at this branch's head (`f86c3d1`), wheel from that tree, `openai` 3.51.0 and `anthropic` 2.8.0 installed beside it |
| **The `.deb` builds here and runs from the extracted tree** — 373 entries, a launcher, the icon, the MIME type, the vendored window backend | `dist/blackbox_0.1.0.dev0_amd64.deb`; `tests/test_desktop_package.py` (9) |
| **The Linux desktop journey's automated half ran** — record, list, verify, diagnose; the window steps are recorded as *not attempted* because there is no display | `bench/results/2026-11-21-desktop-journey-automated.json`, rerun at this branch's head: `verify_exit 0`, `diagnose_exit 0`, six window answers `not-attempted`, verdict `automated-only`. `run_exit 1` there is the sample program's deliberate failure, which the script explains as it does it — the same file also from 2026-11-20 is kept beside it |
| **A package upgrade cannot take the recordings, checked from the built artefacts** — the `.deb` ships nothing outside `/usr`, carries no `postrm`/`prerm`, and its `postinst` never mentions the data directory; `Version:` is orderable by `dpkg --compare-versions`; the Windows job compares the `.bbx` count across a second install | `tests/test_desktop_package.py` (2 tests, built package), `tests/test_windows_installer_workflow.py` (the upgrade step), run `44868905784` |
| **`blackbox doctor` exists** — six probes (environment, capture, tape, hooks, storage, locks), `--json`, exit 0/8 | `src/blackbox/cli/cmd_doctor.py`, `tests/test_error_advice.py` |
| **Six error remediations that named commands this build did not have are corrected**, and a test keeps it that way | `src/blackbox/errors.py`, `docs/errors.md` (regenerated), defect 111 |
| **Proxy response framing fixed** — a body with no length and no chunking was answered with `Transfer-Encoding: chunked` over raw bytes; `curl` refused the stream | defect 109, `tests/proxy/test_response_framing.py` (5) |
| **A derived capsule part built from the wall clock no longer fails its own check** — `verify` exits 9 with `BBX-E412` on a correctly sealed capsule, before the fix | defect 110, `tests/test_attach_curl_kill.py` (2) |
| **§24a's `run -- curl …` + `kill -9` row closed** — the in-flight call survives, byte counts never exceed what the origin pushed, and the capsule verifies | `tests/test_attach_curl_kill.py` |
| **§24a's 10 000 streamed-response byte-identity row measured** — 10 000 of 10 000 identical, 2 195 560 bytes, p50 1.15 ms per response, plus a control that isolates a single corrupted response | `bench/results/2026-11-20-stream-identity.json`, `…-control.json`, `bench/bench_stream_identity.py` |
| **Defect 108 read from its own recorded sample** — the 686.2 ms outlier contains **0 ms** of rebind work; a tool now splits every sample into rebind / dispatch / tick / unexplained, and the loop's own callback exceptions are recorded rather than only printed | `tools/outage_split.py`, `bench/results/2026-11-20-defect-108-split.json`, `tests/test_outage_split.py` (7) |
| **User-facing documents a beginner needs** — `troubleshooting.md`, `privacy.md`, `limitations.md`, an index in `docs/README.md` keyed to what the reader is trying to do | this `docs/` directory |
| **The Windows installer is built, installed, installed again over itself, and uninstalled on a real Windows runner** — the staging script, `iscc`, a silent install into `%LOCALAPPDATA%\Programs\BlackBox`, the installed interpreter's `--version` and `doctor --json`, a second install of the same artefact (the overwrite path an upgrade takes), `--version` again, the uninstaller, and the recordings still present after all three | `.github/workflows/package-windows.yml`, run `44868905784` (green, twelve runs in); artefact `BlackBox-Setup-0.1.0.dev0.exe`, 12.4 MiB, sha256 `a883f9a3d755c9b6dd09b3afe8f49b4bb9da2d0c70f542aca87d3477778778dd`. What it is not: one artefact twice is the overwrite path, not a newer version replacing an older one |
| **Eight failures on the way to that run, each a real defect in the packaging path** — no `ensurepip` in the embeddable Python, a wheel directory the host pip never wrote, a drive letter the CLI received as `D` (defect 116, cause unsettled), an output directory resolved against the `.iss`, `Start-Process -Wait` waiting on a process tree, and a privacy `MsgBox` that ignores `/SUPPRESSMSGBOXES` | `docs/defects.md` 114–116, `tools/package/windows/README.md` (one row per run), `tests/test_windows_definition.py` (12) |
| **A beginner-facing walkthrough that is checked against the product** — install → attach → confirm → open → findings → brief → approve → run the checks, with every command, refusal code and file path asserted by a test | `docs/getting-started.md`, `tests/test_getting_started.py` (14, including three that check the page's flags against `blackbox <verb> --help` and one that records in a temporary directory and looks — defect 122) |
| **Six review packs for independent reviewers**, each row naming the command that settles it and what this machine saw — and none of them a gate file or a signature (`docs/gates/README.md` forbids a builder ticking) | `docs/reviews/`, `tests/test_review_packs.py` (5) |
| **The recovery claim is published whole** — the lock used to be created empty and written afterwards, so a peer could read nothing, judge it stale and take a live claim; `44820329776` produced two recoverers for one run that way | defect 101 (update 3), `tests/test_recovery_identity.py` (5, two of them red against the old code) |
| **Two CI-only failures turned into messages that can name their cause** — a macOS stall dump holding one thread (defect 118, now also stated in `docs/limitations.md`), and a killed `curl` run reporting no node statuses (defect 119); neither reproduced here, both now print the numbers that would settle them | `docs/defects.md` 118–119 |
| **Six consecutive CI runs green on all eleven cells, both events, at the last three commits** | runs `44828064353`, `44828074962`, `44828434391`, `44828441722`, `44828446885`, `44828461054` |
| **The index build's cost is measured, not just its dump** — one transaction per build instead of one per capsule: 1 000 commits → 2, incremental 922.2 ms → 311.7 ms, rebuild 1 068.2 ms → 298.7 ms over 500 capsules, dump hash unchanged (defect 114) | `bench/results/2026-11-21-index-build-cost.json`, `tests/test_index_rebuild.py::test_a_build_of_many_capsules_commits_once` (red against the old code) |
| **An upgrade cannot take the recordings, checked from both artefacts** — the `.deb` ships nothing outside `/usr`, has no `postrm`/`prerm`, and its `Version:` is orderable; the Windows job installs the same artefact a second time over the first, runs the upgraded tree, uninstalls, and counts the `.bbx` files across all three | `tests/test_desktop_package.py` (2 new), `.github/workflows/package-windows.yml`, run `44868905784` |
| **A timing assertion that failed a Windows cell measures the product again** — `ingest` reported 155 ms over the front door against an 80 ms bound on a commit touching no hook code, and green on the next; it is now five interleaved pairs with the same bound (defect 120) | `tests/test_hook_inbox.py`, `docs/defects.md` 120 |
| **The static tests survive an interpreter bug in the parser** — CPython 3.11 raised `SystemError: AST constructor recursion depth mismatch` once, mid-suite, on a file that parses alone; one retry for that error, everything else raised (defect 121) | `tests/_ast_source.py`, `tests/test_diagnose.py` |
| **The full suite is green on this machine**: 1 601 collected, 0 failed, 2 skipped (both POSIX-by-name), `pytest -q` exit 0 | `PYTHONPATH=src .venv/bin/python -m pytest tests -q` |

## 2. Checks that still need a real Linux desktop or a Windows machine

| Check | Why it cannot run here | What would settle it |
|---|---|---|
| The window opens, the setup page lists what it found, the library lists a capsule, a run opens, *check* recomputes, *diagnose* renders, the window closes cleanly | no display, no `python3-gi`, no `gir1.2-webkit2-4.1`; the code path is exercised against a fake webview and the pages are fetched over loopback | `sh tools/desktop_journey.sh` on a Debian/Ubuntu desktop, answers to six questions, the JSON pasted into `docs/desktop.md` |
| `apt install ./blackbox_0.1.0.dev0_amd64.deb`, then `blackbox desktop` | no root in this sandbox; the package is built and extracted, not installed | the same command on a machine you may install on |
| Windows: **install the CI-built installer with a mouse**, launch from the Start menu, record with your own key, open the capsule in the window, uninstall, check the recordings are still there | the build half is done in CI (`44804247919`: compile, silent install, installed `--version` + `doctor`, uninstall, recordings kept); a runner has no wizard, no shortcut and no window, and this machine has no Windows at all | `tools/package/windows/README.md` is the hand-off — run it on Windows 10 and 11 and replace the row in `docs/desktop.md`. Signature is still the owner's certificate decision |
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
* On Windows, install the unsigned CI-built installer, run it from the Start menu (the CLI and the installed tree are
  exercised unattended in CI; the window is not), and uninstall it without losing the recordings.
* Follow one ordered path from nothing to a finished incident — `docs/getting-started.md`, eight steps, each with what to do
  when it goes wrong.
* Be told, in one sentence and with the flag that fixes it, when a run recorded no model call.
* Run `blackbox doctor` when something does not work and get a table of what is fine, what is not, and what could not be
  checked.
* Read `docs/limitations.md` and know which of the above has been seen by a human and which has been seen by a test.

## How to reproduce this page

```sh
git clone --branch arena/86af3334-blackbox <repo> && cd Blackbox
python -m venv .venv && .venv/bin/pip install -e ".[dev,capture,capture-sdks]"
PYTHONPATH=src .venv/bin/python -m pytest tests -q                 # 1 588 collected
gh run list --workflow package-windows.yml --limit 1               # the installer's own run
.venv/bin/python bench/journey.py --out /tmp/journey.json          # the seven-step journey, fresh venv
sh tools/package/linux/build_deb.sh --out dist                     # the package (network once, for pywebview)
PATH="$PWD/.venv/bin:$PATH" sh tools/desktop_journey.sh --automated-only --out /tmp/desktop.json
.venv/bin/python bench/bench_stream_identity.py --n 10000          # about 15 s here
.venv/bin/python tools/outage_split.py --sample /tmp/defect108.json
```

Nothing in this page is a promise about a machine that was not available. The two sentences that matter:
**no gate is signed**, and **the desktop window has not been seen on a real desktop**.
