# Release-gate readiness, as the builder finds it — **not a gate file, not a verdict**

`docs/gates/README.md` says a gate is `G-<name>.md`, written by the **reviewer** after a fresh clone and a re-read of
every must-be-true row. This file is the opposite of that, and is written so the opposite is visible: it is the
**builder's** index of `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` §24 and §24a — one row per acceptance line, with
where the evidence sits in the tree today and what is missing. It exists because a blocker list is not a gate list, and
saying "B4, B5 and B6 are what is left" without reading §24 would be a claim about a checklist nobody had opened.

Every "state" below is one of:

| state | meaning |
|---|---|
| **evidenced** | a test or a committed artefact under `bench/results/` covers the row on this branch; a reviewer still has to re-read it |
| **partial** | some of the row is covered and the uncovered part is named |
| **missing** | no code, test or artefact for the row was found in this tree by this review |
| **humans** | needs people, not code: a second reader, three strangers, a licence decision |
| **machine** | needs a host this sandbox is not (`docs/RELEASE-BLOCKERS.md` B6 and the rows below it) |
| **OS** | needs Windows or macOS, which this sandbox does not have; CI covers part of it |

Nothing here is a tick, and the builder may not tick (`docs/gates/README.md` rules 1–2). Where this file says
"evidenced", read "the builder found a test or an artefact and a reviewer should check it", never "passed".

## §24 Guarantees

| Row (§24, verbatim) | What exists | State |
|---|---|---|
| `docs/guarantees.md` exists and every claim links to a test ID and a published result | `docs/guarantees.md` exists, and every test ID it names is now checked against the tree by `tests/test_guarantees_links.py` — a claim that points at a test file or a node id that does not exist fails the suite. The *published result* half stays a reviewer's read: the test proves the reference resolves, not that the number is right | **partial** (the reference half is test-enforced; a reviewer still has to read the numbers) |
| Kill matrix S1–S18 × 3 modes × 3 segment sizes green on Linux; Windows and macOS on the reduced matrix | `tests/kill/kill_matrix.py`, `docs/kill-matrix.md`; the 500-trial matrix and the 2 000-trial randomized run are recorded in `docs/STATUS.md`; S14–S18 are tests (`tests/test_crash_points.py`, `tests/test_proxy_supervisor.py`, `tests/test_hook_inbox.py`) | **evidenced** on Linux; **OS** for the reduced Windows/macOS matrix |
| 2 000 randomized `kill -9` trials: zero invariant violations, zero unclassified recoveries | `43748375862` (a `workflow_dispatch` nightly) ran 2 000 trials with 0 violations; the *scheduled* week the plan asks for has not started (`docs/STATUS.md` §Labelled, P2-09) | **partial** — the trials happened, the clock did not |
| Recovery idempotent: 10 runs ⇒ 1 capsule, identical sha256; concurrent recoverers ⇒ 1 winner | `tests/test_recovery_identity.py` (2): ten recoveries of one tape → **one** capsule, one sha256, nine `steps_run == []`; six **processes** recovering concurrently → one capsule, one root, at most one claimant, no traceback without a code. `tests/test_adoption.py` covers the writer's reopen path; `tests/test_capsule_determinism.py` the cross-directory determinism | **evidenced** (builder run; CI has not run this file) |
| Torn-frame, bad-CRC, zeroed-head, corrupt-meta produce the exact documented codes | `tests/test_fault_codes.py` (7) walks the four faults to their codes — `E222`/`E220`, `E220`, `E224`, and for a torn frame the *documented outcome* (`recovered`, `PROCESS_DEATH`, `truncated_tail`, `truncated_bytes`, `lost_from_seq`) — and requires each code to be in `errors.REGISTRY` and in `docs/errors.md`. Writing it found defect 94 (a torn last frame hid the whole segment) | **evidenced** (builder run; CI has not run this file) |

## §24 Capture

| Row | What exists | State |
|---|---|---|
| An unmodified `openai` **and** `anthropic` script records correctly with zero code changes | `tests/_day14_child.py` and `tests/_day14_child_anthropic.py` under `tests/test_day14_kill.py` (6 tests) | **evidenced** |
| Streaming: partial chunks captured; `MID_STREAM` interruption classified with byte counts | `tests/test_day14_kill.py`, `tests/test_stream_termination.py` (32 collected; STATUS says 30 — a drift a reviewer should note) | **evidenced** |
| Fail-open suite: recorder deliberately broken ⇒ app outputs and exceptions identical | `tests/test_failopen.py` (8), `tests/test_capture_failopen.py` | **evidenced** |
| SDK matrix declared; unknown version ⇒ safe mode, zero silent mislabels | `tests/test_attach_sdks.py`, `docs/attach.md` (every row dated or marked **unverified**) | **evidenced** |

## §24 Data

| Row | What exists | State |
|---|---|---|
| Frame/head/meta/segment/capsule formats frozen with conformance vectors; a from-scratch reader passes them | three readers, one document: `tests/conformance/vectors` + `tests/test_conformance.py` (Python), `tools/readers/bbx-read.mjs` (Node, 2026-11-19: all 7 CRC-32 vectors, ten hostile inputs refused) and **`tools/readers/go/` (P11-15)** — stdlib-only Go, compiled and run by the new `go-reader` CI job, which is **green on both workflow runs at `c0684a9`** (`44593741838`, `44593755018`): it built, read a capsule the Python build wrote, and produced the three named refusals | **evidenced** on Linux CI (the Go reader's own cell); Windows/macOS cells of that job are the same Linux image |
| `INTEGRITY.sha256` verifies; one byte tampered ⇒ named failure | `src/blackbox/cap/integrity.py`, `blackbox verify`, `tests/test_verify_cli.py` | **evidenced** |
| Index rebuild reproduces a canonical dump of the original (I8) on a 500-run corpus | `src/blackbox/index/` (five `STRICT` tables, idempotent ingest), `blackbox index --rebuild --dump`, `tests/test_index_rebuild.py` (9) rebuilding 500 real sealed runs and comparing canonical dumps; `bench/results/2026-11-16-i8-index-rebuild.json` **500 runs → 6 001 rows, incremental 891.5 ms, rebuild 885.9 ms, identical dump sha256 `8a652604…`, re-index adds 0 rows** | **evidenced** (was missing) |
| Retention + GC: a referenced blob is never deleted; a dry run explains every deletion | `tests/test_retention.py`, `tests/test_gc_hygiene.py`; **`blackbox retention`** (new) and `retention.explain()` give one row per segment — delete/keep/pinned/active with the reason and the numbers — and `tests/test_retention_dry_run.py` (6) proves the dry run writes nothing (no `GAP`, no unlink, unchanged tail seq) and that the real pass declares the span the dry run named. Writing it found defect 95 (`dry_run` committed its own `GAP`) | **evidenced** (builder run; CI has not run this file) |

## §24 Security / privacy

| Row | What exists | State |
|---|---|---|
| 60-secret corpus absent from all **11** sinks | eleven enumerated and asserted in `tests/test_redaction_every_sink.py`: the tape's frames, the capsule file, `otlp/spans.json`, `otlp/spans.pb`, `events/events.jsonl`, `graph.json`, `manifest.json`, the viewer page, the diagnosis report, the repair brief, **and the desktop pages** (`/`, `/library`, `/run/<capsule>`, fetched from the app's loopback server) — 60 of 60 secrets in each, with a redaction-off control | **evidenced** (builder run; CI has not run this file) |
| Capsule attack corpus (12) rejected with correct codes in < 50 ms | `tests/test_capsule_attacks.py` (25 cases): every §13.4 check the reader implements has an attack behind it — E401, E402 (non-zip and 5 001 entries), E403 (traversal, absolute, drive letter, backslash, empty component, symlink), E404, E405, E406 (encrypted and bzip2), E407 (six unreadable manifests, a newer version), E408, and the check-9 counter against a lying archive. Worst rejection **15.718 ms**, the rest under 1 ms. Writing it found defect 83 and closed three gaps. Checks 10 and 11 of §13.4 have nothing to exercise yet (no derived-part line reader, no generated tests) and are named as such | **evidenced** — builder run, awaiting a reviewer |
| 20-payload XSS corpus inert in viewer and Markdown export | `tests/test_xss_corpus.py` (5) sends twenty payloads through a **real capsule** into the viewer page, the desktop's `/`, `/library` and `/run/<capsule>` pages, the Markdown report and the brief, and the capsule's own bytes; the strings must survive as characters and no renderer may act on them. Writing it found defect 93 (the report escaped `\|` and nothing else) | **evidenced** (builder run; CI has not run this file) |
| Netguard: replay produces zero outbound connection attempts, including from child processes | **two layers**: `tests/test_no_egress.py` (5) statically forbids socket imports and URL literals in `src`; `tests/test_netguard_reads.py` (9) installs a runtime guard over `connect`/`connect_ex`/`create_connection`/`getaddrinfo`/`HTTPConnection.connect` and runs `verify`, `view`, `diagnose`, `capsule verify`, `ls`, `serve`, `desktop --selftest` and a `run` whose child is its own process — all complete, **none** attempts a connection, and a control proves the guard refuses | **evidenced** (builder run; CI has not run this file) |
| Data dir `0700` / files `0600` verified at startup on all three OSes | `platform/perms.py` (`inspect`, `audit`, `windows_findings`), used by the first-run doctor; `tests/test_permissions.py` measures **every** path a real run writes after the run (POSIX: exactly `0600`/`0700`) and reads the ACL with `icacls` on Windows. Writing it found defect 96 (`meta`, `seal`, `index.sqlite` at `0644`; `tape/`, `runs/` at `0755`) | **evidenced** (CI, 2026-11-16) — Linux measured here; the Windows arm runs only when `sys.platform == "win32"` and skips only if `icacls` cannot be run, so the green `py3.11`/`py3.12`/`py3.13 / windows-latest` cells at `1d42e0c` are that arm passing; the POSIX half is `perms.is_posix()`, true on macOS, and the three macOS cells are green at the same commit for the first time |

## §24 Usability

| Row | What exists | State |
|---|---|---|
| Clean machine → install → crash demo → `explain` in under 5 minutes, measured on 3 people | `docs/onboarding.md` (one command, three tasks), `bench/journey.py` (install → run → diagnose → view, all measured); nobody outside this session has run it | **humans** |
| Zero third-party imports in core (test-enforced); install works offline from a wheel | `tests/test_stdlib_only.py`, `tests/test_no_egress.py` and the policy module `tests/_capture_policy.py`; wheel install into a fresh venv proven by `bench/journey.py` (no `PYTHONPATH`, no editable install) | **evidenced** |
| `blackbox --help` ≤ 20 commands; every command has `--json` and a documented exit code | `docs/cli.md` (new) is the contract: 15 commands, each with its machine-readable form and exit codes; `tests/test_cli_contract.py` (21) checks the table's set equals the front door's, that every `--json` row is accepted, and measures the documented code on a failing invocation. `status`, `run` and `serve` gained `--json`; `link` (always JSON) and `ingest` (one line on stdout) are documented as such rather than flagged | **evidenced** (builder run; CI has not run this file) |
| Viewer opens from `file://` with no server and no network | `tests/test_ui_viewer.py`, and the journey checks the page for `http://` | **evidenced** |

## §24 Evidence

| Row | What exists | State |
|---|---|---|
| Published overhead table with hardware/versions/raw JSON | `docs/STATUS.md` §Measurements, ~20 rows, each naming its JSON | **evidenced** |
| Published prior-art log with a dated search and the "not found ≠ does not exist" disclaimer | `docs/BLACKBOX_ROUND1_PRIOR_ART_CRITIQUE.md` §11 | **evidenced** |
| (If Q7 = yes) published diagnosis benchmark with protocol, N, reviewers | `docs/benchmarks/diagnosis.md` — checklist prepared, no participants | **humans** |

## §24a Attach

| Row | What exists | State |
|---|---|---|
| `run -- node app.js`, `-- python app.py`, `-- curl …`, `-- claude` each produce a run with the in-flight call preserved after `kill -9`, zero changes | Node, Python and both SDKs: `bench/accept_p3b03.py` (7 clients, all verdicts true); the SDK `kill -9` half: `tests/test_day14_kill.py` (6). **`-- curl` with a kill: closed on 2026-11-20** — `tests/test_attach_curl_kill.py` (2) runs a real `curl` under `run`, kills it mid-stream with `SIGKILL`, and finds the call preserved with chunk and byte counts that never exceed what the origin pushed, on a capsule that verifies. **`-- claude` with a kill is still missing**: the CLI is not installed here and its install needs an account, so the row stays partial with that half named | **partial** (`curl` closed; `claude` not run) |
| Proxy byte identity: sha256 of client-received bytes equals upstream over 10 000 streamed responses | measured on 2026-11-20: `bench/bench_stream_identity.py --n 10000` streams 10 000 responses through the supervisor and compares each with what the origin generated — **10 000 of 10 000 identical**, 2 195 560 bytes, p50 1.15 ms per response (`bench/results/2026-11-20-stream-identity.json`), plus a control that corrupts one byte in flight and reports exactly that one (`…-control.json`). One host, one shape: loopback HTTP/1.1, four chunks per response, no TLS and no HTTP/2 | **evidenced** (builder run) |
| `x-blackbox-run` never reaches a mock provider from an un-proxied app | `tests/test_headers.py` (7): nothing is injected unless the launcher said a proxy is in front, a proxy variable with no run id injects nothing, an un-proxied SDK request carries nothing of ours, and the proxy strips every `x-blackbox-*` header before forwarding — asserted against a mock provider that records what reached it | **evidenced** |
| Hook adapter install/remove round-trip leaves foreign settings byte-identical | `tests/test_hook_claude_code.py` (13); the same property is asserted for the second adapter in `tests/test_hook_claude_code.py` and `tests/test_hook_launcher.py` | **evidenced** |
| `docs/attach.md` has a verified version/date or **unverified** on every row; README carries §11.12 verbatim | `docs/attach.md`, `tests/test_attach_doc.py` | **evidenced** |

## §24a Tape and chain

| Row | What exists | State |
|---|---|---|
| Kill stages S14–S18 green in all modes; S17 proves no `SIGBUS` reaches the app | S16/S18 are tests; S17 needs a sparse file on a full filesystem and the `sudo` control (`tools/s17_control.sh`) is **humans** | **partial** + **humans** |
| `blackbox verify` detects a one-byte change in any sealed segment and names the segment and seq; a retention `GAP` verifies as a declared hole | `tests/test_verify_cli.py`, `tests/test_retention.py` | **evidenced** |
| A Go reader written from `docs/format.md` alone decodes the conformance vectors (P11-15) | **`tools/readers/go/` (2026-11-16)**: `main.go`, `go.mod`, `selftest.sh`, README; 7 tests in `tests/test_go_reader.py` check stdlib-only imports, registry-declared refusal codes, the selftest's shell syntax and that the `go-reader` CI job runs it. **Not compiled or run on this machine** (no Go toolchain): the CI cell is the measurement and it is not yet observed green. What does exist (2026-11-19) is a reader in a **different language** written from the same document — `tools/readers/bbx-read.mjs`, Node, no dependencies — which verifies a real capsule, reproduces the 7 CRC-32 vectors and refuses ten hostile inputs with the same codes as the Python reader (`tests/test_independent_reader.py`, 16 tests). It found defect 85, three gaps in the document. That is the property the row is after. **2026-11-16: the `go-reader` job is green** at `c0684a9`, `8ba986e` and now `1d42e0c` (both workflow runs): it compiled the reader, reproduced the seven CRC-32 vectors, read a capsule the Python build wrote and produced the three named refusals. The reader is still not compiled on this machine — no Go toolchain here — but the row's measurement exists | **evidenced** |

## §24a Triggers and parts

| Row | What exists | State |
|---|---|---|
| `stop_delay`, `cooldown`, `max_per_24h` each have a passing test with a mocked clock; a rule that raises disables only itself | `src/blackbox/trigger/{rules,engine}.py`, `[[trigger]]` in `blackbox.toml`, keeper writes each decision as a `MARK`; `tests/test_triggers.py` (27) holds the three timed fields against a hand-moved clock, the 24 h cap across a restart (`triggers.json`), fail-open, and an end-to-end run whose `exit_code` rule fires into the capsule. **Gaps named in `docs/triggers.md`:** only `exit_code`/`stall_ms` have producers today, `freeze` records `freeze_due` but does not seal a mid-run capsule, `snapshot` carries no stack dump | **partial** (was missing) |
| `events.jsonl` and `otlp/spans.json` regenerate byte-identically from raw frames; drift ⇒ `BBX-E412` | `blackbox verify`'s `derived_parts` check; `src/blackbox/cap/derived.py`, `export_jsonl.py`, `export_otlp.py` | **evidenced** |
| Serverless wrapper leaves no open run after 1 000 simulated invocations with random exceptions and timeouts | `src/blackbox/serverless.py` (`wrap`, `serverless` mode, cold-start recovery, marker, `on_capsule`), `tests/test_serverless.py` (10), `bench/serverless_1000.py` → `bench/results/2026-11-16-serverless-1000.json`: **1 000 invocations (751 ok, 101 timeout, 81 error, 67 killed), 1 000 capsules all verifying, 0 open runs, 205.7 s** | **evidenced** (was missing) |

## What this review changes about "the remaining blockers"

`docs/RELEASE-BLOCKERS.md` lists what a **user** hits, and its open entries are B4 (the owner's tick), B5 (five
unmeasured readers) and B6 (two rows that need a named machine). Reading §24 and §24a says that list is not the release
list, and the difference is not small:

As of the 2026-11-16 build, of the 36 rows §24 and §24a lay out this index finds **27 evidenced**, **7 partial**, **no row
with nothing behind it at all**, and **2 that need people** (counted by each row's *final* state below, so a row that is
partial on one half and needs a machine on the other is counted once — several of the partial rows still need Windows,
macOS or a quiet host, which is why 27 + 7 + 2 comes to 36 and not to more). **Two rows moved on 2026-11-16 evening, both
on evidence this sandbox cannot produce**: the Go reader's CI cell (P11-15) went green and stayed green across three
commits, and the permissions row's Windows and macOS halves stopped being "never run" — the `1d42e0c` run is the first in
this branch's history where all eleven suite cells (3 OSes × 3 Pythons) plus `docs` and `go-reader` are green, macOS
included, after eleven commits of macOS cells sitting in `queued`. Six of the movements in this revision — the
four-fault code table, retention's dry run, the eleven sinks, the twenty-payload corpus, netguard at run time and the CLI
contract — came with defects found by writing them (93–96, `docs/defects.md`), and one of the six rows that moved is still
partial on purpose: it has since moved, when the Windows and macOS cells of `1d42e0c` ran its other two halves.

* **Four rows had no code or artefact in this tree at all**: the 500-run index rebuild (I8), `docs/format.md`'s Go
  reader (P11-15), the trigger rules (`stop_delay` / `cooldown` / `max_per_24h`) and the serverless wrapper. **On
  2026-11-16 three of the four landed** — I8 with a measured 500-run rebuild, the trigger rules with every timed field
  tested against a mocked clock (partial: producers and mid-run freeze still open), and the serverless wrapper with 1 000
  measured invocations. The Go reader exists and CI compiles and runs it; that cell has not yet been seen green. The capsule
  attack corpus was the fifth until 2026-11-19, when `tests/test_capsule_attacks.py` moved it out of this list. None of these appear in `docs/RELEASE-BLOCKERS.md`, because none of them is something a user of the
  current build would hit — which is exactly why the blocker list cannot be read as the gate list.
* **Seven rows are partial**, each with the uncovered part named in its cell. The ones worth a reviewer's time first are the
  rows whose remaining half is a *machine*: the Windows desktop journey (the installer is compiled, installed and
  uninstalled by CI at `44804247919`; the wizard, the shortcut and the window still need a Windows desktop) and the two
  matrix rows. The Windows permissions arm is no longer one of them — it ran green at `1d42e0c`.
* **Twenty-seven rows are evidenced** on this branch — a test or a committed artefact exists, and a reviewer still has to
  re-read it. That is the honest sense in which this branch is close: close on the mechanism, not on the paperwork.
* **Three rows need people** (the 3-person onboarding test, the blinded diagnosis benchmark, the S17 `sudo` control) and
  the licence/tag/merge decision is a fourth that lives outside them; **the release machine** owns the two B6 rows plus
  every quiet-box number, and Windows/macOS own two more rows that no sandbox can run.

So: the honest statement is that the blocker list is shorter than the gate list, and `docs/gates/` — not
`docs/RELEASE-BLOCKERS.md` — is where a release is decided.
