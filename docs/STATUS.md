# BlackBox build status — 2026-11-16 — trunk `1c3db9d` — gates: **G-Promise pass** (built and self-reviewed at `af908bc`, independently counter-signed at `fdb3875`; see `docs/gates/G-Promise.md`), G-Capture, G-Attach, G-Capsule, G-Sec, G-Release not yet opened

**Branch** `arena/4de12d5a-blackbox` · **base** `e8cd2f6` (PRs #10 and #11) · CI at `897803f`: run
`43751123140`, ten jobs, ten `success`. Before it `6a8a742` (`43749402312`) was ten of ten — the first green run
containing the hook path — and two of the runs between were nine of ten for reasons that were not the product:
`4f285ed` (`43749014701`, three Windows cells, defect 37: a POSIX path join in a new test) and `c411a53`
(`43750531880`, one macOS cell, defect 38: a byte-bound test a 100 ms coalesce window could answer). `ac25b4a`
(`43744646557`), `e18ce61` (`43744575973`) and `8a8b58b` (`43743421915`) were ten of ten.
**The nightly has run green once**: `43748375862` on `main` `1390c9b` — both jobs, `kill-matrix` (2000 randomized
`kill -9` trials) and `cbor-fuzz` (100 000 buffers). It was a **`workflow_dispatch`**, not a `schedule`, so the P2-09
clock is started but not yet evidenced by the event the plan asks for.
`8a8b58b` was the first commit to take the whole 3 OS × 3 Python matrix green, **py3.12 included**, since the proxy
landed. `7aa5105` (`43727756275`), `72fdc8a` (`43741188969`), `70420ed` (`43741876100`) and
`a54dda0` (`43742600971`) were each red on their three 3.12 cells and green on the other seven, and the failure was a
hang, not an assertion (defect 35, D-037): one `asyncio.run()` loop hosting the client, the proxy and the echo origin,
which 3.12 parks in `select()` with nothing runnable. The test runs them on three threads now, and the CI that reports
it was rebuilt in the same four commits — a watchdog that names the overrunning test, its report in a file rather than
down a descriptor, and one fixed-shape annotation per run instead of ten `pluggy` frames. Before this branch,
`1c3db9d` (`43710478316`), `50d6b37` (`43710184175`), `c869d9e` (`43707004115`) and `d43b208` (`43706220024`) were ten
of ten — the first two of those are the proxy, on every OS.
**`main` has moved and the nightly clock has started**: PRs #2, #3 and #4 are merged (`e8c3f6f`, `dd92c7b`, and this
one), so `origin/main` is no longer `31241d6` and item 0b below is done. `705fc59` (`43705488648`) and `9fe6d72`
(`43705818997`) are **red and stay red** (defect 31: seven cells failed on the new key code — the creator of the key
file creates it before writing it, so a reader could see zero bytes; and two tests asserted POSIX-only facts), and
`d43b208` is the fix verified on Windows, macOS and Linux. Before that `4f42397` (`43705007628`), `4ce0f7c`
(`43704041733`), `07e5c29` (`43703761892`) and `ce1439b` (`43701652852`) were ten of ten, and the five before those —
`47ec45a` (`43701107412`), `da6ef3e` (`43694261034`), `d901811` (`43698231222`), `43697929744` at `54c3aba` — ended
four commits of Windows red on defect 25, with `846e01a` (`43697198262`) the **first all-OS green since `c7919f9`**.
This note's own commit is docs-only and its run is pending when it is written, which is what the `STOPPED` line
reports. The three Windows cells had been red for four commits and the cause was mine: the Windows teardown fixture in
`tests/conftest.py` called `getattr(obj, "_head", None)` on every object in `gc.get_objects()`, `anthropic` keeps its
optional dependencies behind a `LazyProxy` that raises `MissingDependencyError` when the extra is absent, and the
teardown error landed on nine innocent property tests once `capture-sdks` put the SDK in the environment (defect 25).
Found through the check-run **annotations**, not the job log: GitHub redirects logs to a host this sandbox cannot reach,
so `tools/ci_summary.py`'s `::error::` lines are the only readable failure trail — they now carry 600 characters
instead of 200. Older red runs keep their numbers and their verdicts — `af908bc` (`43678767989`, 9/10, C40), `15cb55c`
and `6d52088` (7/10, a fork-only multiprocessing test) — because a later green tree is not a green commit.

**product: pre-alpha — the recorder, the tape, recovery, the launcher, the SDK lifecycle/decorator/node layer, the
loopback mock provider and capture seams A (client hooks over `httpx` **and** `httpx2`) and B (transport byte tee)
with the ordered preflight are real and tested, as is seam C (SDK method patches, against the real `openai` 3.51.0 and
`anthropic` 2.8.0), the tape sink that turns seam events into `MODEL_CHUNK` frames, and the day-14 rule: an unmodified
`openai` script killed with `SIGKILL` mid-stream is recovered with chunk and byte counts that never exceed what the
provider pushed. **One seam is now installed by the product itself**: `api.install()` asks the preflight for a seam once
its tape exists (defect 49), so `blackbox run -- python app.py` records the child's own vantage and not only the proxy's,
and a second installer is handed the seam that exists instead of patching the same client class twice (D-051). The
proxy's **core and routing** are real and tested (P3B-01/P3B-02: loopback listener, byte-exact passthrough, the route
table as the egress allowlist); the launcher integration (P3B-03), the hook adapters (P3B-05), the repair applier
(P4-18), the viewer (P4-11) and the diagnosis workflow (P4-17) are built and tested row by row below. What is **not**
built is named there rather than implied: the graph's edges now reach the *node* when both writers recorded a span
(the proxy opens a `proxy_call` span per forwarded request, joined by the call id both write — node-level edges measured
on a real instrumented client in `bench/results/2026-11-18-b4-node-edges.json`), and still fall back to `level: "call"`
where one side has no span, which is every uninstrumented client and every Node client, because `api.run` is
Python-only. `docs/node-edges.md` is now the implementation note with that measurement; the owner's decision
(2026-11-16) is that P3B-03 and defect 55 stay open, and nothing here ticks them.**

**The three things this branch was asked to prioritise are done, and `docs/RELEASE-BLOCKERS.md` carries each with its
measurement.** (1) A run that captured nothing now says so: `blackbox run` prints one sentence after the summary when the
run had a proxy, recovered capsules, and recorded zero model calls, naming the cause (an adopted base URL) and the flag
that fixes it (`--force-proxy`) — B2, `tests/test_launcher_capture_warning.py` (5), verified end-to-end through the
installed CLI. (2) The connection ceiling has a policy instead of an accident: a request past `MAX_INFLIGHT` waits
for a slot, a wait past 60 s is answered `503 queue-timeout`, a client past `MAX_CONNECTIONS` is answered `503
connection-limit` in milliseconds — and both caps are **planned from the descriptor budget the host actually has**
(`limits.fd_budget()` → `server.plan_caps()`), shrinking to fit a 256-descriptor macOS shell or a hard-limited container,
stopping at floors with the reason recorded when the budget is below what the proxy needs, and stated as policy rather
than arithmetic on Windows, where no such budget is published. A delay and a rejection are named separately in the run's
sentence, and the rejected request's body says what to do — B3, `tests/test_proxy_backpressure.py` (14),
`tests/test_proxy_limits.py` (7), measured in `bench/results/2026-11-16-b3-connection-limit.json` (450 of 450 served,
probe answered in 948.9 ms instead of timing out at 4 s, 96 surplus clients refused in a median 4.0 ms), end to end on a
hard-200-descriptor host (62 connections + 42 in flight, 8 of 8 requests served), at the 256-descriptor plan (80 of 80
served, 73 queued, longest wait 50 ms), with 50 descriptors already open on a 320-budget host (plan 104/70, peak 87
descriptors), and against abrupt clients that leave while queued, at the ceiling, or mid-stream (slot and connection both
returned; the queued-departure limitation is in the README). The plan now also subtracts what the process already holds
(defect 72). (3) Stream termination is recorded in
three states rather than two, across seven provider conventions, a stream whose ending this recorder does not recognise is
recorded as `unverified` and reported as **cannot tell** — never as complete — and a convention is matched to the *end* of
the path, so a batch listing is not judged as a stream — defects 68 and 70, `tests/test_stream_termination.py` (30). The
zero-capture sentence now offers the base URL as **one possible cause** rather than asserting it, naming the alternative (a
call that never left the process). (4) `blackbox verify` takes **one capsule or many**: a quoted pattern is expanded by
the command (so the same line works on POSIX, `cmd.exe` and PowerShell), every capsule is verified and printed, the exit
code is the worst outcome of the batch, and a pattern matching nothing is named rather than silently shrinking the set —
B8, `tests/test_verify_cli.py` (23, five new). What this file's blocker list still calls open is B4, B5 and B6 —
which is a summary of `docs/RELEASE-BLOCKERS.md`, **not** the release gate list: `docs/gates/` is the authority,
and B4's remaining part is an owner's tick while B6 and the macOS rows need a machine this sandbox is not.
`docs/gates/READINESS.md` is the builder's row-by-row index of that checklist — of §24/§24a's 36 rows it finds 14
evidenced, 12 partial, 5 with no code in the tree at all (the I8 index rebuild, the capsule attack corpus, the Go reader,
the trigger rules and the serverless wrapper), 3 needing people and 2 needing another OS.

## Six gate rows moved from "not found" to measured, and four defects came out of it (2026-11-16)

The §24 rows this build was furthest from were all *review findings*: behaviours the code may well have had, none of
which had a test that could fail. Each new file below was written to fail on a plausible version of the code, and four of
them did — defects 93, 94, 95 and 96, in `docs/defects.md`, each with the fix and its regression assertion.

**Recovery identity** (`tests/test_recovery_identity.py`, 2). Ten recoveries of one tape produce **one** capsule with one
sha256 and nine `steps_run == []` results that say they skipped; six **processes** recovering at once elect one winner,
agree on its root, and never produce a second file.

**The four-fault table** (`tests/test_fault_codes.py`, 7). Corrupt `meta` → `BBX-E222` on a direct read and `BBX-E220`
through recovery (the head commits that checksum, so the earlier check fires); a zeroed `head` → `BBX-E220`; a flipped
body byte after sealing → `BBX-E224`; a torn last frame → **no exception**, `recovered`/`PROCESS_DEATH` with
`truncated_tail`, `truncated_bytes` and `lost_from_seq`. Writing it found **defect 94**: a torn last frame made the whole
active segment scan as empty — a crash in the final 88 bytes hid the entire run. `frame.last_valid_frame_end` resyncs to
the last frame that verifies, and the test asserts five of six frames survive.

**Retention's dry run** (`tests/test_retention_dry_run.py`, 6). It found **defect 95**: `retention.apply(dry_run=True)`
committed the `GAP` it was only meant to describe, so looking at a plan declared a hole over data still on disk. The dry
run now returns before any write, reporting `would_declare`; the test asserts the files, the frame count, the tail seq and
the absence of a `GAP` are unchanged, and that the real pass declares exactly the span the dry run named. New:
`retention.explain()` and **`blackbox retention`**, one row per segment — `delete`/`keep`/`pinned`/`active` with the reason
(the keep window, the budget, the tape's size). It cannot delete, by design: deletion has to commit a `GAP` first.

**Twenty payloads, eleven sinks** (`tests/test_xss_corpus.py` 5, `tests/test_redaction_every_sink.py` 11 now). The corpus
goes through a real capsule into the viewer, the desktop's `/`, `/library` and `/run/<capsule>` pages, the Markdown report
and the brief, and the capsule's own bytes. It found **defect 93**: the report's timeline escaped `|` and nothing else, so
`<script>` reached a Markdown renderer as HTML. `report.inline()` now escapes `& < > \`` and `|` for every recorded string
outside a fence. The redaction file now enumerates the eleven sinks the row names — adding the tape's frames and the
desktop pages, which were the two missing from the previous nine.

**Netguard at run time** (`tests/test_netguard_reads.py`, 9). `test_no_egress.py` is static; this installs a `sitecustomize`
that replaces `connect`, `connect_ex`, `create_connection`, `getaddrinfo` and `HTTPConnection.connect` with a logger that
refuses, then runs `verify`, `view`, `diagnose`, `capsule verify`, `ls`, `desktop --selftest`, `serve` and a `run` whose
child is its own process: every one finishes normally with **zero** attempts in the log. A guard that never fires would
make those tautologies, so one test proves it refuses `getaddrinfo('example.invalid', 80)`.

**Permissions measured after a real run** (`tests/test_permissions.py`, 4 + 1 Windows-only). It found **defect 96**:
`meta`, `seal`, `index.sqlite` were `0644` and `tape/`, `runs/` were `0755`, while the leaf directory was `0700` — the
promise covered the tree's leaves, not its branches. `platform/perms.private_dir`/`private_file` fix it, and the test now
requires **every** path a real run writes to be exactly `0600`/`0700`. The Windows arm reads the ACL with `icacls` and
fails on `Everyone`/`BUILTIN\Users`/`Authenticated Users`/`INTERACTIVE`; **it has never run** — that is the cell a Windows
machine still owns. The first-run doctor reports the same measurement, so a data directory unpacked with the wrong modes
warns the user instead of failing only in CI.

**Two CI failures from `1628d5a`, repaired as tests rather than by relaxing a product bound.** The Windows `py3.11` cell
in run `44581057445` failed two tests, and both were measuring the runner: the hook-latency test compared `ingest`
against a *bare interpreter* (160 ms of Windows interpreter and package layout before any of our imports ran) and the
determinism test used a pid as a needle when that pid was 1280, making the needle `00 00 05 00` — a byte pattern the
archive contains anyway. `ingest` is now compared against `blackbox --version` (the same machine's own front door, so the
delta is what `ingest` costs), and a needle containing a zero byte is not used. Defects 99 and 100; the machine-independent
half of both assertions (the import list, `manifest.created_ns == 0`) was already there and is untouched.

**CI, observed rather than hoped** (2026-11-16, `c0684a9`): the new **`go-reader` job is green on both workflow runs**
(`44593741838` push, `44593755018` PR) — Go 1.22 built `tools/readers/go/`, it read a capsule the Python build wrote, and
it produced the three named refusals. That is the first measurement of the second-language reader, and it moves P11-15
from "written, never compiled" to "compiled and run in CI". `docs` green, **Ubuntu 3/3 green on both runs**, and
**Windows `py3.11` red** on the new concurrency test: six recoverers racing on Windows crashed with an uncoded exception
because the claim path only caught `FileExistsError` (defect 101). Fixed here with `_lock_conflict`
(`FileExistsError` or WinError 5/32/33, with a failed unlink treated as the same race); the Windows cells are the cells
that will confirm it. macOS cells were still queued when this was written.

Then `940577a` showed the other half of the Windows story: `py3.11` and `py3.12` were killed at the **15-minute harness
budget** while `py3.13` passed in 14.6 and 13.2 minutes — four green cells and two timeouts, none of them a failing test.
The suite grew from 1 414 to 1 531 tests and the new ones run subprocesses by design, so the Windows budget no longer fits
(defect 102). The step now has 30 minutes with the measurements in the comment; `faulthandler_timeout` still catches a
real hang. Nowhere is this recorded as a pass: the cells that matter are the ones the next run produces.

`8ba986e` produced four green cells (Ubuntu 3/3, `py3.11` Windows, `docs`, `go-reader`) and three red ones, all on the new
concurrency test: `py3.12` Ubuntu reported "2 recoverers each claimed to have done the work" (defect 103), and the Windows
`py3.12`/`py3.13` cells failed the same test. The claim was exclusive but the *state* was not re-read after the lock, so a
recoverer that lost to a winner who had already finished adopted nothing and ran the tape again; fixed by raising
`_ArchivedElsewhere` once the archive exists and adopting the winner's capsule. Eight consecutive runs of the test here are
green. The same Windows cell also tripped the 120 s hung-test watchdog on the 250-invocation serverless breadth test —
40 s here, over three minutes there (defect 104) — which now declares `@pytest.mark.slow_budget(300)` instead of sharing
one flat bound with a hang detector.

At `4afba25` the Ubuntu cells are green on all three versions — defect 103's fix holds where it was found — but the three
Windows cells still failed the same test, now with "a concurrent recoverer failed without a code": one process died with a
traceback. The annotation could not name the exception, so defect 105 records what *is* measured (a Windows-only, race-only,
uncoded failure) and what is not (the cause). The one Windows-only rename hazard on that path is `os.replace` onto a file a
peer has open, which CPython refuses because it opens without `FILE_SHARE_DELETE`; recovery renames its own state files
while every other recoverer is reading them. `blackbox.platform.replace` now retries the two Windows sharing codes inside a
2 s budget and, if the peer never lets go, refuses with `BBX-E210` rather than a `PermissionError`; `ENOSPC` and friends are
raised untouched. Three tests simulate the violation here. **The Windows cells were not green at that commit and this
paragraph was not a claim that they would be** — the next section is the run that measured it.

Anti-divergence, one layer down: `bc26dce`'s Ubuntu cell then failed the same test with defect 103's symptom at a commit
whose Windows and macOS cells were green, because the fix for 103 reported the winner's capsule but left the adopter's own
`DISCOVERED` in `steps_run` — two processes, one capsule each, both saying they worked (defect 107). `_adopt_archive` now
empties `steps_run`, and a single-process test forces the loser's sequence instead of waiting for six processes to
interleave badly.

## All twenty-two cells green, macOS included (2026-11-16)

`1d42e0c` is the first commit on this branch where every cell finished and passed: 3 OSes × 3 Python versions on both the
push run and the PR run of the commit, plus `docs` and `go-reader` — twenty-two check runs, twenty-two green, no timeout
and no `hung yes` line. It follows `4afba25`, whose three Windows cells were red on defect 105, and it is the run that
measures that fix (all-green at the next commit, with the retry in place, plus three tests that simulate a sharing
violation here). Two things that were open are now closed by measurement rather than by argument:

* **macOS has finally run.** Eleven commits of macOS cells spent the whole time in `queued` — a queue that long is not a
  result, and nothing here ever claimed one. At `1d42e0c` all three macOS cells executed the suite and passed.
* **The Windows arms of two tests ran for the first time.** `test_an_open_view_blocks_the_unlink_only_on_windows` and
  `test_on_windows_no_broad_principal_has_access` are the two skips this Linux box names by name; on `windows-latest` they
  are not skipped (the `icacls` arm skips only if `icacls` cannot be run, and it can), so the green cells are those two
  tests passing. That is what moves the permissions row's Windows half out of "never run".

Then the same test found two more defects *in the fixes*, in the two directions a race can fail: the retry was on the
rename when the refusal was on the **read** (105 → 106, and it took the new failure message to see the path in the
traceback), and the adopting recoverer reported its own `DISCOVERED` as work done (107). `ce32b86` is the commit that
measures both: twenty-two of twenty-two green again, and unlike `1d42e0c` it is a **code** commit rather than a docs one, so
the cells that were red are the cells that ran the repair. The lesson recorded in `docs/defects.md` is not "the race is
gone" — it is that a race which fails differently on every cell needs the *message* fixed first, and that a loser's own
bookkeeping is part of what a test can check without racing at all.

Then `daa7471` (docs only) came back twenty-one of twenty-two: one Windows `py3.11` cell failed the rebind-budget test
with a 686 ms outlier among eight samples whose rebinds were 0–16 ms, and logged a proactor `_start_serving` callback
exception at the same moment (defect 108). Two candidates fit — a stalled runner, or a rare Windows proactor close race —
and the annotation could not say which, so the budget did not move and the failure message now carries the split (tick
lateness, rebind, dispatch, probe cycle) in the characters an annotation keeps. `ce32b86` is still fully green and remains
the pinned commit; this is a known intermittent red on a test that has otherwise been green on every Windows cell since.

What this does **not** measure, and still does not: a Windows **installer** — nothing here compiled or ran one — the
desktop journey on either non-Linux OS (`desktop --selftest` needs a display and CI has none), or a macOS *installer*.
`docs/gates/READINESS.md` now reads 36 rows → **27 evidenced / 7 partial / 0 with nothing behind them / 2 needing
people**, and the two rows that moved are the Go reader (its cell is green) and permissions (its other two OSes are).

**The CLI contract** (`docs/cli.md`, `tests/test_cli_contract.py`, 21). Every command, its machine-readable form and its
exit codes in one table, checked against the binary: the table's set equals the front door's, every `--json` row is
accepted, and the documented code is measured on a failing invocation (`diagnose`'s `1` for "nothing readable" against
everyone else's `2` is documented *because* it differs). `status`, `run` and `serve` gained `--json`; `link` was already
JSON and `ingest` prints one line naming the inbox file, and both are stated rather than flagged.

## A Go reader exists, and CI is its compiler (2026-11-16)

The last of §24a's four missing rows. `tools/readers/go/` is `bbx-read`: a reader written from `docs/format.md` §4/§5/§7/
§8.1 and §13.4's attacker table, stdlib-only, that verifies a capsule's names, limits, duplicates, per-entry sha256, root
and frame structure. Its own header marks every place the document was silent with `// DOC:`.

**It has not been compiled or run here** — this machine has no Go toolchain and no way to fetch one. It is compiled and
executed by a new `go-reader` CI job, which builds it, reproduces the seven CRC-32 conformance vectors, verifies a capsule
written by the Python implementation, and expects three named refusals (tampered entry → exit 4, non-ZIP → 3, traversal
→ 3). The row is therefore **written and reviewed, not measured**, until that cell is seen green;
`tests/test_go_reader.py` (7 tests) checks only what a compiler is not needed for — stdlib-only imports,
registry-declared refusal codes, the selftest's shell syntax, and that CI runs the same script a person would.

## The serverless wrapper exists, with 1 000 invocations measured (2026-11-16)

The third missing row of §24a. `src/blackbox/serverless.py` implements `wrap(handler)` / `@wrap(...)`, the `serverless`
mode (2 MiB segments, a sync per invocation), cold-start recovery of a killed invocation, a marker file that is the only
thing able to survive a sandbox teardown, and `on_capsule` as the user's own durability hook. Design, gaps and the
`unknown_serverless` semantics are in **`docs/serverless.md`**.

Measured, and committed as `bench/results/2026-11-16-serverless-1000.json`:
**1 000 simulated invocations — 751 returned, 101 timeouts, 81 errors, 67 killed outright — produced 1 000 documents
(933 `sealed`, 67 `unknown_serverless`, every one of the 67 recovered by the next cold start), 1 000 capsules that all
verify against their own roots, 0 open runs, 205.7 s (205.73 ms per invocation).** `tests/test_serverless.py` (10 tests)
asserts the same properties over 250 invocations, plus the boundary, the re-raised exception, the defaults, a captured
model call inside the handler, a raising callback and the fail-open path.

**What is not claimed:** a destroyed sandbox is unrecoverable and the wrapper says so rather than inventing a gap event;
the capsule is durable only if `on_capsule` ships it; and 205.73 ms of added latency per invocation is a real cost, not a
detail.

## Triggers exist, with every limit tested against a mocked clock (2026-11-16)

The second missing row of §24a: `stop_delay`, `cooldown` and `max_per_24h` had no module, no test and no configuration
key. `src/blackbox/trigger/` now has the rule schema (`rules.py`, refusals with `BBX-E170` by name), the engine
(`engine.py`), the `[[trigger]]` configuration path, and the keeper writes each decision as a `MARK` frame. The design and
its honest gaps are in **`docs/triggers.md`**; the gaps are repeated here because a status page that only lists the
working half is the thing this project keeps refusing to be:

* **wired**: parsing and refusal, every timed field with a hand-moved clock, windowed counted metrics, edge-triggering,
  per-fingerprint dedupe, the 24 h cap persisted in `triggers.json` across a restart, fail-open (`trigger_error`, one rule
  disabled, keeper and app unaffected), and an end-to-end `blackbox run` whose `exit_code` rule fires and lands a
  `trigger` MARK **inside the sealed capsule**;
* **not wired**: only `exit_code` and `stall_ms` have producers in a real run today — the six seam-side metrics validate
  and never fire until the capture seams feed them; `freeze` schedules the deadline and records `freeze_due` but does not
  seal a second capsule mid-run; `snapshot` writes no stack dump or env snapshot.

Measured by `tests/test_triggers.py`: **27 tests**, all green here, none of them sleeping (24 hours of cap behaviour is an
integer addition).

## The index exists, and I8 is measured (2026-11-16)

`docs/gates/READINESS.md` listed four rows of §24/§24a with no code or artefact in the tree. This one moves the first of
them: **the 500-run index rebuild (I8)**.

`src/blackbox/index/` builds `data/index/main.sqlite` from the sealed capsules — five `STRICT` tables (`run`, `node`,
`event`, `artifact`, `index_meta`), `INSERT ... ON CONFLICT DO NOTHING` so re-ingesting is always safe, and no absolute
path, pid or timestamp in any compared table. `blackbox index [--rebuild] [--dump] [--json]` is the command;
`canonical_dump` is the comparable form (four tables, every row, ordered by primary key, JSON Lines, paths reduced to file
names, `index_meta` excluded because it holds `built_at_ns`).

Measured, and committed as `bench/results/2026-11-16-i8-index-rebuild.json`:
**500 runs → 500 capsules → 6 001 dump rows (500 run, 500 node, 4 500 event, 500 artifact); incremental build 891.5 ms,
rebuild 885.9 ms; the two canonical dumps have the same sha256
`8a65260426d6438b6272e6f7384e83b2994478bc64afcd6c1b3e3151a7359a02`; re-indexing adds 0 rows.** The index file is
measured at the same time (see the JSON) so a reader can see the storage cost of the property rather than only its
correctness. `tests/test_index_rebuild.py` (9 tests) asserts the same equality in the suite, plus the three ways this
could have been false: an unchanged corpus rebuilt twice, a new capsule changing only its own rows, and two tapes of one
run producing one run row and two artifacts.

Suite: **Total: 1537 tests** — 1535 pass here and 2 are skipped on POSIX by name
(`test_an_open_view_blocks_the_unlink_only_on_windows`: POSIX permits unlinking a file another fd still holds, so the
open-view case exists only on Windows; and `test_on_windows_no_broad_principal_has_access`: mode bits are not access on
Windows and `icacls` does not exist here). Measured on this machine: **1 537 collected, 0 failed,
0 errors, 2 skipped, 431.3 s** (`/tmp/full22.xml`) — the six added this round are the Windows rename- and read-retry
tests (defects 105–106) and the adoption test that holds defect 107 down. The 117 added since the 1414-line are the desktop flows and the two things that
could previously only be checked by a person with a screen or a Windows machine:

* `tests/test_ui_flows.py` (14) — the window's four flows over real HTTP: check, diagnose, review a proposal, apply
  it, run the tests, and save the brief or the report as a file. An unticked box, a missing token and a **wrong** token each refuse with the repository byte-identical
  afterwards; the right token applies once and a second attempt refuses because the tree moved; a cross-origin `POST` and
  a wrong form token are refused before any flow runs.
* `tests/test_ui_safety.py` (13) — no secret from the environment on any of seven pages; no page referencing anything
  outside the loopback server; a redacted value staying redacted through a real recorded run; four hostile capsules
  refused with `BBX-` codes and no traceback; a capsule name from the URL never reaching outside the data directory; the
  data directory byte-identical after every page load; a non-form `POST` refused; and a run whose process was killed still
  listing, opening, verifying and diagnosing.
* `tests/test_windows_definition.py` (11) — the Windows installer definition checked as far as Linux allows: every
  `Source:` exists, the icon is a real ICO with the product's colours, the installer is per-user, `HKCU`-only, deletes
  only its own directory, claims no signature, and the staging script and the definition agree on the launcher. **This is
  a definition that would compile, not an installer that has been run.**
* `tests/test_desktop_journey.py` (3) — the harness a person with a Linux desktop runs refuses on a headless machine, and
  its `--automated-only` mode records a real incident, lists it, checks it, diagnoses it, and writes
  `"verdict": "automated-only"` with every window answer `"not-attempted"`.
* `tests/test_desktop.py` (+1), `tests/test_ui_library.py` (+1), `tests/test_cap_read_manifest.py` (+1) — the run page's
  action bar, the repair link on the library, and the manifest reader's last refusal case.

Measured through `--junitxml` because `pytest -q` prints no summary here: **1414 tests, 0 failures, 0 errors, 1 skip,
320.7 s** (`/tmp/full15.xml`) — the one skip is named above, and this paragraph's own number is asserted by a test that
failed when it said 1372 and collection found 1413, exactly as the note below says it would. The number is for the
dependency set CI installs, `.[dev,capture,capture-sdks]`: sixteen tests live behind `anthropic`, so a venv with only
`httpx` and `openai` collects 1146 (D-062).: sixteen tests live behind `anthropic`, so a venv with only `httpx` and `openai`
collects 1146 (D-062). ruff 0.17.5 and mypy 2.4.1 (`strict = true`,
nothing disabled): 0 findings.

**The demo path, run end to end on 2026-11-17 (D-060, D-061).** `pip install -e .` now produces a `blackbox` command
(it declared no entry point before), `blackbox ls` and `verify --data` look where `blackbox run` writes instead of in a
`./.blackbox` that nothing ever created, and a capsule is **9.8 KiB instead of 16 MiB** because recovery no longer puts the
segment's preallocated hole into the archive. With that in place a real `openai` 3.51.0 client was run under the recorder
(`blackbox run -- python3 app.py`, the client shipped unmodified): the call failed, the process exited 3, two capsules
were recovered (23 and 17 events, `recovered/PROCESS_DEATH`), `ls` listed both, `blackbox verify` exited 0 with
`integrity`, `chain_root`, `derived_parts`, `capture_seam` and `links_summary` green and `capture_vantage` printed as
declared-not-checkable, `diagnose` wrote a report whose one finding is `observed` ("this run is named by 2 different
identifiers"), `view` wrote a 56 606 B page, and `repair preview` -> `apply --approve <token>` -> `verify --test` applied
a one-file proposal and printed `PASS`. What that run does **not** show is unchanged and still listed below: no node-level
edge for an uninstrumented or Node client (Python's `api.run` is the only node API), no importer other than Phoenix, **every macOS cell unmeasured on that commit** (GitHub never acquired a runner
for any of the three: "The job was not acquired by Runner of type hosted even after multiple attempts", 0 steps), and no
universal 100 ms rebind claim. **macOS is measured now**: run `44220446574` at `ad3a783` is **ten of ten green**, all
three macOS cells executing 11 steps each — the first macOS result on this branch — and the Linux and Windows cells
kept their green. The rebind claim stays local: the 100 ms gate is asserted by a bench on this machine, and no CI runner
contributes a rebind number.


**What `repair` does not guarantee, in one place.** The gate is documented at length in D-049/D-050 and in the P4-18
row below; these are the three limits a reader should not have to go looking for. (1) **On Windows the symlink guarantee is check-then-write, not atomic.** `os.O_NOFOLLOW` does not exist there, so a swap that lands between the `islink` check and the open is not detected; on POSIX the descriptor flag closes that window, and closing it on Windows means opening by handle with `FILE_FLAG_OPEN_REPARSE_POINT`, which was not done. What *is* guaranteed there is that no read or write returns bytes through a link: `proposal.read_nofollow` checks before opening and `sha256_file` uses it, so the hash a reviewer approved and the bytes on disk are read by one rule (defect 50). (1b) **On Windows the backup directory is not 0700.** `os.chmod` there only toggles the read-only attribute and `os.stat` synthesises `0o777`, so a private directory is not expressible: `<repo>/.blackbox-repair` is as private as the repository's ACLs, `Applied.private_by_mode` is `False`, the manifest records that weaker fact and `repair apply` prints it (defect 51). On a filesystem that *has* POSIX modes and ignores `chmod`, the applier refuses rather than proceeding. (2) **A process killed mid-apply
cannot roll back.** It had no chance to — what survives is `apply-manifest.json`, written before the first mutation and
left in state `planned`, so the tree is auditable rather than silently unknown. (3) **`repair verify --test CMD`
executes CMD** through the platform shell, in `--repo`, with your privileges; it is announced before it runs and
nothing from a proposal is ever executed.

## Fulfilled (test exists, ran, passed / number measured)

| Gate or budget row | Plan ref | Evidence (test path, results JSON, or CI run URL) |
|---|---|---|
| **P3B-04, the header path and the link resolver (the first half)** | P3B-04, §11.10, D-036 | `src/blackbox/cap_sdk/headers.py`, `src/blackbox/rec/links.py`, `tests/test_headers.py` (7), `tests/test_links.py` (10): `x-blackbox-run` is injected by seam A's request hook **only** when `BLACKBOX_PROXY=1` — end-to-end through a mock provider that pretends to be the real one, the same SDK call carries nothing of ours unproxied and `run-seam` when proxied — the proxy reads the value, records it as `linked_run` on the `MODEL_REQUEST` frame, and strips every `x-blackbox-*` header before the upstream; the resolver emits `exact` (same run id, evidence names the header), `declared` (the user said so, `asserted_by: user`) and `inferred` (±2 s **and** fingerprint, confidence capped below 1.0, both numbers in the evidence), never downgrades a pair it has already resolved, never links a writer to itself, and **never merges runs** — asserted, not just documented. The second half is in (D-038): `KIND_LINK` is `19` in the plan's graph range, appended without moving a published id; the resolver runs at seal time and the capsule carries `links.json` (with an empty file when nothing linked, because "looked and found none" is not "never looked") while the tape's segment bytes are byte-identical afterwards and hold no `LINK` frame; a `LINK` payload read back off a tape is checked — unknown class, missing field, self-link, confidence outside `[0, 1]` and an `inferred` link claiming `1.0` are each refused as `BBX-E155`; a tape carrying two runs' traffic names neither, so it can only be linked by inference (with `why_not_exact` in the evidence) or by declaration; and `blackbox link <a> <b>` records one `declared` pair in **both** data directories, idempotently and order-free, refusing a directory whose tape names no run. `tests/test_link_records.py` (16) |
| **The three §14.4a disconnect classes, and the client matrix** | P3B-08, §14.4a, D-035, defect 34 | `src/blackbox/proxy/disconnects.py`, `tests/proxy/` (15): `INTERRUPTED_CLIENT` and `INTERRUPTED_UPSTREAM` are decided by the side that closed first and written on a real tape with both byte counts, read back by `disconnects.error_of` the way a report will read them; `INTERRUPTED_PROXY` is derived from an open call plus a dead writer lease, because nothing can write it. Tested from outside: a client closes its socket mid-stream (`client_gone` 1, `upstream_gone` 0, class on the tape, listener still serving), an origin dies mid-body (the client's `IncompleteRead`, both counts equal at 512 — the client was not the one that left), and S16 (`SIGKILL` a real proxy mid-stream: the lease says `pid gone`, recovery says `INTERRUPTED_MID_STREAM`, and the tape has a request, chunks, and **no terminal frame**). The client matrix runs **curl** (libcurl, a separate process), **Node 22** (`http.get`) and the real **`openai` SDK** (streaming, `[DONE]`-terminated) through the same listener: all three receive bytes sha256-identical to the origin's, the prefix is stripped and the path kept, and the proxy's own counter says one request. Clients missing from a runner are skipped by name |
| **The P0 that a reviewer found in D-032a: a retried POST is a duplicated effect** | P3B-08, D-032b, D-032c, defect 33 | `src/blackbox/proxy/server.py`, `tests/test_proxy_server.py` (18): a pooled connection that dies **after** the request was written is a different thing from one that is found dead **before**, and the pool now treats them differently. Before writing, `_connect` probes the connection (`at_eof`, `is_closing`, `exception`) and discards a dead one — that costs nothing and means no request was ever sent. After writing, the outcome is uncertain by construction, so **the proxy does not retry at all by default**: the client gets `502` with `x-blackbox-error: upstream-closed-uncertain`, a body saying the request may have been applied, and `stats.uncertain` counts it. `--proxy-retry-transmitted` (or `retry_transmitted=True`) opts into exactly one retry, and only for `GET`/`HEAD`/`OPTIONS` — an assertion the caller makes about *their* upstreams, because HTTP only *intends* those methods to be safe and a GET with a side effect is a real thing. The regression tests: the upstream records a POST and closes without answering, and **exactly one** POST arrived (`test_a_post_that_may_have_been_transmitted_is_never_sent_twice`); with the flag **off**, a GET in the same position comes back `502`; with it **on**, the GET arrives twice and a POST in the same position still arrives once; a mutation check (POST made retryable) shows the upstream receiving two. A dead pooled connection discarded *before* the write needs no flag and is covered separately
| **P3B-08, the four §15.1 proxy rows** and the upstream pool that made one of them reachable | P3B-08, §15.1, D-032a | `bench/bench_proxy.py` → `bench/results/2026-11-15-proxy.json`, `src/blackbox/proxy/server.py`: added latency, first-byte passthrough, 200 concurrent streams and RSS at 50 req/s are measured on a real listener with a real origin, with the origin, the proxy and the measuring client each in **their own process** — a shared interpreter turns these four rows into a measurement of the GIL, which the previous revision of this row did without noticing (the four rows above). The **pool** is the interesting part: `_UpstreamPool` keeps idle per-route connections for reuse, releases only a connection whose response was read to its end, and **retries once on a fresh connection** when a pooled one turns out to be dead — counted in `pool.retries`, never shown to the client (`test_a_pooled_connection_that_died_while_idle_is_retried_invisibly`, `test_a_second_request_reuses_the_upstream_connection`) |
| **P3B-03, second half** `blackbox run` starts the proxy, records its vantage, and hands the child the plan | P3B-03, D-033, D-034, defects 45–47, 49 | `src/blackbox/rec/launcher.py`, `tests/test_launcher_proxy.py` (9): a **real child** prints its own environment and makes one HTTP request through the base URL it was given — so what is asserted is what the child saw, and that the listener was up before it was. Unset variables arrive pointing at `http://127.0.0.1:<port>/<name>` with `BLACKBOX_PROXY=1` and `BLACKBOX_PROXY_URL`; a variable the user had already set arrives **untouched**, is recorded as `adopted`, and its destination is added to the route table rather than refused; the request then arrives at the upstream with the prefix stripped and the proxy's own count says `requests: 1`; without `proxy=True` no listener starts and no variable is written; the listener is gone when the child is (a connect is refused); and the summary line names the URL and how many variables were exported. `--no-proxy`, `--upstream NAME=URL[@/prefix]`, `--insecure-upstream` and `--proxy-port` are the CLI's version of the same decisions. **The acceptance line was run for real on 2026-11-16** (`bench/accept_p3b03.py` → `bench/results/2026-11-16-p3b03-acceptance.json`) with a real loopback origin and three real clients — a `urllib` script, Node 22.22.3, and `openai` 3.51.0 — each launched by the CLI: all three exit 0, every capsule passes its own integrity root, the proxy's vantage is on a tape, and **every capsule of a run names one run id** since defect 47 was closed by separating the two identities (a per-tape work-directory key, and the run id the tape's own `RUN_START` declares; capsule ids derive from both, so two tapes of one run are two capsules and neither can replace the other). **The seam the auto-import was missing is installed now** (defect 49, closed): `api.install()` asks `preflight.choose()` for one once the tape exists, so an unmodified SDK script records its own `MODEL_REQUEST`/`MODEL_CHUNK`/`MODEL_RESPONSE` and its `api.run` nodes on its own tape, with the node slot resolved **per event** rather than at install time; a product that already installed a seam gets that seam back with attempt status `already-installed` instead of a second patch on the same client class (two hooks made `openai` raise *Attempted to call a sync close on an async stream* before a byte — reproduced, then fixed by adoption; D-051). **Re-measured on 2026-11-16** with the seam in place (`bench/results/2026-11-16-p3b03-acceptance.json`, four real clients under the CLI: `urllib`, Node 22.22.3, `openai` 3.51.0 as shipped, and `openai` inside `api.run`): every client exits 0, every capsule passes its own root, every two-capsule run names one run id and carries `exact` links, the app's node appears in the graph of the instrumented case (`graph_nodes: 1` matching `counts.nodes: 1`), and **every clean run says `recovered`** — that last one was defect 53, which made every cleanly sealed run read `SEAL_INTERRUPTED / SEAL_NEVER_FINISHED` because the scanner never read the active segment's seal. Defect 54 (the graph listing only *unclosed* nodes) is closed the same way. **The graph carries the link it can justify — now per call — and the ticket is still not ticked** (defect 55, D-052, D-053): `graph.json` gets `edges` derived at seal time from the same bytes the capsule receives as `links.json`. D-052 wrote `run`/`tape` edges and refused `node`; D-053 supplies what that refusal named — a per-call identifier: the seam mints `x-blackbox-call` per outbound request, records it as `call_id` on its own `MODEL_REQUEST`, and the proxy reads the header, writes the same id on its own frame, and strips it before the upstream (asserted at a mock origin that saw neither `x-blackbox-*` header). Those ids become `level: "call"` edges with `evidence: {rule: "same call id", carried_by: "x-blackbox-call"}`, naming each side's node slot where that tape had one and `null` where it did not: `node_join` is `exact` when both sides name a node, `partial` when exactly one does, `ambiguous` (with `candidates`) when an id appeared twice on a side, and no node is ever invented for a proxied call without a slot. `graph.json` is also a **declared capsule part** now — `manifest.derived_parts[]` with its sha256, recomputed from the capsule's own raw frames by `blackbox capsule verify`, which exits 0 on a match, 1 with `BBX-E412` and the drifted fields on a mismatch, 2 when the capsule cannot be read, and reports an old capsule without parts as `absent` rather than rewriting it. Re-measured with the four real clients (`bench/results/2026-11-16-p3b03-acceptance.json`): `run_level_edges_recorded: 10`, call-level edges in both the `sdk` and `sdk-nodes` cases, `edges_naming_a_node: 2`, `node_level_edges_recorded: 0`. **Re-measured again on 2026-11-18, after the proxy learned to record its own span** (`bench/results/2026-11-18-b4-node-edges.json`): the proxy opens a `proxy_call` span per forwarded request from its own slot band (`constants.NODE_SLOT_PROXY_BASE..MAX`; hooks stop at `NODE_SLOT_HOOK_MAX`, so two writers on one tape cannot collide), tags that call's frames with the slot, and the resolver joins it to the app's node by `x-blackbox-call` alone. The instrumented client (`openai` 3.51.0 inside `api.run`) now yields **`node_level_edges_recorded: 2`** (app node `1` ↔ proxy span `16384`, call id `5eb5b57ced07034f`, listed in each tape's graph), `node_join: "exact"`, with both capsules passing their own integrity root; the uninstrumented `urllib` and Node clients yield `0` node-level edges and `node_join: "unknown"`, and `openai` as shipped (a seam but no `api.run`) yields `0` and `"partial"` — those three are the negative control, and none of them is joined by a fingerprint or a time window. Measuring it found defects 75 (sink payloads are JSON, `graph.json`'s reader decoded CBOR only, so a span arrived unnamed) and 76 (`SinkRecorder._forward`'s parameter collided with a span's `name` field, and fail-open hid it). **What still keeps the ticket unticked**: a Node client cannot produce a node-level edge at all (`api.run` is Python-only, so its row is `unknown`, not passing); ambiguity is refused in tests rather than produced by a live client; macOS is unmeasured for these rows; and shape (b) of `docs/node-edges.md` (a hook-to-seam join) plus a launcher-owned process span remain proposals. The owner's decision to keep P3B-03 and defect 55 open stands. The remainder is a node identity both vantages can write, and `docs/node-edges.md` writes down what would count: two recorded spans (each with `NODE_OPEN`/`NODE_CLOSE` on its own tape), an identity that survives crossing tapes, a join by a per-call id, and ambiguity recorded as a refusal — with the proxy opening a span of its own for each forwarded call as the shape worth costing first, and a launcher-owned process span explicitly labelled a launcher observation rather than an app node. **The owner's decision (2026-11-16) is to keep P3B-03 and defect 55 open until that exists**, to document the limits of the current call-to-node association (done, above and in the proposal), and to test concurrency, ambiguity and missing instrumentation in the meantime — the third case is now a real-run test, `tests/test_launcher_proxy.py::test_an_uninstrumented_child_leaves_no_node_and_no_call_edge` (a plain `urllib` child: `call_id` and `linked_run` both `null`, no node, no call edge, and no link needed because the proxy's frames are on the run's own tape). Tests: `tests/test_call_links.py` (13), `tests/test_capsule_graph.py` (10), `tests/test_launcher_proxy.py` (9, two of them real proxied children with two concurrent calls), the edge tests in `tests/test_recovery.py`, and `tests/test_capture_failopen.py` for the interface half (a receiver written before the `call_id` hint keeps recording instead of failing quietly).
| **P3B-03, first half** the proxy as a supervised thread, and the variable plan | P3B-03, D-033 | `src/blackbox/proxy/supervisor.py`, `tests/test_proxy_supervisor.py` (9): `plan_exports()` is the only place the child's environment and the proxy's routes are decided, in one pass, so they cannot drift — an unset `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL`/`OLLAMA_HOST`/`GOOGLE_GEMINI_BASE_URL` is pointed at the proxy, a **set** one is adopted as an upstream and never overwritten (a recorder that moves traffic is not a recorder), a value that is not a URL is adopted but not routed rather than guessed at, and when every variable was already set nothing claims to be the default. `ProxyThread` runs the listener on its own loop in its own thread, and its watchdog rebinds a dead listener **on the port the child was told**, and the row is measured the way §11.8 writes it: the **outage** from the listener stopping to a listener serving on that port, which contains the tick as well as the rebind. `bench/bench_rebind.py` reports every watchdog-triggered sample in **four** columns — idle/loaded crossed with the two detectors, `notice` (the listener's own `lost_cb`) and `tick` (nothing announced it) — with the outage, the rebind, the detection time and the supervisor's own `noticed` time each carrying its own sample count (`bench/results/2026-11-16-rebind-detectors.json`: 160 samples, 0 over the 100 ms budget, notice columns p50 0.72–0.92 ms / max 3.30 ms and tick columns p50 11.9–12.7 ms / max 21.1 ms — D-055), and originally in two columns — idle **worst 21.86 ms** / p50 11.58–12.39 / p95 20.53–20.77 and loaded (one busy process per CPU) worst 21.47 / p50 11.52–11.77, rebind alone p50 0.80–0.82 / max 2.90 ms, **0 of 240 samples over 100 ms and 0 port moves across two committed runs** · `bench/results/2026-11-16-rebind-a.json`, `-b.json` — and `test_every_watchdog_rebind_lands_inside_the_budget_not_the_median_of_them` asserts *every* sample of 8 watchdog rebinds at the tick that ships, because the median-of-five this row used to quote was four manual rebinds and did not contain the tick at all (defect 40, D-043, ADR-006). The ticket's **second half** is now built too: a Python child gets a temporary `sitecustomize.py` on its `PYTHONPATH` (`--no-sdk` omits it), the directory is deleted when the child exits — including on `SIGKILL` — and an existing `sitecustomize` is chained rather than shadowed; `--force-proxy` names every variable it displaces, `--no-hooks` removes `BLACKBOX_INBOX` instead of leaving a directory nobody folds, and `--no-proxy` starts no listener. One run is now one **set of linked tapes**: the child's tape carries the launcher's run id, so `blackbox run -- python app.py` leaves three tapes recovered into three capsules joined by `exact` links (before this, the child's frames were in no capsule and `links` was empty), and `counts.nodes` counts the nodes the run had rather than the ones still open (defect 41). Tests: `tests/test_launcher_attach.py` (23, and the two that failed on Windows are now platform-correct: defect 43) |
| **P3B-01** the proxy core: loopback in, the provider out, a tee onto the tape | §13, P3B-01 | `src/blackbox/proxy/server.py`, `tests/test_proxy_server.py` (12): an origin and the proxy share one background loop and the test body speaks HTTP to it **synchronously**, so nothing passes because two coroutines agreed; a POST arrives with its body and the upstream sees the routed path and its own `Host`; an SSE response is **sha256-identical** to what the origin pushed and is still relayed as `Transfer-Encoding: chunked`; hop-by-hop headers are not forwarded in either direction and an `x-blackbox-*` header from upstream never reaches the client; an unknown route is `502` + `x-blackbox-error: upstream-not-allowed` with the caller's input **not** echoed back; a dead route is `504 upstream-unreachable` and the listener survives it; `Expect: 100-continue` is answered **before** the body is read; two requests share one keep-alive connection; a client that hangs up mid-stream leaves the proxy serving; behind a real `TapeWriter` + `TapeSink` (`coalesce_bytes=1`) the scan finds one `MODEL_REQUEST`, one `MODEL_RESPONSE` and chunks whose concatenation hashes to the body; and a recorder that raises on every hook changes nothing the client receives (stat `record_errors` rises) |
| **P3B-02** routing: the table is the allowlist | P3B-02 | `src/blackbox/proxy/routes.py`, `tests/test_proxy_routes.py` (10) + `tests/test_no_egress.py::test_the_proxy_can_only_reach_its_configured_upstreams`: the single default route takes what no prefix claims; the **longest** prefix wins, is stripped, and the base path is kept (`/local/v1/models` → `/v1/v1/models` against `http://127.0.0.1:8000/v1`); the bare prefix is the base path; the `x-blackbox-upstream` header names a route and an unknown name is `BBX-E140`, never a fallback; no default + no match is an error; a non-loopback bind is `BBX-E143` unless `bind_unsafe` is said out loud; destinations are a closed set (`{("api.openai.com", 443), ("127.0.0.1", 8000)}` in the test); a route can be made to skip verification and `verify_all()` moves only towards verification; `proxy/` is the second declared network exception (`asyncio`, `ssl`, `socket`, and `urllib.parse` **only** — `urllib.request` stays banned), and the single `open_connection` call site is handed `route.destination` by an AST assertion |
| Canonical CBOR; decoder hardening | §8.4, P1-03 | `tests/test_codec.py`, `bench/fuzz_cbor.py` → `bench/results/2026-11-14-fuzz-cbor.json`: 100 000 buffers, 0 non-`CBORError` |
| One checksum (CRC-32/ISO-HDLC), conformance vectors | §8.3, DV-005 | `tests/test_conformance.py`, `tests/conformance/vectors/crc32.json` check value `0xCBF43926` |
| Frame layout, CRC scope, head two-slot publish + rebuild | §8.3, §8.5 | `tests/test_frame.py`, `tests/test_head_creation.py` |
| Segments, roll, adoption (S15) | §8.7, P1-12 | `tests/test_tape.py`, `tests/test_adoption.py`: adoption idempotent in the writer and in `rec/discover.py` |
| Crash stages S1–S5, S14, S15 green on the new store | §16.2 | `tests/test_crash_points.py`, `tests/test_scan_states.py` (seeds 388/209/396) |
| S17 ENOSPC, first segment included | §16.2, P1-20 | `tests/test_enospc.py`, `tests/test_prealloc.py`: exit 0, identical stdout, `DEGRADED`; no first segment ⇒ sink, never an exception |
| Randomized parent-`SIGKILL`, P2-09 | §14, C19 | `tests/kill/kill_matrix.py --trials 500` in a fresh clone of `af908bc`: **500/500 effective trials, 0 violations, 0 `not_started`**, 10 cells × 50 (includes a 20 ev/s window cell and a `kernel` cell) |
| Append-path syscalls, C11 | §8.6 | `tests/test_no_syscall.py`: 100 000 appends ⇒ exactly two `pwrite`s per batch (data + 64 B head slot) and nothing else |
| Group-commit window ≤ 5 ms; keeper | §8.6, P1-08 | `tests/test_keeper.py`: window measured at 1 ev/s, 1 000 ev/s and unbounded |
| Launcher `blackbox run -- cmd`, stall dump (P2-11, P2-13) | §10.3, F13 | `tests/test_launcher.py`; fresh clone: `run r_40ead954098fb5df · exit 137 · signal 9 · recovered c_40b2ebb29e649534.bbx (SEAL_INTERRUPTED, root 14f1737e50b6)`; a real deadlock yields `stall.json` + both thread stacks in `stall_tb.txt` |
| Capsule v0 root verifies; P2-12 idempotence | §12, P2-10 | `tests/test_capsule.py`, `tests/test_recovery.py`: 10 recoveries ⇒ 1 capsule hash, 1 `.bbx`; second recoverer `BBX-E210`; dead holder's lock taken over |
| Recovery state machine (P2-06) and node classification (P2-07) | §14.4 | `tests/test_recovery.py`: 8 steps × 3 kill modes all resume to the identical capsule root; the five interrupted-node strings exactly |
| Retention + declared holes (P1-09b) | §9.5, D-016/17/18 | `tests/test_retention.py`, `tests/property/test_retention_random.py`: 8 seeds × 60 steps per run, 320 recorded runs in `bench/results/p1-09b-retention.json` (`totals`: 19 200 steps, 5 400 kills, 0 violations of I1/I9); the file is written only with `BLACKBOX_RECORD_RESULTS=1` |
| Seal footer, verifier, compaction into `arch/` (P1-13) | §8.7, F4 | `tests/test_tape.py`, `tests/test_compact.py`: re-seal byte-identical; archived segment reads back frame-identical; second compaction a no-op |
| Blob store (P1-14), compression on the keeper (P1-15) | §9.9, §7.1 | `tests/test_blobs.py`: hash → tmp → write → replace, every read re-verifies CRC and hash, `BBX-E162` on a renamed blob, `ChunkCoalescer` 2 000 chunks → 3 blobs |
| Blob durability per mode, **D-019**: the calling thread never syncs a blob | §7.1, C32 | `tests/test_blob_durability.py`, `tests/test_no_syscall.py`: `process` records no sync at all; `kernel` syncs blob + directory in the keeper tick **before** the tape sync; `paranoid` before the frame is appended; 64 KiB event = open/write/close/replace + 2 `pwrite`s + **0** syncs |
| Platform shims, P2-03/P2-04 | §7 | `tests/test_platform_shims.py`, `tests/test_platform_dispatch.py`: `is_current` computed (no `tested_here` constant); no shim imported by name outside `platform/`; Windows `OpenProcess`+`GetProcessTimes`, macOS `sysctl` + `F_FULLFSYNC` exercised on the real cells — run `43678447134` |
| no mapping anywhere (`import mmap` gone from `src/`) | ADR-005, DV-010 | `tests/test_no_mapping.py`: no module in `src/` imports `mmap` and no `mmap.*` attribute is referenced; the head is published with one `pwrite` per batch |
| Budget: proxy added latency, non-stream, loopback | §15.1 | **p50 0.165 · p99 0.471 ms** against p50 ≤ 1 ms and p99 ≤ 5 ms — `bench/results/2026-11-15-proxy.json` `gate.added_latency_ms` (n 500, paired: each proxied request minus the same request sent straight at the origin; four consecutive runs gave p50 0.162–0.182, p99 0.31–0.471) |
| Budget: proxy first-byte passthrough (SSE) | §15.1 | **p50 0.184 ms** against ≤ 0.5 ms — `bench/results/2026-11-15-proxy.json` `gate.first_byte_ms` (n 100, paired); the five block medians are 0.207, 0.146, 0.187, 0.162, 0.189 ms, so **no block sits above the budget** and the row has 2.7× margin. The previous revision of this row read 0.441 ms with one block at 0.517 and called itself marginal; that was not the proxy, it was the harness: origin, proxy and client shared an interpreter, so every hop paid a GIL hand-off. Origin and proxy now each run in their own process — the topology the budget is about — and the same code, same budgets, reads 0.184 ms. Pooling (D-032a) is still what makes it reachable at all; coalescing head with the first chunk made it *worse* (0.548 ms) and was reverted, because the client cannot parse anything until the head arrives |
| Budget: proxy concurrent streams | §15.1 | **200 concurrent SSE streams, 0 errors, every body sha256-identical to the origin's** — `bench/results/2026-11-15-proxy.json` `concurrent_streams` (wall 157.687 ms, 0 errors) |
| Budget: proxy RSS at 50 req/s | §15.1 | **0.0 MiB growth** against ≤ 60 MB — `bench/results/2026-11-15-proxy.json` `rss` (394 requests over 10.0 s at 50.0 Hz, baseline 23952 KiB peak 23952 KiB, read from `/proc/<proxy pid>/status` — the proxy's own `VmRSS`, not the measuring interpreter's) |
| Budget: 1 KiB `content=hash` p50/p99, throughput | §15.1 | p50 11.057 · p99 51.662 µs · 73 338 ev/s — `bench/results/2026-11-14-writer-throughput.json` |
| Budget: recovery ≤ 120 ms / 10 k frames | §15.1 | 106.1 ms/10 k, n 30 — `bench/results/2026-11-15-recovery.json` |
| Budget: disk amplification ≤ 1.25× (2× hard fail) | §15.1 | 16 KiB group commit 1.231× (3 150 352 / 2 560 000) — `bench/results/2026-11-15-recovery.json` |
| Budget: Disabled overhead per decorated call | §15.1 | **p50 0.12 µs · p99 0.16 µs** against a ≤ 1 / ≤ 3 µs target, hard fail 5 µs — `bench/results/2026-11-15-capture-overhead.json` `results.ext4.disabled.overhead_us` (n=5 000, measured against the same function undecorated in the same process) |
| Budget: `content=off` event enqueue | §15.1 | **p50 1.42 µs · p99 2.09 µs** against ≤ 8 / ≤ 40 µs, hard fail 100 µs — `bench/results/2026-11-15-capture-overhead.json` `results.ext4.enqueue_off.us` (n=5 000, 1 KiB reads, in-memory emitter so the number is the recorder's own work) |
| Budget: `content=hash`, 1 KiB payload (redact + digest + frame) | §15.1 | **p50 10.95 µs · p99 31.79 µs** against ≤ 25 / ≤ 250 µs, hard fail 1 ms — `bench/results/2026-11-15-capture-overhead.json` `results.ext4.enqueue_hash.us`. The **worst case is published too**: a 1 KiB body containing the word `token` costs p50 138.7 µs (p99 242.3, max 1 092) because that is the literal the `assignment` pattern hunts, and it is still under the hard fail. `enqueue_hash_keyword` |
| Budget: provider call latency Δ p95 (700 ms streamed call) | §15.1 | **−1.02 %** on 20 recorded against 20 bare calls of a 14 × 50 ms stream (p95 691.8 ms recorded vs 698.9 ms bare) against a ≤ 3 % target and 10 % hard fail — `bench/results/2026-11-15-capture-overhead.json` `results.ext4.provider_latency`. A negative delta is noise, not a speed-up: n=20 per side and the p95 of a 700 ms call is dominated by the mock provider's 14 sleeps |
| Budget: startup `install()` | §15.1 | **p50 2.90 ms · p99 3.89 ms** against ≤ 25 ms, hard fail 150 ms — `bench/results/2026-11-15-capture-overhead.json` `results.ext4.startup.ms` (n=20, from nothing: open the tape, write `RUN_START`, start the keeper) |
| **The content mode is honoured, and it is what the configuration says** | D-030, Appendix A, defect 30, §15.1 | `src/blackbox/cap_sdk/sink.py`, `tests/test_content_modes.py` (9): the default is the plan's `hash` and not whatever was convenient; `full` keeps the body byte-for-byte, `hash` keeps `[bbx-hash sha256=… bytes=N]` and the digest of each group recomputes from the body, `off` keeps the count alone and no byte of the text; the request and summary frames declare the mode; a secret in the body does not reach a `hash` tape; and `recovery` reports the same partial byte total from the frames in all three modes, which is what lets a `kill -9` still answer "how much arrived" |
| Budget: RSS ≤ 24 MiB above baseline | §15.1 | 1.56 MiB at 28.13 % of a segment — `bench/results/2026-11-15-recovery.json` |
| Budget: 64 KiB `content=full` incl. blob write (C32) | §15.1 | p50 226.864 · p99 461.281 µs · 4 356.4 ev/s — `bench/results/2026-11-15-payload-blob.json` |
| Group-commit window, per OS (C40) | §8.6, C3 | `tests/test_keeper.py::test_low_rate_event_is_published_within_the_window` starts its own window and `test_a_window_older_than_the_bound_publishes_inline` states the expired-window rule; the ≤ 5 ms row is measured on ext4/Linux only and the Windows bound (one 15.6 ms timer tick for a keeper-only drain, inline publish for arriving events) is stated in `docs/compatibility.md` and marked `not measured` |
| **P3-02/P3-03/P3-04** decorators, node stack, context managers | §10.2, P3-02–04 | `src/blackbox/api.py`, `src/blackbox/rec/context.py`, `tests/test_api_decorators.py` (6), `tests/test_context_nodes.py` (7): one `NODE_OPEN`/`NODE_CLOSE` pair per call for all five decorator kinds; signature, `__name__` and `__doc__` survive; an exception closes the node with its type and is re-raised; a **disabled** decorated call costs under 1 µs of median overhead; async keeps its node across awaits; 50 nested steps stay balanced with each level's parent the level above; `mark` lands in the node it was called inside; `handled_error` records a handled exception where it was handled; a plain thread gets a `detached:` root (marked `detached: True`) instead of an orphan; a forked child writes to its own tape |
| **P3-01** SDK lifecycle: `install`/`uninstall`/`shutdown` | §10.2, P3-01 | `src/blackbox/api.py`, `tests/test_api_lifecycle.py` (6): double install ⇒ one writer UUID and one `RUN_START`; 16 threads install at once ⇒ one writer and `refs` 16; `uninstall()` during an in-flight append leaves the tape open and the frame lands; `BLACKBOX_DISABLED=1` creates no tape and `emit()` returns 0; an unwritable data dir leaves the app running with the reason recorded; `shutdown()` is idempotent and install works after it |
| **P3-13MP** loopback mock provider | §18.5 (G-Capture needs it), D-021 | `src/blackbox/cap_sdk/mock_provider.py`, `tests/test_mock_provider.py` (9): OpenAI and Anthropic SSE dialects over a real socket, driven with stdlib `http.client` so the test cannot be coupled to the library under test; knobs for chunk count, inter-chunk delay, first-byte delay, truncation, mid-stream error, non-200 status and payload size; the request log proves what arrived; the source is asserted to contain no wildcard bind and to bind an ephemeral loopback port |
| **P3-05a** seam A, httpx response hooks | §18.5, D-022 | `src/blackbox/cap_sdk/seam_httpx_hooks.py` + `tee.py`, `tests/test_seam_hooks.py` (7): the hook wraps the still-unread stream, so every chunk is recorded — the recorder's count equals the number of chunks the application observed in the same run, and the body is sha256-identical with and without the seam; a request hook stamps the clock before the request goes out so `first_byte_ms`/`total_ms` are end-to-end; a response whose body is already gone is recorded `partial=True` with no byte count; a recorder that raises never reaches the application (fail-open); `uninstall` restores both client classes and is idempotent; the caller's own event hooks still run |
| **P3-05a/05b async** — the path that was implemented and unexercised | §18.5, defect 20 | `tests/test_seam_async.py` (4), `src/blackbox/cap_sdk/tee.py`: `wrap_stream` picks the tee from the stream (`__aiter__` ⇒ `AsyncTeeStream`), so an async response is recorded chunk-by-chunk instead of being handed a sync wrapper that fails the moment it is iterated; both seams are tested through `AsyncClient` for chunk count, byte total and sha256 equality against an unseamed client |
| **P3-05b** seam B, transport byte tee | §18.5 | `src/blackbox/cap_sdk/seam_httpx_transport.py`, `tests/test_seam_transport.py` (9): the tee is compared against an unmodified client by sha256, the recorded chunk count against what the application actually read, a truncated stream is recorded short instead of completed, an error mid-stream is recorded and re-raised, an abandoned stream is closed with the bytes that arrived, and both `client=` (instance) and class-patch installs restore exactly what they patched |
| **P3-05d** ordered preflight A→B, `capture_seam` | §18.5 | `src/blackbox/cap_sdk/preflight.py`, `tests/test_preflight.py` (8), `SUPPORTED.md`: forcing A picks B and records why, forcing both leaves the recorder installed-but-not-capturing with two reasons and no exception, an absent library is `unavailable` rather than `forced-failed`, an `install` that raises does not escape, a client handed to the preflight is wrapped rather than having its class patched. `capture_seam` is written into the decision (`as_dict()`) and tested; writing it into a `run.json` waits for the sink (P3-06) and is labelled pending in `SUPPORTED.md` |
| **P3-05c** seam C, SDK method patches | §18.5, D-023 | `src/blackbox/cap_sdk/seam_sdk_methods.py`, `tests/test_seam_sdk_methods.py` (10) against **real** `openai` 3.51.0 and `anthropic` 2.8.0 pointed at the mock provider: `chat.completions.create` (streaming and not), `embeddings.create`, `messages.create`, sync and async; the SDK signature survives the patch (`inspect.signature` compared); an unrecognised SDK shape is skipped and listed, never guessed; a 429 is recorded as an error and re-raised. Every record is `partial=True` with `bytes_received = 0` because a parsed event is not a wire byte (D-023) |
| **P4-10** the viewer: one offline HTML file | P4-10, D-045 | `src/blackbox/ui/` (`model.py`, `viewer.py`, `static/{index.html,viewer.css,app.js}`), `tests/test_ui_viewer.py` (20), `blackbox view`: the stylesheet, the script and the capsule's summary are inlined into one page, so `file://` opens it with **no request of any kind** — the suite fails the built page on `http://`, `<link>`, `fetch(` and any non-fragment `href`/`src`. Black-and-gold glass, with contrast computed from the stylesheet's own tokens (body and accents ≥ 4.5:1) and `prefers-contrast: more` raising the muted ones. Keyboard-first: `role="tree"` rows with roving `tabindex`, ↑/↓/←/→/Home/End/Enter/`/`/`?`/Esc, a skip link and an `aria-live` banner; `prefers-reduced-motion` is honoured in the CSS *and* by the script before it asks for a smooth scroll. Capsule data reaches the DOM only through `textContent` (no `innerHTML` in the file) and the JSON block escapes `<`, so an XSS corpus of six payloads survives a build → parse round-trip unchanged. A capsule that fails its own root, a torn tail and an open node are reported as such, and a damaged archive is exit 2 rather than a page |
| **P4-17** the diagnosis workflow: evidence in, an incident report and a repair brief out | P4-17, D-047 | `src/blackbox/diagnose/` (`evidence.py`, `findings.py`, `report.py`, `cli.py`), `tests/test_diagnose.py` (25), `blackbox diagnose`: reads one capsule within a frame bound and a size ceiling, classifies what was cut short with §14.4's own function, and ranks findings `observed` / `likely` / `possible` / `cannot-tell` — each with what else fits the same frames and the check that would settle it. A tool with no result is `cannot-tell`, never "the tool did not run"; a proxy seam never claims the app read the bytes. Recorded application text is scrubbed of credentials, fenced and labelled untrusted; the brief names no `path:line` it cannot see, states its unknowns, and carries success criteria phrased against a *new* capsule. Read-only by construction: an AST test fails the build if a socket, subprocess, `eval`/`exec`, or `os.system`-class call ever appears in the package, and `--brief`/`--out` refuse to overwrite an existing file without `--force`. The same viewer page now shows the findings and a copy/save-as-file brief, computed into the document because the page is a file with no Python in it |
| **P4-11** the viewer over loopback, once, at a secret path | P4-11, DV-015 | `src/blackbox/ui/server.py`, `tests/test_ui_server.py` (13), `blackbox serve`: binds `127.0.0.1` on an ephemeral port (the bind is a literal and there is no host argument), serves the same inlined page at a per-process secret path, and stops after the first successful fetch unless `--keep-open`. A wrong path is a 404 that confirms nothing and does not consume the token; `/favicon.ico` is answered without consuming it; the second fetch gets a 404 from the handler rather than a race with shutdown. Headers: `CSP default-src 'none' … frame-ancestors 'none'`, `nosniff`, `no-referrer`, `no-store`. The access log is silenced because the request line contains the token, and `log_message` is asserted to write nothing. The module has no client-socket call at all (AST test), and `tests/_capture_policy.py` records it as the third named exemption, scoped to `http`/`socket`/`socketserver` |
| **P4-13** `events/events.jsonl`: the timeline as one JSON object per frame, verified like the graph (P4-13) | P4-13, D-054, §13.1a | `src/blackbox/cap/export_jsonl.py`, `src/blackbox/cap/derived.py`, `tests/test_export_jsonl.py` (11), `tests/test_capsule_graph.py` (10): the part ships inside the capsule and is declared in `manifest.derived_parts[]` with its sha256, and `blackbox capsule verify` re-renders it from the capsule's own segments — a re-render of a real sealed capsule is **byte-identical** (sorted keys, LF, final newline, no generation timestamp), an edited line is reported as `BBX-E412` with the line number and its stored and recomputed text while the capsule stays exactly as it was, an old capsule without the part verifies as `absent`, and a part this build cannot recompute is printed as `unknown` rather than counted as a pass. The names are the pinned ones — OpenTelemetry semconv **v1.27.0**: `gen_ai.operation.name` (`chat` / `text_completion`, from the recorded path, only for the APIs the mapping lists), `gen_ai.system` (from the recorded host, only for a provider we can name) and `gen_ai.request.model`; everything else is `bbx.*`, and a test asserts every attribute of every line is in one of the two namespaces. Three absences are deliberate and documented: no provider guess (the host still goes to `bbx.provider.host`), no `error.type` (the tape has a message, not a type), and **no chunk content** — a `MODEL_CHUNK` line carries a byte count and the `[bbx-…]` tag, so the export is a timeline and not a second copy of the text the content policy was written to protect. A payload of a kind the mapping says nothing about is carried under `bbx.payload`, or as `{bytes, sha256, truncated}` above 2 KiB |
| **P4-20** `otlp/spans.pb`: the protobuf encoding a real receiver takes, and the import read back | P4-20, D-059, §13.1 | `src/blackbox/cap/export_otlp.py` (`render_pb`), `tests/test_export_otlp_pb.py` (12), `bench/results/2026-11-16-otlp-pb-importer.json`. The same span model as the JSON export, written as OTLP protobuf by a hand-written wire encoder (the package has no dependencies and this part was not going to be the first): varint/fixed64/length-delimited, field numbers read from the `opentelemetry-proto` **1.45.2** descriptors, and a test decoder that **shares no code with the encoder** — which is how the one real bug was caught: span attributes were emitted at field 1 (correct for `Resource`, wrong for `Span`, whose is 9) and a real decoder read every id as an attribute blob. Byte-stable by construction (ascending field numbers, attributes sorted by key, spans by `(start, seq)`), shipped as the fourth declared derived part, re-rendered and compared **as bytes** on every CI cell, and reported on drift as `bytes (N stored, M recomputed)` with both sha256s rather than an invented line number. **Measured**: Phoenix **21.0.0** answered **200** to the capsule's own bytes and read back 3 spans with the trace id `50444de0…` byte for byte, both children naming `bbx.run`'s span id as parent, `bbx.model.bytes_received` still an integer, every `bbx.*` attribute unchanged and Phoenix's `llm.system`/`llm.model_name` derived from our `gen_ai.*` names; the same receiver answers **415** to `otlp/spans.json`. Langfuse, the Collector's OTLP/JSON receiver and Phoenix's gRPC port are **untested** and named as such |
| **P4-02** the manifest schema, executed on write and on read | P4-02, D-058, §13.2 | `src/blackbox/cap/manifest.py` (`SCHEMA`, `validate`), `src/blackbox/cap/format.py`, `tests/test_manifest_schema.py` (10). The schema is data this build executes — nested `(kind, required)` pairs walked by one function — not a paragraph: checked **on write** after `format`/`integrity`/`signature` are filled in and before a byte is written, and **on read** after the format-version gate. A missing required key or a wrong type is `BBX-E407` naming the dotted path (`manifest 'run.run_id' is missing`). Required means every capsule this format has produced carries it, so `derived_parts` (P4-13) and `chain_root`/`capture_vantage`/`capture_seam`/`links_summary` (P4-16) are **optional** — a manifest with all of them deleted still reads and still verifies, which is the compatibility rule pointing both ways. Unknown keys are **tolerated, preserved and reported** on `Capsule.unknown_manifest_keys`. Two deliberate strictnesses: `True` is not `1` (a bool where an int belongs is refused), and the validator raises on a spec kind it does not implement. What it does not check is meaning — that `counts.events` matches the frames is `derived`'s job, that `chain_root` is the capsule's own is `blackbox verify`'s |
| **P4-16** `blackbox verify`: five checks, three ways to name a target, and one check that refuses to be a pass | P4-16, D-057, §13.1a/§13.2 | `src/blackbox/cap/manifest.py`, `src/blackbox/cli/cmd_verify.py`, `tests/test_verify_cli.py` (18). `blackbox verify <capsule>`, `--run <id> --data DIR`, `--writer <uuid> --data DIR` run the same checks and exit **0** verified / **9** a check failed / **2** the target cannot be read. The manifest now declares `chain_root`, `capture_seam`, `links_summary` and `capture_vantage`: the first three are **recomputed from the capsule's own bytes** (the `CHAIN` frames, the `MODEL_REQUEST` payloads, `links.json`) and a disagreement fails the named check; every derived part is re-rendered byte for byte and a drift is `BBX-E412`. The fourth is **not checkable from a capsule** — a capsule carries no `meta` page — so it is printed as `declared` with `ok: null, checkable: false` in `--json`, counted in a "declared, not checkable" line, and never as a pass: a green line there would be a check that never ran. `--writer` is a check, not a lookup: the capsule id is recomputed from the writer and the capsule's own run id, and a capsule claiming a tape it did not come from is reported rather than filtered out. One tamper test per part — a raw entry through the integrity root, a derived part through `BBX-E412`, a manifest field through its recomputation — plus a test that a failed verification leaves the capsule byte-identical |
| **P4-14 (code half)** `otlp/spans.json`: the same frames as OTLP/JSON, validated against a schema snapshot; the importer clause is **not** ticked | P4-14, D-056, §13.1a | `src/blackbox/cap/export_otlp.py`, `tests/test_export_otlp.py` (10), `tests/_schema.py`, `tests/schemas/otlp-spans.schema.json`; `tests/test_export_jsonl.py` (11) and `tests/test_capsule_graph.py` (10) updated for the third part. **The mapping**: `bbx.run` is the root, a node becomes `invoke_agent {gen_ai.agent.name}` (or `invoke_agent` with no name), a node whose kind is `tool` becomes `execute_tool {gen_ai.tool.name}`, and a model call becomes `{gen_ai.operation.name} {gen_ai.request.model}` as a CLIENT span — with `bbx.model.call`, no semconv name, whenever the path is not one the mapping knows, which is D-054's rule one level up. **Derived, not drawn**: `traceId = sha256(run_id)[:32]` so two capsules of one run share a trace (claimed at trace level only — it is **not** node-level evidence: a shared trace id is not a node edge, and node-level edges are joined by the per-call id, not by the trace), `spanId = sha256(capsule_id:seq)[:16]`, so the writer's uuid is not needed and the capsule stays the only input. **Verified like the other parts**: declared in `manifest.derived_parts[]`, re-rendered byte for byte, an edit reported as `BBX-E412` with the first differing line, nothing rewritten; an unclosed node or call gets a span that ends at the last frame with `bbx.span.truncated` and an error status naming the missing frame. **Absent on purpose**: no `gen_ai.usage.*` (no response decoder, P3-08 open), no finish reasons, no request parameters, and no prompt or completion text — counters and fingerprints only. Both exports now share **one** semconv pin, v1.39.0 (`6d05e92`), which renamed `gen_ai.system` to `gen_ai.provider.name`; the JSONL export was moved with it, pre-release, rather than carrying two vocabularies. **The schema snapshot** is checked by `tests/_schema.py`, which implements the keyword subset the snapshot uses and **raises** on any keyword it does not implement, so the snapshot cannot grow a check nobody runs. **The importer clause — "a Phoenix/Langfuse-class importer accepts the file" — was not performed**: no such importer runs in this environment, so the ticket is `wip` and the check is an open blocker (see Labelled) |
| **P4-19** the interruption lab: kill for real, recover for real, diagnose for real | P4-19 | `bench/interruption_lab.py`, `tests/_interruption_child.py`, `tests/test_interruption_lab.py` (18), evidence in `bench/results/2026-11-16-interruption-lab.json`: one child per §14.4 class writes real frames through a real `TapeWriter` and is killed with a real signal (`SIGKILL`, or the `TerminateProcess` fallback), then the parent runs the real recovery and the real diagnosis. Three repeats per class: every capsule is built, every integrity root verifies after the kill, and the node statuses and findings are identical across repeats. The mid-stream row is `likely` with `MODEL_CHUNK` evidence and its alternatives; the killed tool is `cannot-tell` — *the side effect may have happened* — and the words "did not run" appear nowhere; the clean row has no interruption finding. No row asserts an exit code, because `-9`, `137`, the signal number on Windows and an `os._exit` fallback are four different conventions for one idea |
| **P4-18** the approved-fix workflow: preview, token, apply, verify | P4-18, D-049 | `src/blackbox/repair/` (`proposal.py`, `apply.py`, `cli.py`), `tests/test_repair_workflow.py` (42, fixtures write bytes so the hash and the file agree on every platform — defect 48), `blackbox repair preview|apply|verify`: the product still proposes nothing — `diagnose` writes evidence, and a human or an agent writes the proposal. `preview` prints exact paths, a unified diff, named risk notes (not a score), the suggested tests **as suggestions**, and the scope, then writes nothing. `apply` refuses without a token bound to the proposal's bytes, the repository root and every target's reviewed hash — so a file edited after the preview, or a proposal edited after approval, aborts with nothing written. Paths must be relative, inside the repository, with no `..`, no absolute path and no symlink anywhere in the chain; deletion is unsupported; writes are atomic per file, preserve the mode, keep originals in `.blackbox-repair/`, and a failure part-way restores every file and removes what it created. `verify` runs only commands a human types (`--test`, repeatable) and reports each one's real exit code and output tail; with none it exits 3 and says the change is *unverified*. A proposal's `tests` are never executed — `tests/test_repair_workflow.py` proves it with a sentinel file that must not exist, and `docs/DECISIONS.md` D-049 records why the product applies your proposal rather than writing one. **Hardened after review** (D-050), because the first version checked the tree only while *reading* it: every path check, hash check and link check now runs again per file immediately before that file's write (a target swapped for a symlink after approval, a parent directory swapped, and a swap landing mid-apply are three separate tests that fail closed); descriptors are opened `O_NOFOLLOW` and the read path goes through `platform().open_fd` so Windows cannot translate line endings in the bytes it backs up or restores (a CRLF-and-NUL file is compared byte for byte across apply and rollback); backups are `<repo>/.blackbox-repair/backup-<UTC>-<token8>` mode `0700`, refused if symlinked, and re-hashed after copy, so a corrupted backup stops the apply before a write; `apply-manifest.json` is written before the first mutation and ends `applied` or `rolled_back`; rollback restores in reverse with its own retry path, attempts every file, names the ones it could not restore, and runs on `KeyboardInterrupt` too. On Windows `O_NOFOLLOW` does not exist, so that platform's guarantee is check-then-write — recorded as the limit it is, not claimed closed |
| **P3-06** the sink: seam events become frames | §18.5, D-024 | `src/blackbox/cap_sdk/sink.py`, `tests/test_sink.py` (8): 2 000 chunks become **8 chunk frames** (bound 40) with the payloads concatenating back to the identical body; the time bound flushes at 100 ms without waiting for 8 KiB; `MODEL_REQUEST` before every chunk and `MODEL_RESPONSE`/`MODEL_ERROR` after the last; frames land on a real tape and the same `scanner` a recovery uses reads them back with a sha256 match against the body; an unfinished call is left as chunks with no response, which is the shape `recovery` classifies as `INTERRUPTED_MID_STREAM`; a sink that raises is counted and never reaches the application |
| **P3-11** redaction before persistence (the corpus, the modes, the default policy) | §12.2, P3-11, D-029, DV-013 | `src/blackbox/redact.py`, `tests/_secrets.py` + `tests/test_redaction.py` (139): 60 synthetic secrets of 10 kinds, each caught alone and then **at every split offset of every one of the 60** (the network chooses where a read ends, and a secret cut in half is exactly what a per-chunk regex cannot see), plus 20 random chunkings of the whole corpus in one stream, one byte at a time, and binary payloads around a secret; ordinary SSE JSON comes out byte-for-byte identical; `off` is proved off rather than quietly redacting; `hash` is a stable pseudonym; the buffer is bounded and empty after `flush()`; and the end-to-end test pushes the corpus through a real seam, a real sink and a real tape, then greps the tape and the `MODEL_RESPONSE` frame. **Not built, and named here rather than implied:** the keyed HMAC of §12.2/D8 (DV-013), mask offsets, and the `pure/patterns.py` split — P3-11 stays `wip` |
| **P3-11d** the keyed pseudonym: HMAC under a per-machine key | DV-013 → DV-014, D-031, §12.2 stage 7, D8 | `src/blackbox/keys.py`, `src/blackbox/patterns.py`, `tests/test_redaction_key.py` (11): the key is created once at `0600` in a `0700` directory and reused; **four processes starting at once agree on one key** (`O_EXCL` plus a read-back); the pseudonym equals `HMAC-SHA256(key, secret)[:8]` and provably not the bare digest; the same key correlates across processes and a different key does not; a capsule records `key_id` and contains neither the key nor its hex; an unusable key path falls back to the unkeyed digest **and records `keyed: false`** rather than implying a key it never used; `full` and `off` create no key file at all. The pattern tables now live in `src/blackbox/patterns.py`, so the rules can be reviewed without the state machine. **Still absent**: mask offsets (DV-014) |
| **Redaction is a default, holds only what is unfinished, and fails closed** | D-029, P3-11, C4 | `src/blackbox/cap_sdk/sink.py`, `tests/test_redaction.py`: `TapeSink(emit)` with no argument redacts (`Redactor.from_config()`, `BLACKBOX_REDACTION`, default `full`, a misspelt mode falling back to `full`); a chunk is held only while it is unfinished, so **an ordinary SSE line is released on the read that brought it** and the day-14 `SIGKILL` test — which the first, fixed-tail design broke by recovering no chunks at all — passes again (defect 29); a redactor that raises writes `[REDACTION_FAILED]`, keeps the call's place in the stream and counts `redaction.failures` instead of dropping bytes silently; and the summary reports `chunks_raw` (reads) apart from `chunks_stored`/`bytes_stored` (what the tape got), which is the first half of the directive's P0 item 4 — counts are not a content fingerprint — done by naming both numbers and their difference |
| **The zero-code-change row, both SDKs** (G-Capture §15.1) | §4, G-Capture, P3-11b | `tests/_day14_child.py` + `tests/_day14_child_anthropic.py` driven by `tests/test_day14_kill.py` (6): the same experiment run against **`openai` 3.51.0 and `anthropic` 2.8.0** — an unmodified SDK script, killed with `SIGKILL` mid-stream, recovered as `INTERRUPTED_MID_STREAM` with chunk and byte counts that never exceed the mock provider's own tally; the anthropic control records the whole stream as `MODEL_RESPONSE` with `path` `/v1/messages` and bytes equal to the concatenated chunk payloads; and the two SDKs are shown to take **the same seam** in the same run, which is what makes "zero code changes" one claim rather than two |
| **Day-14 rule** (the gate's core mechanism; **G-Capture itself is still open**) | §4, G-Capture, D-025 | `tests/_day14_child.py` + `tests/test_day14_kill.py` (3): an **unmodified** `openai` script (client built after install, `chat.completions.create(stream=True)` as documented) is killed with `SIGKILL` mid-stream; `recovery.recover()` then reports the node as `INTERRUPTED_MID_STREAM` with `chunks ≥ 1`, `partial_stream_bytes > 0`, and **both no larger than the mock provider's own tally of what it pushed down the socket**; the control run (no kill) records the whole stream and ends with `MODEL_RESPONSE` whose byte count equals the sum of its `MODEL_CHUNK` payloads; `capture_seam` and the coalescing parameters are readable in the request frame |
| **Fail-open, measured as identical application behaviour** | P3-14, §4 | `tests/test_failopen.py` (8): a 100-call agent run twice — once bare, once with seam A and the sink installed — with **identical return value, identical stdout**, and when it raises at call 37, the **same exception type and message**. The recorder is then broken three real ways and the comparison repeated: an impossible configuration (`segment_bytes=1`, which `TapeWriter` refuses with `BBX-E130` on **every** OS: `install()` returns `enabled: False` and that error as its reason, asserted, so the test cannot pass against a recorder that was never broken), a data directory that is a file (POSIX, because Windows let the writer open anyway — defect 26), a keeper that raises on every publish, and a tape truncated by 512 bytes the way a previous `kill -9` leaves one |
| **The Windows teardown fixture touches nothing it did not identify** | defect 25 | `tests/_hygiene.py` + `tests/test_gc_hygiene.py` (3): the fixture finds a leaked writer by **exact type**, proved against an object that raises on any attribute access and a subclass that must not be mistaken for its parent; the SDK's real `LazyProxy` is instantiated to show the shape still raises. This is why three Windows cells were red from `c7919f9`: the old fixture called `getattr(obj, "_head")` on every live object, `anthropic` keeps optional dependencies behind a proxy that raises on load, and the resulting `ERROR` landed on nine innocent property tests |
| **The nightly workflow actually runs** | defect 28, P2-09 | `.github/workflows/nightly.yml`: both steps named files that do not exist, so the only scheduled run this repository has had failed with exit 2 (`kill-matrix`) and exit 4 (`cbor-fuzz`). Now `tests/kill/kill_matrix.py --trials 2000 --json …` and `bench/fuzz_cbor.py --iterations 100000 --out …`, each smoke-run here before the workflow was touched |
| **Both client modules, both directions, and cancellation** | P0 §"verify, do not infer" | `tests/test_capture_modules.py` (11): the same five claims run against **`httpx` 0.28.1 and `httpx2` 2.16.0** directly (not through an SDK): a stream is byte-identical to an unseamed client with `call.chunks` equal to the reads the application actually made, a plain POST is one call and one chunk with `stream is False`, a truncated stream is recorded short and never completed, an async stream is identical with every read counted, and a **cancelled** task keeps `CancelledError` while the call is left `partial=True` with the chunks that arrived. The chunk expectations are the *observed* read count, never a fixed number — a six-chunk stream can arrive as four reads on a slow runner, and asserting otherwise is a flake (C40) |
| **A stream closed early is marked `partial`** | D-023 applied end to end | `tests/test_capture_modules.py::test_cancellation_mid_stream_stays_cancelled_and_leaves_the_call_open` (both modules), `src/blackbox/cap_sdk/tee.py`: `break`, timeout or cancellation closes the tee, which sets `call.partial = True` and records the status that actually arrived; a completed iteration records the same call without the marker. The mock provider now labels a non-streaming answer `application/json` and a stream `text/event-stream`, so `call.stream` is decided from the header the provider really sent rather than from a fixture that said SSE to everything |
| **Capture scope: an explicit allowlist, with skips counted** | D-026, P0 §"immediate" | `src/blackbox/cap_sdk/scope.py`, `tests/test_capture_scope.py` (12): the default scope is `api.openai.com`, `api.anthropic.com` and loopback — nothing else; `capture_hosts=` / `BLACKBOX_CAPTURE_HOSTS` extend it, `"*"` is the only way to capture everything and it says so in `source`; ports and schemes are ignored, `*.example.com` matches subdomains and **not** the bare domain. Seam A and seam B both consult it and return before opening a call, so nothing half-written is left on the tape; the skip is counted in `detail["scope"]["stats"]["out_of_scope"]`, because an absence is not evidence. Proved in both directions: an out-of-scope loopback request yields **zero** calls and byte-identical output, and an unreachable unrelated host fails with the *same* exception type as it does unseamed |
| **A data directory that is a file is refused by name, on every OS** | defect 26, closed | `src/blackbox/api.py`, `tests/test_failopen.py::test_a_data_directory_that_is_a_file_is_refused_on_every_os`: `install()` now rejects an existing non-directory before any directory is touched (`NotADirectoryError`, "exists and is not a directory") instead of relying on `os.makedirs` to refuse — which on Windows it did not, and the writer opened against a tape directory that cannot exist. The test asserts the reason string, so a platform that starts tolerating the case cannot pass silently. **Windows evidence is the CI cell, not an observation made here** |
| **Fail-open is per callback, not per seam** (the reviewer's P0 at `ee210ae`) | P0 §"immediate", D-027, defect 27 | `src/blackbox/cap_sdk/failopen.py`, `tests/test_capture_failopen.py` (18): `open_call`, `receive` and `close_call` each raising, on seam A and seam B, sync **and** async — the application gets every byte, the same body and no exception, and the failure is counted. Reproduced first in a clean worktree at `ee210ae` (the reviewer's script printed `ESCAPED RuntimeError: recorder blew up on a chunk`); after the fix the same script prints `SURVIVED bytes=849`. Also asserted: `CancelledError` is not swallowed, a provider 429 arrives as 429 with the library's own error, the failure sample is bounded at 64, and no exception *message* is ever recorded |
| Capture layer's network exception | P0-08, P0-13, D-020 | `tests/_capture_policy.py` + `tests/test_stdlib_only.py`, `tests/test_no_egress.py`, `tests/test_codec.py`: four named files are exempt and nothing outside `cap_sdk/` can be; `httpx` is allowed only **inside a function body**, so `import blackbox` works with httpx absent; the mock provider may bind loopback only and the wildcard-bind source check runs; `httpx==0.28.1` lives in its own `capture` extra, `[project].dependencies` is still empty |
| Lint, types, forbidden words, egress, stdlib-only | C6, C12, P0-14 | `make fmt` 0 findings, `mypy` strict 0 findings, `tests/test_forbidden_words.py`, `tests/test_no_egress.py`, `tests/test_stdlib_only.py` |
| **The hook path: ingest, the inbox, and the Claude Code adapter** | P3B-05, P3B-07 (first half), §11.9, D-039 | `src/blackbox/hooks/{events,inbox,claude_code}.py`, `src/blackbox/cli/{ingest,hook}.py`; `tests/test_hook_inbox.py` (19), `tests/test_hook_claude_code.py` (13). **The table**: all ten Claude Code events of §11.9's table (its prose says nine) map to the frames it names, and an unmapped event is `MARK{kind: tool_event, raw_name}` rather than a drop. **The privacy rows are assertions, not intentions**: a prompt leaves a 16-byte hash and a byte count and never its text, a tool call leaves a key-order-independent fingerprint of its arguments and never the arguments. **The inbox**: one atomic file per event (`tmp + fdatasync + os.replace`, 50 concurrent ingests through 50 real processes → 50 files, zero corruption, no `.tmp` left), folded into the tape with `NODE_OPEN`/`NODE_CLOSE` sharing a slot that is kept in `inbox/<session>/state.json` so it survives the process, `ingest_id` on every payload so an at-least-once refold is detectable, a corrupt entry quarantined to `inbox/.bad/` and counted rather than dropped, and a close with no matching open written as a `MARK` that says so. **The adapter**: install/remove on a settings file with unrelated keys returns it **byte-identical**, our entries are found by marker, a second install adds nothing, a user's edit made in between survives, and an unknown schema is refused (`BBX-E151`) before a byte or a backup is written. **Cold start, measured here and now with an artefact** (`bench/bench_ingest_cold_start.py` → `bench/results/2026-11-19-ingest-cold-start.json`, n 60 per column): `blackbox ingest` p50 33.0 / p95 44.5 / p99 53.9 ms, against a bare-interpreter p50 10.9 ms and a CLI floor of p50 32.2 ms, so the command itself adds 0.85 ms and the rest is process start; the plan's ≤ 40 ms p95 is a **bench-box** row, this host misses it at the p95, and the artefact says so (`on_the_bench_box: false`) |
| CI on three OSes x three Pythons + docs | C7 | run `44220446574` at `ad3a783` — **ten jobs, ten `success`, macOS included** (all three macOS cells executed 11 steps each, the first macOS result on this branch after three runs of cancellations at 0 steps); `docs/compatibility.md` holds the per-cell verdicts and every historical red run. The next run at `f57c2ac` (`44223453730`) was red on **macOS 3.11 only**, on a probe test whose ceiling assumed a quiet machine — the first macOS samples of the probe tests on this branch, and the flake is defect 63, fixed in the commit that adds it |
| **P3B-09** the attach matrix, with four rows run instead of read | P3B-09, §11.7, §13, D-042, defect 39 | `docs/attach.md`, `tests/test_attach_doc.py` (14), `tests/test_attach_sdks.py` (7). Every row of the two verdict tables carries a version **and a date** or the word **unverified**, which the doc test enforces mechanically (it refuses a `**verified**` row with no evidence — checked against a doctored document in the same session). Four rows are verified because a client was **run**: `openai` 3.51.0 and `anthropic` 2.8.0 (Python 3.11.2), `curl` 7.88.1 and `node` v22.22.3 reach `/v1/chat/completions` and `/v1/messages` on a provider-shaped origin through the base URL `plan_exports()` hands them — each asserted on the path and body the **origin** received, so the client chose its own path. Eleven rows are unverified and say what is doubted (Node/Go/Java/C#/Rust/Ruby/PHP SDKs, Aider, LangChain, LiteLLM, Cursor, a real Claude Code session, Codex's settings shape). **Running them found defect 39**: the flat export lost the `/v1` the OpenAI SDK does not append, so a proxied call went to a path the provider does not serve while every existing test stayed green because those tests typed the path by hand. Fixed in `BASE_URL_SUFFIX` and pinned by a two-column equality test against the default upstream table |
| **P3B-06** the Codex adapter, and the assumption it prints | P3B-06, §11.9, D-041 | `src/blackbox/hooks/{settings,codex,events}.py`, `src/blackbox/cli/hook.py`; `tests/test_hook_codex.py` (26). **Twelve events**: each of `session.start/end`, `turn.start/end`, `user.prompt`, `tool.call`, `tool.result`, `tool.error`, `agent.message`, `context.compact`, `notify`, `error` maps to the frame the ticket names, and an event whose name this build does not know becomes `MARK{kind: tool_event, raw_name}` rather than a drop. A recorded session through the real folder becomes `RUN_START` … `NODE_OPEN`/`NODE_CLOSE` on paired slots closing innermost-first — `RUN_END`, so the mapping is checked by driving it, not by reading a table. **The honesty part**: no documentation of Codex's settings location or hook-block shape could be read from this sandbox, so the adapter declares `verified=False` and the belief is *printed* — `blackbox hook install --codex` writes the assumption to stderr, `hook status --codex` prints `NOT verified` and `"schema_verified": false` in `--json` — rather than living only in a paragraph. It is also *contained*: an unknown `hooks` shape is refused (`BBX-E151`) before a byte or a backup is written, `--settings <path>` lets an operator point it at the real file, install/remove round-trips **byte-identically**, a user's own hooks beside ours survive, and an edit made in between survives `remove`. The shared machinery in `hooks/settings.py` is the same code the Claude Code adapter runs, so the two adapters cannot drift |
| **Three vantage points on one timeline, end to end** | P3B-07 (second half), §11.9 step 6, §11.10, D-040 | `src/blackbox/rec/launcher.py`'s `_InboxFolder`, `tests/test_hook_launcher.py` (5), `tests/test_hook_inbox.py`'s S18 row (20 in that file). A child process fires five real hooks through `blackbox ingest`; the launcher folds them every 250 ms into a tape of **their own** with `meta.vantage = 3`, and one last time at exit before `RUN_END`. The assertions are the chain, not the parts: the hook tape's five events become `RUN_START`, `MARK` (the prompt's hash and byte count, never its text), a `NODE_OPEN`/`NODE_CLOSE` pair on one slot, `RUN_END`; `correlate.resolve_one` resolves **`exact`** between the two tapes with `carried_by: x-blackbox-run`; and the launcher's **own capsule** carries that link in `links.json`. Two processes, one timeline, with the class of the claim written on the tape. A run that fires no hooks leaves no hook tape (an empty artefact would look like a measurement); a corrupt entry is quarantined without touching the child's exit code; the fold thread is finished when `launch()` returns; and **S18** is a real `kill -9` between the `tmp` write and the rename (`BLACKBOX_DIE_AT=INGEST.after_tmp`) that leaves nothing a reader could mistake for an event |
| **A hung test costs a failure that names it** | C7, C40, D-037, defect 35 | `tests/conftest.py`'s autouse watchdog (`BLACKBOX_TEST_TIMEOUT`, default 120 s, stdlib only — the `dev` extra stays `{pytest, hypothesis}`), its report file in the tempdir, `tools/ci_summary.py`'s **single** fixed-shape annotation (`exit N · out M lines · hung yes/no · <FAILED line> · <last useful lines>`, ≤ 600 chars, printed even when the child said nothing), and `timeout-minutes: 15` with `-s` on the step. What the change bought is a run that names the test instead of the orchestrator: `a54dda0` (`43742600971`) reported `FAILED tests/test_headers.py::test_the_proxy_reads_the_run_id_and_the_upstream_never_sees_it — ran longer than 120 s at …/python3.12/selectors.py:468 in select`, where `7aa5105` (`43727756275`) had said only *"the action has timed out after 15 minutes"*. That line was the whole diagnosis of defect 35: one `asyncio.run()` loop hosting client, proxy and origin, which 3.12 parks in `select()` with nothing runnable — and in two days it caught two more flakes of the same species (36: a chunk count compared to an event count; 38: a byte bound a clock could answer), each on exactly one cell of an otherwise green run. The test now runs the proxy on its own loop in a thread, the origin as a `ThreadingHTTPServer` and a synchronous client (7 tests, unchanged count), and the 3.11/3.13 cells were green before and after, which is what made the platform the variable |

## Labelled

| Item | Label (requires humans / requires time / not testable here / blocked) | Reason / fix attempts |
|---|---|---|
| P4-14's importer acceptance | requires humans | **Met at P4-20 for Phoenix**: `arize-phoenix` 21.0.0 answered 200 to the capsule's own `otlp/spans.pb` and read back 3 spans with the trace id `50444de0…` byte for byte, the parent chain, integer types and every `bbx.*` attribute intact (read with `arize-phoenix-client` 3.7.0). The same receiver answers **415** to `otlp/spans.json` — protobuf-only — which is why the capsule ships both encodings. **Widened on 2026-11-18** (`bench/results/2026-11-18-otlp-importers.json`): the capsule's own `otlp/spans.pb` parses as the official OTLP messages (`opentelemetry-proto` 1.45.2 / `protobuf` 7.36.2 — 3 spans, names `bbx.run` / `invoke_agent agent` / `chat gpt-4`, trace id `50444de0…`, every `parent_span_id` resolving inside the message), `otlp/spans.json` parses as proto3 JSON with `ignore_unknown_fields=False`, and an OTLP/**gRPC** `TraceService` built from OTel's own servicer (`grpcio` 1.85.3) accepts the same bytes and returns `ExportTraceServiceResponse`. That closes the transport half of the question without pretending it is a product. **Untested and named as such**: Langfuse (its SDK is an exporter, not a receiver, and the server needs TypeScript plus Postgres), the OpenTelemetry Collector, Grafana Tempo and Jaeger (Go release binaries behind `objects.githubusercontent.com`, which this sandbox cannot reach — the fetch stops at 0 bytes), Phoenix's gRPC port 4317 (the 2026-11-16 run used its HTTP receiver). Three artifacts hold the raw results (`bench/results/2026-11-16-otlp-pb-importer.json`, `…-otlp-importer.json`, `2026-11-18-otlp-importers.json`) |
| **Independent gate review** | G-Promise: **independent review pass (owner-relayed)**; every other ticket row: requires humans | G-Promise was counter-signed by a different model family on a different machine (fresh clone of `fdb3875`, CPython 3.13.18, ext4): 337 passed / 1 skipped, 100/100 kill trials with 0 violations, smoke 9 keys, launcher recovery, and it was that reviewer who found C40. The signature and a copy-paste reproduce block are in `docs/gates/G-Promise.md`. **Ticket rows in `docs/reviews.md` stay `self`** until a second model is wired into the loop; none of them counts as independent. |
| P2-09 week of nightlies | requires time | **The nightly has now passed once, and not yet on the event that counts.** `43748375862` on `main` `1390c9b` (a **`workflow_dispatch`**) is the first green nightly in this repository's history: `kill-matrix` ran 2000 randomized `kill -9` trials and `cbor-fuzz` 100 000 buffers, both `success`. The only `schedule` run that has ever fired is `43675454143` at `31241d6` (2026-11-15 04:08Z), and both of its jobs failed on defect 28 — the steps named `tests/kill/randomized_kills.py` and `tests/test_cbor_fuzz.py`, which do not exist (exit 2 and exit 4); both now call the real drivers, which is what the dispatch proves. The clock the plan asks for is seven consecutive `schedule` runs, so it still has not started: **one green dispatch is evidence that the jobs work, not evidence of a week** |
| **Codex's real hook schema** | not testable here | `hooks/codex.py` is `verified=False` on purpose: Codex's settings path and the shape of its hook block could not be confirmed from this sandbox, so the ticket is delivered as a *labelled assumption* — the product surface says `schema_verified: false`, `install --codex` prints the assumption, and `docs/STATUS.md` (this row) names it. What the assumption could cost is bounded: an unknown shape is `BBX-E151` with no write and no backup, `--settings <path>` overrides the guess, and the twelve event names map whether or not the path is right. Fix attempt: a real Codex install to compare against, which is not available here |
| 3-person stranger test | requires humans | Prepared to one command: `docs/onboarding.md` holds the setup line and three tasks; nobody outside this session has run it. |
| Blinded diagnosis benchmark | requires humans | Prepared: `docs/benchmarks/diagnosis.md` holds the checklist and the capsule set is reproducible with `blackbox recover`. Needs a second and third person. |
| S17 sparse-control run under `sudo` | requires humans | `tools/s17_control.sh` builds the 2 MiB tmpfs control and prints the signal; no superuser here. |
| Real-provider capture (day-14 rule) | not testable here | No provider keys in the environment; the gate is the mock provider, as the plan allows. |
| Licence decision, release tag, merge to `main` | requires humans | `release.yml` is prepared and **not** triggered; no tag, no `LICENSE`, no licence field. |
| P10, P12 | blocked (not started by design) | P10 starts only after every v1.0 gate is green; P12 never without instruction. |

## The desktop app (2026-11-19)

`blackbox desktop [path]` opens the offline viewer in a window of its own, using the **system** webview (WebKitGTK on
Linux, WebView2 on Windows) rather than bundling a browser engine. The core stays stdlib-only; `pywebview` is an optional
extra imported only by the window code, and `tests/test_stdlib_only.py` still holds.

| What | Where | Evidence |
|---|---|---|
| The shell: loopback URL, secret path, `debug=False`, server stopped on close, refusal instead of a browser tab | `src/blackbox/ui/desktop.py` | `tests/test_desktop.py` (12) with a fake webview backend; `desktop --selftest --json` on a real machine |
| Linux package: `.deb` with launcher, `.desktop` entry, icon, `.bbx` MIME type, vendored `pywebview`, `Depends: python3 (>= 3.11), python3-gi, gir1.2-webkit2-4.1` | `tools/package/linux/build_deb.sh`, `debian/` | `tests/test_desktop_package.py` (6): built here, extracted with `dpkg-deb -x`, `usr/bin/blackbox --version` and `desktop --selftest` run from the extracted tree |
| Windows installer | `tools/package/windows/{blackbox.iss,build_windows.ps1}` | **not built, not run**: no Windows, no Inno Setup, no wine here. The journey is the open gate row |
| macOS | — | experimental and untested; no package |

**Not claimed:** that a window has appeared on any machine. This environment has no display, no `python3-gi` and no
WebKit, so `desktop --selftest` correctly reports `missing: python3-gi + gir1.2-webkit2-4.1` and exits 3 — the code path is
tested, the pixels are not. `docs/desktop.md` carries the platform matrix and says the same thing to a user.

## CI as observed, not as hoped (2026-11-18)

| ref | docs | Ubuntu | Windows | macOS |
|---|---|---|---|---|
| branch `6256118` (library rows by name; run 44562203122) | green | **3/3 green** | **3/3 green** | **still queued ~10 min after dispatch** — none of the three macOS cells was acquired; neither pass nor fail, unmeasured |
| branch `87cc056` (setup + library; run 44557174647) | green | **3/3 green** | **3/3 green** | **still queued when this was written, ~20 min after dispatch** — none of the three macOS cells was acquired, so the run's overall status stays `queued`; neither pass nor fail, unmeasured |
| branch `6674ab8`, `1ec8be9` (desktop app + packaging) | green | **3/3 green** — including the job that builds the `.deb` and runs the installed launcher from an extracted tree | **3/3 green** — the suite runs on Windows, the desktop package test skips there by name (no `dpkg-deb`) | **queued, then not acquired**: the three macOS cells sat in the runner queue and were cancelled for capacity; unmeasured |
| branch `0427242` (runs 44534156643 push, 44534172473 PR) | green | **3/3 green** | **3/3 green** — both Windows repairs held (defect 86) | **not measured**: all three cells cancelled / "not acquired by Runner of type hosted" (capacity). The push run's overall conclusion is `failure` for that reason alone |
| branch `47ecb8c` (runs 44532577231 push, 44532588901 PR) | green | **3/3 green** | **2/3** — py3.11 failed on two test-shaped assumptions (defect 86, fixed in `0427242`) | 1/3 green, 2 cancelled at 0 steps — unmeasured |
| `main` `f4a9abb` (run 44400312163) | green | 3/3 green | 3/3 green | **3/3 cancelled at 0 steps** — "not acquired by Runner of type hosted". Unmeasured, not passed |
| branch `9e6a35a` (run 44405724450) | green | 3/3 green | 3/3 green | **3/3 cancelled at 0 steps** — unmeasured |
| branch `c1c0ad8` (run 44412902847) | green | 3/3 green | 3/3 green | **3/3 green — 11 steps each** (this is the first fully measured matrix on this branch) |
| branch `2d0f0db`, `ce61225`, `82c20d2` (runs 44413091184, 44413317497, 44415198012) | green | 3/3 green | 3/3 green | **3/3 green** each — three more fully measured matrices |
| branch `feeb598` (run 44406070968) | green | 3/3 green | 3/3 green except **py3.11**, which failed `test_the_ingest_command_costs_the_agent_tens_of_milliseconds` (84 ms marginal over a bare interpreter, 115 vs 31, against an 80 ms bound) | **3/3 cancelled at 0 steps** — unmeasured |
| branch `af6023f` (runs 44463187708 push, 44463193283 PR) | green | 3/3 green | 3/3 green | **3/3 green in both runs** — ten of ten twice, with the defect-82 repair in |
| branch `fa5d616` (runs 44462471620 push, 44462478169 PR) | green | 3/3 green | 3/3 green | push run **3/3 green**; PR run **2/3**, py3.11 hung in the same burst test at the same 120 s watchdog (defect 82) — the same commit, two runs, one green and one hung |
| branch `d842397` (run 44461898689) | green | 3/3 green | **3/3 green** — the py3.13 cell that failed at `228097f` passed, so the supervisor repair holds | 2/3 green: **py3.11** was killed by the 120 s watchdog inside `test_a_burst_larger_than_the_old_soft_limit_is_still_served` (defect 82), so it reported a hang and no numbers |
| branch `228097f` (run 44417127703) | green | 3/3 green | 3/3 green except **py3.13**, which failed `test_stopping_while_a_handler_is_pending_does_not_abandon_it` — a *precondition* sleep in the test, not the product: on a loaded runner the handler was not yet pending when the stop arrived, so the assertion about abandonment was never reached (`docs/defects.md` 81) | **3/3 green — 11 steps each** |

The Windows py3.11 failure was re-run rather than declared a flake, and the second sample said something different: the
failed step's annotation now reports `BaseProactorEventLoop._start_serving` raising in a callback and **"Task was destroyed
but it is pending!"** — an asyncio loop left open by a test fixture, reported by a *later* test, not a slow hook. That is
this branch's `tests/test_proxy_spans.py`: it runs a proxy on its own event loop in a thread, and it stopped that loop
without closing it (the timing assertion the first sample failed was a shared-runner margin — 84 ms against an 80 ms
bound, with the same measurement 21.7 ms here — and the ingest path was untouched by that commit either way). The fixture
now cancels cooperatively, shuts async generators down and **closes** the loop in the thread that owns it; five
consecutive local runs of the file are silent. The timing bound was not weakened. The same re-run gave **py3.11 /
macos-latest: success** — a macOS sample for `feeb598` — while `py3.12` and `py3.13` macOS cells were still waiting when
this was written. Job logs are not retrievable from this sandbox (`gh run view --log-failed` → `EOF`), so the numbers
above come from the check-run annotations GitHub stores.

**What `228097f` cost, and what was fixed instead of excused.** The one red cell was a test's own precondition
(`test_proxy_supervisor.py`): eleven steps ran, the suite ran, and one assertion never got a chance to fail — the fixture
sleeps 50 ms waiting for a handler thread to be *pending* on a server, and on that Windows runner 50 ms was not enough.
The repair is the one the earlier row already taught (`docs/defects.md` 81): the test now waits on the fact it needs
(`held` is set by the handler once it is in the handler), with a generous ceiling and an explicit failure if the wait
times out — the product is untouched by the change and no bound in the product suite was weakened. Twelve of twelve in
this file pass locally, three consecutive runs just now (12 tests, 0 failures, 0 errors, ~1.0 s each). The new CI attempt is the one recording this paragraph's commit.


## B4's acceptance, run with seven clients (2026-11-18)

`bench/accept_p3b03.py` → `bench/results/2026-11-18-p3b03-acceptance.json`. One loopback origin stands in for the
provider (stated in the artefact), and seven clients run under `blackbox run` against it:

| client | roots ok, one run id | `node_join` | node-level edges |
|---|---|---|---|
| `urllib` (no seam) | 2, yes | `unknown` | 0 |
| Node v22.22.3 (no seam) | 2, yes | `unknown` | 0 |
| `openai` 3.51.0 as shipped | 2, yes | `partial` | 0 |
| `openai` 3.51.0 inside `api.run` | 2, yes | `exact` | 2 |
| `anthropic` 2.8.0 inside `api.run` | 2, yes | `exact` | 2 |
| two `openai.AsyncOpenAI` calls in flight at once | 2, yes | `exact` | 4 |
| one call id written twice by one client | 2, yes | `unknown` | 0 |

Totals the harness asserts: `edges_recorded 22`, `edges_naming_a_node 10`, every capsule's own root verifies, every run
with two capsules is joined by `exact` links, and all seven clients exit 0. Three of the rows exist to keep the other
four honest. The **second SDK** shows the seam is not tuned to one vendor's call shape. The **two concurrent calls**
join to two *distinct* proxy spans, so a resolver that collapsed both onto the first span would fail the verdict. The
**repeated call id** is a run-produced refusal rather than a fixture: the resolver returns `level: "call"` with no
node-level edge, and `repeated_call_ids` proves the id really was repeated per capsule before the refusal is read as a
pass — counting one call id across both tapes would always look like a duplicate, because the app's tape and the proxy's
tape each record it once.

What it does not show: a Node client still cannot produce a node-level edge (`api.run` has no Node equivalent); shape (b)
of `docs/node-edges.md` and a launcher-owned process span are proposals; and this artefact is **one Linux host** — CI runs
the tests, not this harness, so macOS has no acceptance row.

## The fresh-install journey, measured (2026-11-18)

`bench/journey.py` → `bench/results/2026-11-18-journey.json`: install from a **wheel** into a **new venv** (no
`PYTHONPATH`, no editable install), attach a real `openai` 3.51.0 app under `blackbox run`, capture, `ls`, `verify`,
`diagnose`, `view`, then a repair proposal (approval token → apply → `verify --test`) and a second run afterwards. The
provider is a **loopback origin** standing in for OpenAI — stated in the artefact — and the app's mistake is a model name
the origin refuses with a 404. Every row below is a measured exit code, not an intention:

| step | result |
|---|---|
| install from the wheel in a fresh venv | works (`blackbox-recorder`, `openai`, `httpx`, `anthropic` versions recorded) |
| attach + capture under `blackbox run` | works: the child reached the origin through the launcher, crashed on the 404, exit 1, two capsules |
| `ls` / `verify` | works: both capsules listed, `verify` exit 0, `capture_vantage` printed as declared-not-checkable |
| `diagnose` | works, and now says **the failure the user had**: "the provider answered HTTP 404 for the call at node 16385 (model gpt-4o-mni)" on the proxy's capsule and "the step 'support-agent' ended with an error" on the app's (defect 79) |
| `view` | works: 57 KB single page, no request of any kind (the page is checked for `http://` in the journey *and* by the viewer suite) |
| repair preview → apply → verify | works: preview printed a token, apply wrote the file and saved a backup, `verify --test` reran the app and exited 0 |
| a second run after the fix | works: exit 0, both calls succeed, and its diagnosis names no 404 |

What this run **found and fixed** — three defects no unit test was going to produce: 77 (the proxy marked every call
`partial`, so `diagnose` reported a cut stream for a complete 404), 78 (the seam judged a plain JSON body against the SSE
terminator table, so a non-streaming call looked cut), and 79 (the report did not mention a 404 status or a node that
closed with an error). What it does **not** show: `repair verify --test` runs in *your* environment, not the launcher's,
so a test that needs the variables `blackbox run` exports has to be given them (the journey sets `OPENAI_BASE_URL` for
that step and says so); the repair is a human-written proposal, not a model's; and no real provider key is used.

## Measurements

Hardware for every ext4 row: Intel Xeon @ 2.60 GHz, 2 cores, 4 034 452 KiB RAM · Linux 6.1.158+ x86_64 (glibc
2.36) · CPython 3.11.2 · ext4 (tmpfs is a labelled second column and is never gate evidence, D-010).
One line per §15.1 row: fs · N · warm-up · p50 / p95 / p99 · raw JSON.

- 1 KiB `content=hash`, group commit 16 KiB, CHAIN on · ext4 · N 200 000 · warm-up 20 000 · p50 11.057 · p95 20.53 · p99 51.662 µs · 73 338 ev/s · `bench/results/2026-11-14-writer-throughput.json` (`results.ext4.rows[gate=true]`)
- same row, keeper thread publishing · ext4 · N 200 000 · warm-up 20 000 · p50 11.044 · p95 20.6 · p99 54.527 µs · 73 471 ev/s · `bench/results/2026-11-14-writer-throughput.json`
- kernel mode (`sync_ms=250`, 1 MiB) · ext4 · N 200 000 · p50 11.278 · p99 89.903 µs · 60 368 ev/s · sync stall max 2 866 µs over 28 syncs · `bench/results/2026-11-14-writer-throughput.json` (reported, not a gate row)
- kernel mode, sync-dense (64 KiB) · ext4 · p50 11.387 · p99 81.232 µs · stall max 11 875 µs over 453 syncs · `bench/results/2026-11-14-writer-throughput.json` (reported)
- group-commit window · ext4 (Linux only; the Windows bound is stated, not measured — `docs/compatibility.md`) · 1 ev/s 4.603 ms · 1 000 ev/s 4.999 ms · unbounded 3.572 ms (120.99 ev/commit) · `bench/results/2026-11-14-writer-throughput.json`
- recovery, 22 312-frame tape from 10 000 events · ext4 · n 30 rounds · min 231.59 · p50 236.83 · p95 248.36 · p99 250.78 ms → **106.1 ms/10 k** (p99 = max because n < 100) · `bench/results/2026-11-15-recovery.json` (`results.ext4.recovery`)
- disk amplification, 16 KiB group commit · ext4 · N 10 000 events, 2 560 000 payload bytes · 1.231× (3 150 352 / 2 560 000) · `bench/results/2026-11-15-recovery.json` (`results.ext4.amplification_by_batch[batch_bytes=16384]`)
- disk amplification, publish-per-event · ext4 · 1.843× (4 718 976 / 2 560 000) · `bench/results/2026-11-15-recovery.json` (`results.ext4.amplification`) — reported, above the 1.25× row, below the 2× hard fail
- disk amplification, 4 KiB batch · ext4 · 1.263× (3 234 416 / 2 560 000) · `bench/results/2026-11-15-recovery.json` (`amplification_by_batch[batch_bytes=4096]`) — reported
- disk amplification, 64 KiB batch · ext4 · 1.222× (3 127 816 / 2 560 000) · `bench/results/2026-11-15-recovery.json` (`amplification_by_batch[batch_bytes=65536]`) — reported
- RSS above baseline · ext4 · 10 000 events, `head_off` 4 723 072 (28.13 %) · anon +1.27 MiB, file +0.29 MiB → 1.56 MiB · `bench/results/2026-11-15-recovery.json` (`rss_write_only_process.ext4`)
- 64 KiB `content=full`, keeper on · ext4 · N 20 000 · warm-up 2 000 · p50 226.864 · p95 325.83 · p99 461.281 µs · 4 356.4 ev/s · 0 tape syncs, 0 blob syncs on the calling thread · `bench/results/2026-11-15-payload-blob.json` (`results.ext4.rows[gate=true]`)
- same row, keeper off · ext4 · p50 167.899 · p99 304.614 µs · `bench/results/2026-11-15-payload-blob.json` (reported)
- same row, `kernel` mode · ext4 · p50 194.982 · p99 477.095 µs · 1 024 blob syncs · `bench/results/2026-11-15-payload-blob.json` (reported)
- watchdog rebind of a dead proxy listener · no filesystem involved · two runs, idle n=60 each: p50 12.39 / 11.58 · p95 20.76 / 20.53 · max **21.86** / 21.38 ms (detection + rebind; rebind alone p50 0.82 / 0.80, max 2.90 ms) · loaded n=60 each (one busy process per CPU): p50 11.52 / 11.77 · max 21.47 / 20.59 ms · 0 of 240 over the 100 ms budget, 0 port moves · 2 vCPU x86_64 Linux 6.1.158 container, phase randomised per trial, every sample kept · `bench/results/2026-11-16-rebind-a.json`, `bench/results/2026-11-16-rebind-b.json`
- request-level watchdog rebind, `tools/rebind_probe.py` on the child's port · no filesystem involved · 4 columns × 60 trials: p50 1.213 / 0.881 / 10.156 / 8.080 ms, p99 5.891 / 53.980 / 21.512 / 23.123 ms · worst observed error bound **53.98** ms, 0 of 240 samples over the 100 ms budget, 0 port moves, 0 not restored · 2 vCPU x86_64 Linux 6.1, `open_fds` 12–25 (the earlier 3.3 s samples were the probe exhausting the 1024-descriptor limit, defect 60) · `bench/results/2026-11-18-rebind-instrumented.json`
- `blackbox ingest` cold start (P3B-07's row, on the **wrong machine** and labelled so) · no filesystem gate · n 60 per column, console script, inbox emptied per sample · ingest p50 **33.015** / p95 **44.462** / p99 53.942 ms (MAD 1.685), bare interpreter p50 10.888, CLI floor p50 32.166 → the command adds 0.849 ms p50 · `bench/results/2026-11-19-ingest-cold-start.json` (`results.ingest`, `verdict.on_the_bench_box: false`)
- randomized parent-`SIGKILL` · ext4 · 500 effective trials, 10 cells × 50 · 0 violations, 0 `not_started` · fresh clone of `af908bc`, `tests/kill/kill_matrix.py`
- tmpfs labelled column for every row above · tmpfs · recovery 79.9 ms/10 k, amplification identical · `bench/results/2026-11-15-recovery.json` (`results.tmpfs`) — never gate evidence

## Deviations applied (DEVIATIONS.md) and held findings (F#)

The defect list (seed → invariant → cause → fix → test) lives in `docs/defects.md` — eighty-one entries, numbered to 81. Six are
from this stretch of work: defects 23 and 24 (a seam that installed, reported `installed`, and captured nothing because both real SDKs speak
`httpx2`; and a request frame that could say `model: null`), defect 25 (the Windows teardown fixture that probed
every object in the collector and turned `anthropic`'s `LazyProxy` into nine teardown errors), defect 26 (open: on
Windows `install()` accepted a data directory that is a file), and the two older capture-layer gaps (defect 20, the
untested async path, still open; defect 21, a wall-clock assertion that reddened `43693106857` at `714554c`, fixed at
`771f5ea`).
Fourteen deviations, all dated and argued in `docs/DEVIATIONS.md`: DV-005 (one checksum, CRC-32), DV-006/007 (S17
degrades instead of raising, including the first segment), DV-008 (P2-03/P2-04 written with an honest
`capabilities()`), DV-009/010 (ADR-005 store change; the head is no longer mapped), DV-011 (every OS call through
`platform.current()`), DV-012 (`O_BINARY` on every descriptor). Findings F1–F13 are decided, not pending: F4 became
DV-001 (seal hashes over the uncompressed body), F13 lives in P2-11 (launcher stall dump), and the remainder are
recorded in `docs/errors.md` with the defect that motivated each. No finding is "awaiting a word".

## Decisions taken with plan defaults (DECISIONS.md)

Thirty-one decisions, D-001 … D-031. The ones taken since the last checkpoint, each with the plan default and the
reason beside it: **D-026…D-028** (capture scope is an explicit host allowlist; fail-open is per callback; a stream
closed early is `partial` and chunk counts are only compared with the reads the application made), **D-029** (redaction
before persistence, on by default, holding only what is unfinished), **D-030** (the content mode is honoured and its
default is Appendix A's `hash`), **D-031** (the pseudonym is an HMAC under a per-machine key, and a missing key is
recorded as unkeyed). The two knobs of `P1-02b` were the
last thing switched off in `mypy`; nothing is switched off now. Format-freeze decisions carry their `DEVIATIONS.md`
row in the same commit.

## What is next

0a. **What the 2026-11-16 additions still need, stated so a machine's absence is not mistaken for a pass.**
    * **Windows, a real machine or CI with a GUI**: the `icacls` arm of `tests/test_permissions.py` has never run; the
      unsigned installer's install → launch → attach → record → view → diagnose → uninstall journey is still unrun; the
      desktop's own flows on Windows remain unmeasured (the window is the only untested part of the product that a user
      touches first). **Nothing in this turn claims any of that.**
    * **Linux with a display**: this sandbox has none, so `blackbox desktop --selftest` still reports `gtk_ready:false`
      and exits 3. The window's flows are covered over HTTP by `tests/test_ui_flows.py`, which is not the same thing.
    * **CI**: every file named in the six sections above is a builder run on this machine. The workflows have not run
      them (`1628d5a` and the local commits after it are unpolled; the `go-reader` job is new and has never been
      observed green). A reviewer should read the cells as "written and measured here", never as "measured on the
      matrix".
    * **People**: the reviewer's signatures (`docs/gates/`), the three-person onboarding measurement, and the owner's
      decisions — merge, tag, publish, licence — are outside this branch by direction.

0. **The reviewer's three P0s are fixed and reproduced first** (`ecf8b88`): a receiver exception no longer reaches
   the application through any callback (defect 27 — reproduced at `ee210ae` with the reviewer's script, which printed
   `ESCAPED RuntimeError` before the fix and `SURVIVED bytes=849` after it), capture is scoped to an explicit host
   allowlist with skips counted (D-026), and a data directory that is a file is refused by name on every OS
   (defect 26). `47ec45a` then verified both client modules directly for streaming, truncation, async and
   cancellation, and found two things that were only true by assumption (D-028).
0b. **`main` has moved, so the nightly clock has started**: PRs #2, #3 and #4 are merged (`e8c3f6f`, `dd92c7b`, and
   the proxy-thread one) — the owner asked for the merges directly, which is what this item was waiting for. The
   scheduled workflow's one historical run failed because it named files that do not exist (defect 28, fixed here);
   the next scheduled run is the first that can succeed, and `docs/defects.md` says how to read it if it does not.
0c. **`d43b208` is green on all ten cells** (`43706220024`) — the fix for defect 31, which reddened seven of ten
   cells at `705fc59` and kept `9fe6d72` red with it. Before that `4f42397` (`43705007628`), `4ce0f7c`
   (`43704041733`), `07e5c29` (`43703761892`) and `ce1439b` (`43701652852`) were ten of ten. `STATE.last_green_commit`
   is `d43b208`; the red pair keep their numbers and their verdicts (C40).
1. **G-Promise is signed** (`docs/gates/G-Promise.md`, verdict pass, self-signed — see §Labelled) and `STATE.json`
   carries `gates["G-Promise"] = "pass"`.
2. **G-Capture's mechanism rows are all green and the gate needs a signature that is not mine.** Green: the day-14
   rule for **both** SDKs (unmodified `openai` 3.51.0 and `anthropic` 2.8.0 scripts killed mid-stream, recovered with
   counts that never exceed the mock provider's own tally, taking the same seam); P3-14 (a 100-call agent identical
   with and without the recorder, under three real breakages); P3-11's redaction (60-shape corpus at every split
   offset, three modes, default policy, fail-closed, keyed pseudonyms — and, since `tests/test_redaction_every_sink.py`,
   the 60-secret corpus asserted absent from **every artefact the product renders from a capsule**, not only the tape);
   and the five §15.1 capture rows, measured. Each of those rows is now also quoted in `docs/gates/G-Capture.md` beside
   the file or JSON it comes from; that file's verdicts are all still pending, because a builder may add evidence and
   never a tick.
   What is left of P3-11 is **mask offsets** (DV-014), which belong with the viewer that would use them, and the gate
   itself needs a reviewer's counter-signature on `docs/gates/G-Capture.md`.

   The L3 attach work is now done on both halves: the proxy's §15.1 rows are measured (P3B-08: added latency p50 0.165 / p99 0.471 ms, first-byte 0.184 ms, 200 concurrent streams, RSS 0.0 MiB growth), the kill and disconnect classes and the curl/Node/SDK client matrix are tests, both hook adapters install byte-identically (P3B-05, P3B-06/D-041), the inbox reaches a tape of its own vantage (P3B-07/D-040), and `docs/attach.md` labels every surface with a version and a date or the word **unverified** (P3B-09/D-042). **Next ticket: P3B-03's remaining half** — the Python auto-import `.pth` and the `--no-sdk` / `--force-proxy` / `--no-hooks` flags its done-when names — then P3B-07's bench-box cold-start row, then the viewer and the diagnosis workflow a user would actually judge this by.
3. **Defect 26 is open and cannot be closed here**: on Windows, `install(data_dir=<a file>)` reported `enabled: True`
   and let 100 calls run against a tape directory that cannot exist. Three questions are written down in
   `docs/defects.md`; answering them needs a Windows host, which this sandbox does not have.
4. **The capture seams each cover both client modules** (`httpx` and `httpx2`) — the published `openai` and
   `anthropic` packages are built on `httpx2`, which is why seam A reported `installed` and captured nothing until
   the day-14 test found it (defect 23, D-025). No seam, and no unit test of a seam, could have found it from the
   inside.
5. **L3 is under way from `50d6b37`**: the proxy's core (`proxy/server.py`), its routes (`proxy/routes.py`) and its
   supervised thread plus variable plan (`proxy/supervisor.py`) are built and tested. Still to come: `launch()`
   starting that thread (P3B-03's second half), the header links (P3B-04), the Claude Code / Codex hooks and the
   inbox (P3B-05–07), the proxy bench and kill classes (P3B-08), and `docs/attach.md` (P3B-09).
6. The independent reviewer's loop is the gate loop: a counter-signature at every gate, with the reproduce block in
   `docs/gates/` and `docs/reviews.md` as the ledger. It found C40 on its first pass and defect 25 on this one, which
   is the argument for it.
7. The 2 000-trial kill run starts in the background during L5–L8; it is not needed for L2/L3.
