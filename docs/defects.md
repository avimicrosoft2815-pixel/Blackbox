# Defects found and fixed

The rule (C9): a defect is written down as **seed → invariant → cause → fix → test**. A defect without a test is a
story; a test without a defect is a decoration. This list moved here from `docs/STATUS.md` when STATUS took the
plan's §10 shape, and it is append-only.

1. **seed 352** → adoption demanded a `SEG_END` → roll can legally omit it → seal exactly `[4096, head_off)` → `test_adoption_does_not_require_a_seg_end_frame`.
2. **seed 388** → a segment created in place left a half-written header → tmp + header + `fdatasync` + `replace` + `fsync(dir)` → `test_segment_creation_publishes_the_header_atomically`.
3. **seeds 209/307/386** → an empty tape was called corruption → `test_scan_of_an_empty_tape_is_not_corruption`.
4. **seed 396** → "holds frames" judged by file size → decode a frame → `test_a_preallocated_segment_without_frames_is_not_treated_as_full`.
5. **seed 120** → kill during head creation left `segments exist, head unreadable` → atomic head creation + `repair.rebuild_head` → three tests.
6. **bench-found** → keeper and app thread could both `roll()` → one re-entrant lock → `test_keeper_and_app_thread_never_roll_twice`.
7. **fuzz** → `struct.error` escaped the CBOR decoder → bounds-check every field → `test_truncated_headers_raise_cbor_error_not_struct_error`.
8. **S17** → `roll()` raised `E132` into the app → reserve before closing, degrade in place → `tests/test_enospc.py`.
9. **seed 120/S17-at-open** → `open()` raised when the first segment could not be allocated → sink mode → `test_enospc_at_the_first_segment` (both modes).
10. **bench-found (C16)** → the gate number excluded its own CBOR encode, and `statistics.quantiles(method="exclusive")` produced a p90 *below* the p50 → clock starts at the dict, all percentiles from one sorted array → the row itself is the regression test (`verdict` is computed from those numbers).
11. **Windows (C29)** → every `os.open` gave a text-mode CRT descriptor, so `0x0A` expanded to `0x0D 0x0A`, `0x0D 0x0A` collapsed back, and `0x1A` ended reads → `platform.open_fd` ORs `O_BINARY`, the dispatch test forbids bare `os.open`, and `test_positional_io_is_byte_exact_for_all_256_values` writes all 256 values and demands byte identity.
12. **bench-found (C17)** → the `write()` arm issued one `pwrite` per event and allocated a slot object per event → batch buffer + plain ints → `profile_arms()` asserts the difference stays under 200 ns/event.
13. **`P1-09b` random retain/kill, seed 0 step 38** → seqs 1–13 vanished with no `GAP` anywhere → a later retention pass had pruned the segment that held the *first* `GAP` → tombstone segments are pinned, and a merged `GAP` replaces them in the same pass → `test_gap_chain_link_matches_the_writer_chain` (two passes), `test_i1_and_i9_hold_under_random_retain_and_kill`.
14. **`P1-09b` control case** → a whole leading segment deleted behind retention's back was invisible: the scan simply *began* at seq 30, so no hole was reported → the hole range now starts at `min(first recovered, first declared, tail_seq + 1)` → `test_a_retention_hole_is_declared_not_silently_missing`.
15. **`P1-09b`** → the `GAP` range stopped one seq short of the deleted segment, because `SEG_END` is a committed frame that dies with its segment → the range includes it → `test_gap_is_committed_and_published_before_the_unlink` (asserts every declared `to` is inside the committed range at unlink time).
16. **`P1-09b`** → `codec.cbor.decode` returns `(obj, pos)`, so the scanner's new `isinstance(body, dict)` check silently rejected every `GAP` → `decode_exact` plus `ScanResult.gaps_unreadable`, so an undecodable declaration is reported rather than swallowed.

17. **C40 — the gate commit's own CI run was 9/10 and three documents called it green** → CI for `af908bc` (run
    `43678767989`) failed one cell, `py3.13 / windows-latest`, on
    `test_low_rate_event_is_published_within_the_window` (`assert 0 >= 1`) → the group-commit window is measured from
    the **last publish**, and `flush()` with nothing pending is a no-op, so the test's "start the window here" line
    set nothing; on a cold Windows runner more than `batch_ms` (200 ms) can pass between `open()` and `emit()`, the
    window is already expired, `emit` publishes inline (correct), the keeper publishes nothing, and the assertion
    that the *keeper* did it fails → the test now publishes first, so the reference point is its own baseline on
    every OS, and `test_a_window_older_than_the_bound_publishes_inline` states the expired-window behaviour instead
    of leaving it implicit. 30 consecutive local runs green. The documents that claimed green are corrected in the
    same commit: the gate file is re-signed at a commit whose run is 10/10, `STATE.last_green_commit` moves to it,
    and the ≤ 5 ms window is now stated per OS in `docs/compatibility.md` — on Windows the keeper wakes on the
    default 15.6 ms timer, so a keeper-only drain can be one tick late; the bound holds for *arriving* events
    because `emit` publishes inline when the window has expired. The Windows window is **not measured** (no runner
    numbers), and saying so is the point.
18. **C40/C37 — the recovery row said `52.1 ms/10 k` under a sentence that named the wrong denominator** →
    the row is milliseconds to scan a tape divided by the frames on that tape → STATUS read *"p50 115.46 ms for
    10 000 frames of 22 144 on the tape = 52.1 ms/10k"*, which names both numbers and says neither clearly:
    115.46 ms is the scan of **22 144** frames, produced by 10 000 events. The arithmetic that was published
    (115.46 / 22 144 × 10 000 = 52.1) was right; the sentence around it was not, and the number moved anyway —
    n was 7 rounds, and C37 found two scanner regressions (`segments.logical_bytes` copied every raw segment;
    `_segment_has_frames` read whole segments to look at one frame) that had pushed the row to 133 ms/10 k. Fixed:
    both copies removed, the row re-measured at **106.1 ms/10 k** over 22 312 frames with n 30, `per_10k_frames_ms`
    computed from `frames_on_tape` in both columns, and the C39 wording names frames and events separately.
    Checked against the owner's hypothesis that the bench *timed* `requested_frames` and divided by
    `frames_on_tape`: that would have printed 115.46, not 52.1, so the defect was the sentence and the sample size,
    not the division. `tests/test_bench_results.py` keeps every percentile inside its own sample; the row sits at
    88 % of its 120 ms budget, so any change to `scanner.py` or `discover.py` re-measures it.

19. **C40 follow-up — three Windows cells failed on a POSIX-only test, in two consecutive runs** → runs
    `43689004423` at `15cb55c` and `43689012017` at `6d52088` (the docs-only commit carried the same broken test),
    both 7/10:
    `ValueError: cannot find context for 'fork'` in `test_a_forked_process_records_into_its_own_tape` → the test
    hard-coded a start method that does not exist on Windows, and its child body was a closure, which `spawn`
    cannot pickle → the test takes `fork` where it exists and `spawn` where it does not, and the child body is a
    module-level function. The recorder was never wrong. A POSIX-only assumption in a test is invisible on the
    builder's machine and blocks the gate exactly on the cells that matter — and `6d52088` shows why a docs-only
    commit still has to be read for its CI verdict: it inherited the failure and would have been recorded as green
    by anyone who trusted "it only touched markdown".

20. **found by writing `SUPPORTED.md`, closed by `tests/test_seam_async.py`** → both seams implemented the async path (`AsyncClient` hooks, the async
    tee) but **no test exercises it**: no async HTTP harness is installed here, so an async client is a claim, not
    evidence → recorded in `SUPPORTED.md` as a gap in that word rather than left to read as verified → the fix is a
    test, so this entry stayed open until `tests/test_seam_async.py` existed. It now exists (4 tests, `asyncio.run`,
    stdlib) and it found **two real faults** rather than confirming an assumption: `AsyncClient` awaits its event
    hooks, so the sync hook seam A installed raises `TypeError` on the first async call; and async clients build an
    `AsyncHTTPTransport`, so seam B's class patch covered `HTTPTransport` and missed them entirely. Both fixed in the
    same commit: async hook wrappers, both transport classes patched, and `wrap_stream` choosing the tee from the
    stream. The lesson is the one this list keeps teaching — "implemented but untested" was hiding a broken path, not
    a neutral gap.

21. **CI runs `43693106857` at `714554c`, `43693172961` at `1112881` and `43693216632` at `0f81445`** → a test must fail for a reason that is wrong, not because a machine was
    busy → `test_inter_chunk_delay_is_real_time` asserted that two 40 ms gaps take **>= 80 ms**; the `py3.11 /
    windows-latest` cell measured **79 ms** and the suite went red. The fixture was right: the delay was real. The
    assertion encoded "wall clock never rounds down", which no scheduler promises → the test now measures the same
    request with and without the knob and asks for **60 % of the nominal gap**, which still proves the knob works and
    survives a millisecond of jitter → `tests/test_mock_provider.py::test_inter_chunk_delay_is_real_time`. It fired
    three times in a row (79, 78, 78 ms) and *not* on `99a5164`, the run between two of them — the same tree with the
    same test passed, which is what a flaky test looks like from the outside and why a re-run is not a fix: the
    honest reading of `99a5164 · success` is that the run was lucky, not that the tree changed. This is
    the third timing test of mine to be wrong in the same direction (C40 was the second); the rule that comes out of
    it is in the test's own docstring: assert an effect, not a schedule.

22. **`5e7feb8` was pushed red, by me, in this session** → a green gate has to be an exit code, not the last lines of
    output → `PYTHONPATH=src pytest tests -q 2>&1 | tail -2 && git commit …` reports **`tail`'s** exit status, so the
    `&&` ran on a failing suite and `5e7feb8` went to trunk with `test_status_doc` failing (the suite grew by two
    tests and STATUS still said 397). Nothing was wrong with the code; the process was, and every previous commit in
    this session had the same shape and was only green because the suite happened to pass → the count is now 399,
    `tests/test_status_doc.py` is the test that caught it, and the rule is `set -o pipefail` before any command whose
    exit code is used to decide a commit — or, better, no pipe at all.

23. **found by the day-14 child, in `d075a32`'s seams** → a seam that installs must also *capture*, and the
    preflight must not be able to report success without a capture → the published `openai` 3.51.0 and `anthropic`
    2.8.0 build on **`httpx2`**, not `httpx`. Seams A and B patched `httpx.Client`/`HTTPTransport`, so for both real
    SDKs they installed, reported `installed` in `capture_seam`, and recorded **nothing** — a capsule that looks
    recorded and is empty. Every unit test was green because every unit test used `httpx`. → seams patch every
    importable client module (`http_clients.available()`), the handle lists the modules it covered,
    `tee.wrap_stream` takes its base class from the stream's own module, and the day-14 test is the guard:
    `tests/test_day14_kill.py`. This is the argument for the plan's insistence on a real SDK against a real socket —
    the seam suite could not have caught it.
24. **found while writing the day-14 test** → a sink writes the request frame the moment a call opens, so a seam that
    filled `call.model` *after* `open_call` left durable frames saying `model: null` for calls that named their model
    → `Receiver.open_call` now takes `model`/`stream`/`partial` as hints, seams pass them at open time, and the
    ordering is asserted by `tests/test_day14_kill.py::test_the_capsule_says_which_seam_captured_it` reading
    `model` back off the tape.
25. **found by reading the CI annotations, `c7919f9` → `d075a32`, Windows only, 3 cells red** → a cleanup fixture must
    identify its targets, not interrogate every object the collector knows → `tests/conftest.py`'s Windows teardown
    called `getattr(obj, "_head", None)` on **every** object in `gc.get_objects()`. `anthropic` keeps its optional
    dependencies behind `LazyProxy`, whose `__getattr__` calls `__load__()` and raises `MissingDependencyError`; so
    the fixture raised during teardown and pytest blamed whichever test was running — nine property tests picked up
    `ERROR … anthropic.lib._extras._common.MissingDependencyError`. It needed both conditions: Windows (the block
    returns early on POSIX) and `anthropic` in the environment (added to CI by `capture-sdks` in the same commit).
    → `tests/_hygiene.py` selects by **exact type** (`type(obj) is TapeWriter`), never by attribute, and
    `tests/test_gc_hygiene.py` proves it against an object that raises on `__getattr__` and `__class__` plus the
    SDK's real proxy. The annotations came from `tools/ci_summary.py`'s `::error::` lines (C27) — job logs are
    redirected to a storage host this sandbox cannot reach, the check-run annotations are readable through the API.
26. **open, found on the Windows cells at `67b5ae5`** → `install(data_dir=<a path that is a file>)` reported
    `enabled: True` on Windows, `enabled: False` with `NotADirectoryError` on Linux → 100 calls then ran with a
    writer whose tape directory cannot exist, and the suite recorded `closed: 18` against `runs_started: 1`. Three
    questions, none answered yet: what `os.makedirs` actually did there, whether any frame reached a directory,
    and why 18 of 100 calls closed. Until it is answered the fail-open test claims this case on POSIX only and
    **skips on Windows with this number in the reason**, and the strict all-OS breakage the suite uses is
    `segment_bytes=1`. This is the same failure family as defect 23 — a recorder that looks like it is recording —
    and it is why the writer's own `degraded_reason` path (S17) exists. It cannot be reproduced from this sandbox:
    there is no Windows host here and the CI job log is unreachable, so the check-run annotation is the evidence.
27. **reported by the independent reviewer at `ee210ae`, reproduced here before any change** → a recorder failure must
    not reach the application through *any* callback → `tee.py` called `self.recorder.receive(...)` bare, so a
    receiver exception during a real `httpx.SyncByteStream` came out of the application's `for chunk in
    response.iter_bytes()` and killed the host. Seam A's hook had a `try` around the open-call path — which is why the
    existing fail-open test passed — and seam B's `open_call` had none. Reproduced in a clean worktree at `ee210ae`
    with the reviewer's script: `ESCAPED RuntimeError: recorder blew up on a chunk`; after the fix, the same script
    prints `SURVIVED bytes=849` and the receiver's failure is counted. → `cap_sdk/failopen.py` guards all three
    callbacks in one wrapper, the seams and the tee both hand their receiver through it (idempotent), and
    `tests/test_capture_failopen.py` (18) covers `open_call`/`receive`/`close_call` × sync/async × seams A and B, plus
    cancellation, a provider 429 reaching the application unchanged, the bounded failure sample and the rule that the
    message is never recorded (D-027).
28. **found by auditing the nightly workflow on `main` (owner said a merge had happened; it had not)** → a scheduled
    workflow that names files which do not exist fails every night and looks like a code problem → `nightly.yml`
    ran `python tests/kill/randomized_kills.py --trials 2000 --out …` and `pytest tests/test_cbor_fuzz.py`; neither
    path exists (the driver is `tests/kill/kill_matrix.py` with `--json`, the fuzzer is `bench/fuzz_cbor.py` and it
    is a script, not a test module). The one scheduled run this repository has ever had — `43675454143` on `main` at
    `31241d6`, 2026-11-15 04:08Z — failed both jobs with exit 2 and exit 4, which are Python's
    "no such file" and pytest's "usage error". → both steps now call the real drivers (`--trials 2000 --json`,
    `--iterations 100000 --out`), verified locally with a 3-trial and a 500-buffer smoke run. **The P2-09 clock still
    has not started**: the schedule only runs on the default branch, `main` has not moved past the branch base
    `31241d6`, and the fix therefore takes effect only when the owner merges.
- **defect 29 — the redactor held a fixed tail, and its email pattern was quadratic.** Two faults in one new module, both
  found by running the tests that claim the property rather than by reading it. (1) The first release rule held a fixed
  4 KiB tail; on a five-chunk stream that is the whole stream, so `tests/test_day14_kill.py` — a `SIGKILL` of an
  unmodified `openai` script — recovered `MODEL_REQUEST` and no chunks, i.e. the day-14 rule broken by a privacy
  feature. Fixed by holding only what is *unfinished* (D-029), and the test that caught it is the one that was already
  there. (2) The email pattern `[A-Za-z0-9._%+-]+@…` is unanchored, so on a 4 KiB buffer with no `@` at all it re-scans
  the buffer from every position: **41 ms per read**, more than the model call it was recording. Found by the
  megabyte-cost test, fixed with a literal-needle pre-check per pattern (0.33 ms per read, 1.1 ms per 58 KiB). Also in
  this pass: three of the 60 corpus fixtures were malformed (`eyJ…` third segment of seven characters), so the jwt
  pattern correctly refused them and the failure looked like a redactor miss — the fixture was the bug, and the corpus
  now has a test that every entry is matched by its own kind.
- **defect 30 — the configured content mode was ignored: the default said `hash` and the tape got the body.** Found
  while preparing the §15.1 rows for `content=off` and `content=hash`: `constants.DEFAULT_CONTENT` is `hash` and
  `config` validates the value, but `TapeSink` had no `content` parameter at all, so every capture wrote the model's
  text whatever the configuration said. A user reading `content=hash` would have believed the prompt was not on the
  disk. Fixed in the same commit as the measurements it blocked (D-030): the sink honours `full` / `hash` / `off`, the
  request and summary frames declare the mode, and `recovery` reads the byte count from a payload header so an
  interrupted call still reports how much arrived. Three suites depend on the body being on the tape and now ask for
  `content="full"` explicitly rather than relying on a default that was not what it said.
- **defect 32 — `Expect: 100-continue` answered after the body was read.** The proxy read the request body first and
  wrote `HTTP/1.1 100 Continue` afterwards. A client that honours the header sends nothing until it sees the interim
  response, so the two waited on each other: the test that speaks raw bytes to the proxy hung on the first `recv`.
  Fixed by moving the interim response before `_read_body`, which is the only order that cannot deadlock;
  `test_expect_100_continue_is_answered_before_the_body_is_read` is the test that would have caught it, written before
  the fix because the plan names `Expect: 100-continue` in P3B-01's acceptance.
- **defect 33 — the pool retried a request that may already have been transmitted.** Found by a reviewer as a P0 on
  D-032a, and it is the worst class of bug in this file: not a wrong number but a wrong *effect*. The stale-connection
  path caught a dead upstream connection after the request had been written and answered it by **replaying the
  request** on a fresh connection. For a `GET` that is harmless; for a `POST /v1/chat/completions` it is a second
  charge, and for anything with a side effect on the far end it is a duplicate that no local record explains. Cause: the
  code could not distinguish "the pooled connection was dead before we wrote" from "the request went out and the reply
  never came", and treated both as retryable. Fix: probe pooled connections *before* writing (`at_eof`, `is_closing`,
  `exception`) and discard them for free; after writing, retry only `GET`/`HEAD`/`OPTIONS`, and answer everything else
  `502 upstream-closed-uncertain` with a body that says the request may have been applied, counted in `stats.uncertain`.
  Test: the upstream records a POST and closes without responding —
  `test_a_post_that_may_have_been_transmitted_is_never_sent_twice` asserts one POST arrived and a 502 came back; a
  mutation check (POST made retryable again) shows the upstream receiving two, which is the bug reproduced.
  **Follow-up, same reviewer:** the fix still called `GET`/`HEAD`/`OPTIONS` safe and retried them by itself, which replaced one
  unstated assumption with another — HTTP only *intends* those methods to be safe, and an upstream that bills, warms or logs on
  a `GET` is common enough to matter. Retries after transmission are now **off by default for every method** and reachable only
  with `--proxy-retry-transmitted`, which the caller uses after checking their own upstreams; even then `POST`/`PUT`/`PATCH`/
  `DELETE` are never resent. The bench that made this class hard to see had its own version of the same bug: it measured the
  proxy from inside the proxy's interpreter, so its first-byte row (0.44–0.52 ms, PASS/FAIL alternating) was a GIL hand-off
  rather than the proxy. Origin, proxy and client now each run in their own process, and the row reads 0.184 ms. D-032c.
- **defect 34 — a client that hung up was recorded as an upstream truncation.** Found while implementing §14.4a's three
  disconnect classes, which exist *because* the proxy is the only component that sees both ends of a call. It saw both and
  then put them in one bucket: `_relay_body` was wrapped in a single `except (ConnectionError, asyncio.IncompleteReadError)`
  that stamped the tape `upstream-truncated`, whatever side had actually gone. A client that died mid-stream — the case
  where the provider keeps billing for tokens nobody received — read back as the provider's failure, so the recording
  blamed the wrong party in exactly the situation a user opens it to diagnose. Fix: one helper for client writes
  (`_to_client`) and one for drains (`_flush`), raising `_ClientGone` with a per-side byte tally; upstream failures keep
  their own path; each side's class is written as `INTERRUPTED_CLIENT` / `INTERRUPTED_UPSTREAM` with both counts, and
  `stats.client_gone` / `stats.upstream_gone` count them separately (one `upstream_errors` number is what hid this).
  `INTERRUPTED_PROXY` gets no frame at all — the writer is dead — so it is derived from an open call plus a stale lease.
  Tests: `tests/proxy/test_disconnect_classes.py` (real sockets both ways, real tape, class read back off it) and
  `tests/proxy/test_proxy_death.py` (S16: `SIGKILL` a real proxy mid-stream, recover, `INTERRUPTED_PROXY`).

- **defect 35 — a test that hung on one interpreter, and a CI that could not say which one.** Not a product bug, and
  found exactly the way a product bug is found: a claim was checked on one platform and asserted on three. P3B-04's
  first half landed with `tests/test_headers.py`'s proxy test running the `httpx` client, the `ProxyServer` and the echo
  origin on **one** `asyncio.run()` loop — accept, serve and read interleaved on a single scheduler, which is a shape
  that *usually* works and is never guaranteed. On CPython 3.11 and 3.13 the cells were green; on 3.12 all three failed,
  and the failure was not an assertion but **the absence of one**: the step hung until GitHub's `timeout-minutes`
  expired, and the only annotation the run produced was `The action 'Unit + property tests' has timed out after 15
  minutes` (`check-runs/132853301928`). Everything a reader needed — which test, which line, what it was waiting on —
  was inside a process that had been killed by the orchestrator. Two defects, then: the loop, and the silence.
  *The loop.* On 3.12 the loop parks in `select()` with nothing runnable: every coroutine is waiting on a server whose
  accept callback can only run once the loop comes back, so it never does. The rewrite is the shape
  `tests/proxy/` already used — the proxy on its own loop in a daemon thread, woken by a `threading.Event`; the origin a
  `ThreadingHTTPServer`; the client synchronous `httpx.Client(timeout=15.0)`. Three threads, no shared scheduler to
  starve, and the test states the trap in its own docstring so the next one is not written the same way.
  *The silence.* Fixed in four parts, because each layer lost the information for its own reason (D-037): an autouse
  `conftest` watchdog (`BLACKBOX_TEST_TIMEOUT`, default 120 s) that writes a `FAILED <nodeid> — ran longer than N s at
  <file>:<line> in <function>` line and exits the process; a **file** in the tempdir as the durable channel, because
  pytest's capture is an `fd`-level `dup2` and `os._exit` skips its flush, and because GitHub keeps only ~10 annotations
  per step so a `faulthandler` dump of every thread filled them with `pluggy` frames; `tools/ci_summary.py` printing one
  fixed-shape annotation (`exit N · out M lines · hung yes|no · <FAILED line> | …`) that is emitted even when the child
  said nothing, since the first version printed nothing at all in that case; and `-s` on the pytest invocation, because
  under capture the child's own output never reaches the log. The first run with all four in place named the test and
  the frame (`selectors.py:468 in select`) from a run that previously said only that it had timed out.
  `tests/test_headers.py` is still seven tests; the count did not change because nothing was deleted to make it pass.
- **defect 36 — a test compared a read count to an event count, and one cell out of ten disagreed.** `d6ab810`'s run
  (`43743518320`) was nine of ten: `py3.13 / ubuntu-latest` failed on
  `tests/test_seam_transport.py::test_a_truncated_stream_is_recorded_short_rather_than_completed` with *"the recorder
  invented or dropped a chunk — `2 == ModelCall(...).chunks`, `4 == len(...)`"*, and the ten cells before and after it
  were green. Nothing was invented and nothing was dropped: `call.chunks` counts the **reads the application made**, and
  on that machine two SSE events arrived in one read, so the body's four `data:` frames had been delivered in two
  chunks. The assertion had compared a property of the transport to a property of the payload, and no amount of care in
  the seam could make the two agree. It is the same shape as C40's window flake — an assertion that is true of the
  common scheduling and false of the honest one — and it was found the way this file has been catching them: the
  watchdog and the single annotation named the test and the two numbers in one line, from a run that would otherwise
  have said "exit 1". Fix: "short" is now measured against an **oracle** (the same request through an unseamed client,
  untruncated: the truncated body must be strictly smaller and must lack `[DONE]`), and the chunk count is compared to
  the reads that run's application actually made, which is the only relation the seam guarantees.
- **defect 37 — my own new test hard-coded a POSIX path join, and the Windows cells said so within the hour.** The
  inbox-defaults test asserted `inbox.default_dir(None) == "/tmp/bbx-data/inbox"` on every platform; `os.path.join`
  produces `…\inbox` on Windows, so all three Windows cells of run `43749014701` failed while the six POSIX cells were
  green — the mirror image of defect 35, and the same lesson from the other direction: *the platform is a variable, and
  a test that spells a separator has decided the answer in advance.* Found by the annotation, which had the two strings
  side by side (`'/tmp/bbx-data\\inbox' == '/tmp/bbx-data/inbox'`) and named the test, in a run whose log this sandbox
  cannot read. Fixed by comparing against `os.path.join("/tmp/bbx-data", "inbox")`; `6a8a742` is ten of ten (`43749402312`).

- **defect 38 — a byte-bound test that a clock could answer.** `c411a53`'s run (`43750531880`, a docs-only commit) came
  back nine of ten: `py3.12 / macos-latest` failed `tests/test_sink.py::test_the_byte_bound_is_respected` with *"16 KiB
  is under a 64 KiB bound"* — the assertion that nothing had been flushed before `close_call`. The sink coalesces on
  **two** triggers (§7.2): the byte bound *and* a 100 ms window. On that runner more than 100 ms passed between
  `open_call` and the fourth `receive`, so the window fired first, which is exactly what the sink is supposed to do —
  the test had asked a question about bytes and accepted an answer from a clock. The same family as defects 35 and 36,
  and the third flake the new annotation has caught in two days: the fix is to make the window unreachable
  (`coalesce_ms=60_000`) so the only trigger left is the one under test, and the docstring now says why the clock was
  removed instead of leaving the next reader to wonder. Re-run green at `6a8a742`+ (`43749402312`); the macOS cell is
  the evidence, since a fast local loop cannot reproduce a 100 ms gap.
- **defect 39 — the exported OpenAI base URL lost the `/v1`, so a real client asked the provider for a path it does not
  serve.** Found by P3B-09's matrix work: the row *"does this client honour the base URL `blackbox run` exports?"* was
  the first question in this repository that had to be answered by **running the client**, and the first client run put
  `POST /chat/completions` on the upstream instead of `/v1/chat/completions`.
  *Invariant*: `exported path + the path the client appends == the path the provider documents`. It is a two-column
  equality, and it was never written down.
  *Cause*: `ExportPlan.with_url()` rendered one flat shape, `http://127.0.0.1:<port>/<name>`, for all four variables.
  That is right for the Anthropic SDK, whose default base is `https://api.anthropic.com` and which appends
  `/v1/messages`. It is wrong for the OpenAI SDK, whose default base is `https://api.openai.com/v1` and which appends
  only `/chat/completions` — so the version segment has to be **in the value we hand it**. The plan had said so in
  §13 (`OPENAI_BASE_URL=http://127.0.0.1:8787/openai/v1`); the implementation disagreed with the plan and nothing
  compared them. Every proxy test that existed used a hand-written `/v1/chat/completions`, so the suite agreed with the
  bug: the tests had modelled a client that appends the version, and no real client does.
  *Fix*: `BASE_URL_SUFFIX` in `proxy/supervisor.py`, one entry today (`OPENAI_BASE_URL → /v1`), applied where the value
  is rendered. Adopted values are untouched — a variable the user set is still never rewritten.
  *Test*: `tests/test_attach_sdks.py::test_the_exported_base_plus_the_clients_own_path_is_the_documented_provider_path`
  compares export + appended against the documented path for openai, anthropic and gemini, using the **default upstream
  table** rather than a fixture; and the four client tests run the real SDKs, `curl` and `node` through the proxy so the
  appended path is a client's decision and not the test's. Both fail on the old rendering (openai: `/chat/completions`
  ≠ `/v1/chat/completions`). The `OLLAMA_HOST` and `GOOGLE_GEMINI_BASE_URL` rows are labelled **unverified** in
  `docs/attach.md` instead of being quietly assumed right — `OLLAMA_HOST` is a `host:port` without a scheme in Ollama's
  own documentation, which this build does not produce.
- **defect 40 — one wall-clock sample of a rebind, asserted on a shared runner, failed at 107.9 ms against 100.** Run
  `43767898299` (docs-only commit `19d831e`) came back nine of ten: `py3.11 / macos-latest` failed
  `tests/test_proxy_supervisor.py::test_the_watchdog_brings_a_dead_listener_back_without_being_asked` with
  `assert ([107.91783399997712] and 107.91783399997712 <= 100.0)`. The other nine cells, including the two other macOS
  cells, were green.
  *Invariant*: the watchdog restores the listener **on the port the child was told**, inside the budget, without being
  asked. None of those was violated: the listener came back on the same port, and the rebind was the watchdog's.
  *Cause*: the assertion took `max(proxy.rebinds) <= REBIND_BUDGET_MS` from a **single** sample, and `rebind()` is
  wall-clock time from entering the lock to the listener serving again — a quantity that includes whatever the shared
  macOS arm64 runner does to a thread that has just been woken. The budget is a promise about a machine we control; a
  one-sample wall clock on a runner is a measurement of the runner. Same family as defects 36, 37 and 38: an assertion
  true of the quiet case and false of the honest one.
  *Fix*: the test keeps the structural assertions (the watchdog is what restored service, and on the same port — now
  asserted explicitly with `len(rebinds) == 1` before anything else happens) and takes the budget from the **median of
  five** rebinds, printing all five samples in the failure message so the worst case stays visible instead of being
  averaged away. A genuinely slow rebind still fails, on every sample; one scheduling pause no longer does.
  *Test*: `test_the_watchdog_brings_a_dead_listener_back_without_being_asked`; green at `19d831e`+ locally and in the
  re-run, and the number that a reader should quote is still the recorded median, not a rounding of the budget.
- **defect 40, superseded 2026-11-16 — the median was a second measurement of the wrong quantity.** Closer reading of the
  sentence the row implements (§11.8: *"the listener is re-bound within 100 ms"*) says the subject is the listener coming
  back, so the quantity is an **outage** — detection (a tick) plus the rebind — and the tick is the larger of the two. The
  median of five was wrong in three ways at once: it measured `rebind()` and called it the budget, four of its five
  samples were manual `rebind()` calls no child experiences, and a median of a distribution is not the one outage a child
  waits through.
  *Fix*: `bench/bench_rebind.py` measures watchdog-triggered outages only, alternating idle and loaded columns, with the
  phase randomised per trial and every sample kept, run twice and both committed
  (`bench/results/2026-11-16-rebind-a.json`, `-b.json`); the default tick moved from
  50 ms to 20 ms so the budget is not spent waiting for a watchdog to look (`SUPERVISE_MS`, D-043, ADR-006); and
  `test_every_watchdog_rebind_lands_inside_the_budget_not_the_median_of_them` asserts **every** sample of 8 watchdog
  rebinds against `REBIND_BUDGET_MS`, printing the worst one.
  *Measured*: 240 samples over two runs — idle worst 21.86 / p50 11.58–12.39 / p95 20.53–20.77, loaded worst 21.47 /
  p50 11.52–11.77, **0 of 240 samples over budget, 0 port moves**; rebind alone p50 0.80–0.82 / max 2.90 ms. Gate
  **PASS** in both runs on a 2 vCPU Linux container.
  *Still open*: the macOS arm64 cell that produced the 107.9 ms sample has not re-run since, so it is the second data
  point, not a confirmation; and ADR-006 §6 records what no in-process watchdog can promise (a frozen container, or a C
  extension holding the GIL past the tick on the in-process attach path). The 107.9 ms sample is retained here as the
  reason the margin matters, not as a number that has been explained away.

- **defect 41 — every clean run's capsule said `counts.nodes: 0`, while its `graph.json` listed the nodes.** The count
  was `len(result.node_statuses)`, and `recovery.classify_nodes` deliberately returns only the **unclosed** nodes,
  because its job is the §14.4 table ("what was interrupted, and what may have happened"). The plan's example manifest
  (§13.2) has `"nodes": 44` for a run whose nodes all finished, so the two disagreed and the code was the one that was
  wrong: a reader, and later the viewer, would have read "no nodes" off a run that had a dozen.
  *Cause*: one function answering two different questions, and the answer to the rarer one used for the common one.
  *Fix*: `recovery.node_count(frames)` counts distinct `NODE_OPEN` slots (slot 0 is the run itself, not a node);
  `counts.nodes` uses it, and the interrupted count is kept beside it as `counts.nodes_interrupted`.
  *Test*: `tests/test_launcher_attach.py::test_one_run_is_one_set_of_linked_tapes_not_one_tape` asserts the child's
  capsule reports its node, and `tests/test_recovery.py` still asserts the interrupted table on the runs that have one.
  *Found by*: measuring P3B-03's "one run with linked nodes" rather than assuming it — the capsule was the thing being
  read at the time, and the zero was next to a `graph.json` that said otherwise.

- **defect 42 — six finished ticket rows existed only in the generated ledger, and one regeneration reverted them.**
  `docs/progress.md` is written by `tools/gen_progress.py` from the plan plus a dict of statuses in that script. P3B-04,
  -05, -06, -08, -09 (done) and P3B-07 (wip) had been edited straight into the markdown at some point and never added to
  the dict. Nothing noticed, because nothing regenerated the file — until P4-10's row was added and the generator ran,
  at which point six finished tickets became `todo` in the ledger while `STATE.json` still had them done.
  *Cause*: two sources of truth for one field, with the weaker one (a generated file) holding the real value.
  *Caught by*: `tests/test_state_consistency.py::test_state_and_progress_agree_in_both_directions`, on the first
  regeneration after the drift — the guard was already there and did its job.
  *Fix*: the rows are in `tools/gen_progress.py`'s status map, copied from the previous file by a script rather than
  retyped, and the generator's ticket filter now includes `P4-` so the viewer rows appear in the ledger at all
  (they were silently absent while `STATE.json` listed them). D-046 records the rule: a status is edited in the
  generator and regenerated, never in the file it generates.
  *Test*: the consistency test above, plus a regenerated `docs/progress.md` in this commit whose diff is the P4 rows and
  the restored P3B rows and nothing else.

- **defect 43 — two of the new launcher tests were POSIX-only and failed all three Windows cells.** Run `43880930567`
  on `66830f2`: `py3.11`, `py3.12` and `py3.13 / windows-latest` each failed
  `test_the_temp_dir_is_removed_even_when_the_child_is_killed` (exit 1, expected 137) and
  `test_a_shell_child_is_given_no_pythonpath_at_all` (`FileNotFoundError: [WinError 2]`). The other seven cells passed
  (one macOS cell and three Ubuntu cells green; two macOS cells cancelled with no runner).
  *Cause*, two of them, and only one is the test's: (a) the kill script used `signal.SIGKILL`, which **does not exist on
  Windows** — merely accessing it raises `AttributeError`, so the child exited 1 and the `137` expectation was a POSIX
  convention asserted everywhere; (b) `/bin/sh` does not exist on Windows. (c) A third problem was in the **product**:
  `cleanup_sdk()` set `removed: True` after `shutil.rmtree(..., ignore_errors=True)` without checking, so on a Windows
  host where the directory is briefly locked the record would have claimed a cleanup that did not happen.
  *Fix*: the kill script picks `SIGKILL` where it exists and `SIGTERM` otherwise, and the expected code follows the
  platform (`137` on POSIX, the platform's own status on Windows); the shell child is `cmd /c echo [%PYTHONPATH%]` on
  Windows and `/bin/sh -c printf` on POSIX, with the same assertion; `cleanup_sdk()` retries three times and reports
  `removed` from `os.path.exists`, with `cleanup_error` naming the path when it survives.
  *A fourth bug, found while making the decision testable*: `_is_python_command`'s regex ended in `[a-z]*`, so
  `pythonista` matched — the launcher would have shipped a `sitecustomize` to a text editor and reported `sdk: enabled`.
  The pattern is now `python(\d+(\.\d+)*)?w?`, plus `py.exe`/`pyw.exe` on Windows only when the name resolves.
  *Tests*: `tests/test_launcher_attach.py` (20), including `test_the_python_command_test_itself_is_platform_neutral`,
  which is the test that caught the fourth bug. Verified on Linux; the Windows cells are the evidence and are quoted
  from CI, not from here.
  **Second round, same defect (run 43885473662, commit 1009576).** The first fix was itself POSIX-shaped in two places,
  and the Windows cells said so precisely: `AssertionError: exit 15 (os.name=nt)` and
  `FileNotFoundError: …\test_a_shell_child_is_given_no0\pp.txt`. (a) `os.kill(pid, sig)` on Windows is
  `TerminateProcess(handle, sig)`, so the exit status *is* the signal number — 15 for the `SIGTERM` fallback, not the
  1 that the helper guessed. The helper now returns 137 on POSIX and `SIGTERM`'s number on Windows, and the comment
  says where the number came from. (b) A quoted redirect path inside a `cmd /c` string is re-quoted by
  `subprocess.list2cmdline`, whose backslash escapes `cmd.exe` does not parse as escapes: the file was created under a
  different name, so the read failed. The test no longer round-trips through a shell on Windows at all — it captures the
  `env` the launcher passes to `Popen` and asserts the SDK path appears nowhere in it, which is the property under test
  observed directly. POSIX keeps the stronger child-observed check.

- **defect 44 — the plan's most safety-relevant classification row was unreachable, and a killed tool call was reported
  as if the tool had never run.** `rec/recovery.py::classify_nodes` counted `KIND_TOOL_REQUEST`/`KIND_TOOL_RESULT`
  frames. **No seam writes those frames**: the hook adapters (`hooks/events.py:116`, `:147`) map `PreToolUse` to a
  `NODE_OPEN` whose payload has `kind="tool"`, and `PostToolUse` to the matching `NODE_CLOSE`. So a process killed
  between a tool's open and its close produced `INTERRUPTED_BEFORE_FIRST_BYTE` with the detail "node opened and never
  touched a model or a tool" — a claim that the side effect did not happen, which §14.4 exists specifically to avoid.
  The row that says *tool invoked; outcome unknown — the side effect may have happened* could not be produced by any
  real recording. **Fix**: `classify_nodes` reads the node payload's own `kind` (new `node_kind()` helper) and counts a
  tool node as a tool request until its close, with the close counting as the result; `ui/model.py` counts `tool_calls`
  the same way, since it showed 0 for every hook-recorded run for the same reason. **Tests**:
  `tests/test_recovery.py::test_a_tool_node_that_never_closed_is_interrupted_in_tool`,
  `::test_a_closed_tool_node_is_not_interrupted`, and `tests/test_ui_viewer.py::test_a_tool_node_counts_as_a_tool_call_in_the_page`.
  Found by the diagnosis ticket's own fixtures, which is the point of writing evidence readers against real frames.

- **defect 43 is closed (2026-11-16, run 43888826452).** All three Windows cells pass on `724f1b4`, and `283554f`
  repeats it (7 success, 3 macOS cancelled). The two fixes it needed are recorded above; the third one — the product's
  `cleanup_sdk` claiming a removal it had not checked — is covered by the same run, since
  `test_the_temp_dir_is_removed_even_when_the_child_is_killed` asserts `removed` against `os.path.exists`.

- **defect 45 — the proxy forwarded the traffic and recorded none of it, so `blackbox run` produced a capsule with no
  request in it.** `rec/launcher.py::_start_proxy` took a `receiver` and the CLI never passed one, so `ProxyServer`
  ran with `receiver=None`: `_call_open`/`_call_chunk`/`_call_close` returned immediately and the listener was a
  relay. Every `blackbox run -- python app.py` therefore recovered a capsule with `events: 5` (bootstrap frames only)
  and `nodes: 0`, while `result.proxy["stats"]["requests"]` said `1` — the run's *only* model call was absent from the
  artefact, and nothing in the summary line hinted at it. `tests/test_launcher_proxy.py` asserted the request reached
  the upstream, which it did; no test asserted the request reached the *tape*. **Fix**: `launch()` builds the
  receiver itself when the caller supplies none (`_default_proxy_receiver`, a `SinkRecorder(TapeSink(writer.emit))` on
  the launcher's own writer — `TapeWriter.emit` is lock-guarded, so the proxy's loop thread and the launcher's
  main thread share one frame chain safely), and `proxy_receiver` stays an override for tests that want their own
  sink. **Tests**: `::test_the_proxy_vantage_lands_on_the_launchers_tape`. Found by running P3B-03's acceptance line
  against real clients instead of trusting the flag, `bench/accept_p3b03.py`.

- **defect 46 — a model call outside any node was reported as an interrupted node, in the run that worked.**
  `rec/recovery.py::classify_nodes` created a node record for whatever slot a `MODEL_REQUEST` named, including slot 0,
  and slot 0 is *the run itself* — `node_count` says so in as many words and skips it. So a run that ended normally,
  with its response fully recorded, reported `nodes_interrupted: 1` and the detail *"response fully recorded; the crash
  happened in the app's handling"*: a claim about a crash that never happened, in the diagnosis a user reads first.
  The manifest said `nodes: 0` and `nodes_interrupted: 1` in the same breath. **Fix**: when the frames carry a
  `RUN_END`, an unclosed slot-0 call is complete and is not reported; with no `RUN_END` the slot-0 call is the only
  evidence of what was in flight and is still classified (mid-stream, before-first-byte), and an unclosed node *of the
  app's own* is unaffected either way. **Tests**: `tests/test_recovery.py::test_a_completed_call_outside_a_node_is_not_an_interrupted_node`,
  `::test_a_call_outside_a_node_is_still_reported_when_the_run_never_ended`, `::test_a_clean_end_does_not_silence_a_real_node`,
  and `tests/test_launcher_proxy.py::test_a_completed_proxied_call_is_not_reported_as_an_interrupted_node`.

- **defect 47 — two capsules of one run name two different run ids, and the link between them says otherwise.**
  **Open.** `rec/recovery.py::Recovery.__init__` derives the run id from `(writer_uuid, meta.created_wall_ns)` and never
  reads the tape's own `RUN_START`. The launcher's tape agrees with that derivation because the launcher *computed*
  `BLACKBOX_RUN_ID` from `run_id_for()`, but a child tape that attached to the launcher's run (`RUN_START` with
  `attached: true`) carries the launcher's id and gets a capsule whose manifest names the derived one instead. Measured
  with `bench/accept_p3b03.py` (real `urllib` child, real loopback upstream): one `blackbox run`, two capsules,
  `r_3fb2a3e364a6255f` and `r_f090ee72bdc2cbf6`, while both tapes' `RUN_START` payloads named the same id — and both
  capsules' `links.json` describe the same pair as `exact` with `run_id` set to the *declared* id. So a capsule
  contradicts its own tape and its sibling, and any consumer grouping capsules by `run.run_id` sees two runs where the
  link says one. P3B-03's "one run with linked nodes" is **not** ticked on this evidence. The fix is not a one-liner:
  the run id is also the run *directory* name and the capsule id (`"c_" + _hash(run_id)`), so two attached tapes sharing
  a run id would collide on one capsule path unless capsule identity is derived from the writer too, and the
  `recovery.done.<STEP>` resumability markers are keyed on it. That is a ticket, not a patch at the end of a session.
  **Closed 2026-11-16**: the two identities were separated instead of merged. A tape now has a `tape_key` (derived from
  its writer and creation time) that names its *work directory*, where every file belongs to that tape alone — nothing is
  shared, so nothing can be overwritten; and a `run_id` that is the run its own `RUN_START` declares, falling back to
  `tape_key` when the tape names nothing (or names something that is not a run id, like the writer uuid an unattached SDK
  tape writes). The capsule is per *tape* — `c_` + hash(run id, writer) — so two tapes of one run are two capsules and
  the second seal cannot replace the first, while both manifests and both links now name one run. Verified end to end
  with real clients (`bench/accept_p3b03.py`, `bench/results/2026-11-16-p3b03-acceptance.json`): `python` (urllib),
  `node` 22.22.3 and a real `openai` 3.51.0 client each exit 0 with every capsule root intact and one run id across the
  capsules. Tests: `tests/test_recovery.py::test_two_tapes_of_one_run_share_the_run_id_and_keep_separate_capsules`,
  `::test_a_tape_that_names_no_run_keeps_its_own_id`, `::test_a_declaration_that_is_not_a_run_id_is_ignored`,
  `::test_the_capsule_id_changes_with_the_writer_not_only_the_run`, and the assertion added to
  `tests/test_launcher_attach.py::test_one_run_is_one_set_of_linked_tapes_not_one_tape`.

- **defect 48 — the repair tests hashed `\n` and wrote `\r\n`, so the applier's own guard failed them on Windows.**
  Run 43916957036 (5663879) fixed the symlink-root refusal and the Windows cells still failed, on
  `test_the_right_token_applies_exactly_the_approved_bytes_and_keeps_a_backup` and two neighbours, with
  *"src/app.py changed after it was approved (reviewed b912ea56, now f9c84e02)"*. Nothing had changed: `Path.write_text`
  opens in text mode and translates `0x0A` to `0x0D 0x0A` on Windows, while the fixture's `_sha(text)` hashed the
  untranslated string. The product was right — a file whose bytes differ from the reviewed hash is exactly what the
  write-time re-check exists to refuse — and the fixture was the thing that was wrong, on the platform where nobody had
  run it. **Fix**: every fixture write goes through `_write()` (`write_bytes`), `_change()` hashes `read_bytes()`, and the
  tree snapshot compares hex bytes rather than text, so a CRLF difference can no longer hide inside `read_text`'s
  translation on either side. This is the third defect in this family (C29, D-050, and now the tests): text mode on
  Windows is not a detail of this feature, it is a way for a byte-exact guarantee to be silently untrue.

- **defect 49 — the auto-import makes a tape and installs no seam, so an unmodified SDK script records nothing itself.**
  **Closed 2026-11-16.** `cap_sdk/autoimport.py::activate()` calls `api.install()`, which opens the child's tape and writes
  `RUN_START`/`RUN_END` — and nothing else. The three capture seams are installed by `cap_sdk/preflight.py::choose()`,
  and **no module in `src/` calls `choose()`**: the seams are exercised by tests and by the day-14 lab, which wires them
  by hand (`SinkRecorder(TapeSink(api.emit))`). Measured under `blackbox run`: a child using the real `openai` 3.51.0
  client, with the banner saying `sdk auto-import (sitecustomize)` and `BLACKBOX_VERBOSE=1` saying
  `blackbox sdk: installed`, leaves a capsule with `{RUN_START, CHAIN, RUN_END, SEG_END}` and **no `MODEL_*` frame**.
  The call is still recorded — by the proxy's vantage, on the launcher's tape — so nothing is lost, but the child's own
  vantage is empty and the summary line implies otherwise. This is why P3B-03's "one run with linked nodes" is not
  claimed as met: with no seam and no `NODE_OPEN`, both vantages report `nodes: 0` and the graphs have no nodes to link.
  Found by adding the SDK client to the acceptance harness rather than by reading the flag.
  **Fix**: `api.install()` calls `api.install_capture()` once the child's tape exists — after `RUN_START` and the
  Keeper, so a recording that fails is still a run — and that builds `SinkRecorder(TapeSink(_emit_at_current_node))` over
  the same writer the launcher's tape uses, then asks `preflight.choose()`. The node slot is resolved per *event*
  (`rec.context.current()`, slot 0 when no node is open), not once at install time, because a seam outlives any one
  `with api.run(...)`. `capture_status()` and `status()["capture"]` report `{installed, seam, attempts}` without the
  handle or the recorder, and `_close_locked()` releases the seam before the writer goes away, so a later close cannot
  emit into a dead tape. Installing a seam from `install()` exposed the second half of the defect: a product that had
  installed its own seam (`tests/_day14_child.py` does exactly that, as the day-14 lab's examples do) got the client
  class patched **twice**, and `openai` then raised *Attempted to call a sync close on an async stream* before reading a
  byte — reproduced here, not theorised. So `preflight` gained `adopt()`/`release()`: when the product has
  already installed a seam, `choose()` returns that seam with attempt status `already-installed` and the reason *the
  product installed this seam; a second patch would double it*, instead of patching again. One seam per process, and
  the second caller still gets a working recorder. **Tests**: `tests/test_launcher_attach.py::test_a_childs_own_model_call_lands_on_its_own_tape`
  (proxy **off**, real `httpx` child, `MODEL_REQUEST` present, `counts.nodes == 1`, `nodes_interrupted == 0`),
  `::test_the_same_capture_works_for_an_unmodified_openai_client` (real `openai` client, same assertions) and
  `::test_one_seam_per_process_even_when_the_app_installs_its_own` (the second `choose()` returns `httpx-hooks` with
  `already-installed`, and one request writes exactly one `MODEL_REQUEST`). All three are red with `install_capture()`
  disabled — the child's capsule then holds `{RUN_START, NODE_OPEN, NODE_CLOSE, RUN_END, SEG_END}` and the second
  decision says `installed` — and green with it, which is the check that these tests are about the defect and not about
  the harness. This closes *recording nothing on the child's own tape*; it does not by itself tick P3B-03's "one run
  with linked nodes", which still needs a run whose `graph.json` shows nodes and `exact` links between the vantages.

- **defect 50 — `_read_nofollow` followed the link on the one platform where the flag is missing.** Run 43922055318
  (HEAD `081fac5`) failed `test_reading_an_original_does_not_follow_a_link` on all three Windows cells with *DID NOT
  RAISE*. The cause was not the test: `NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)` is `0` on Windows, so `os.open` followed
  the link and the function returned the bytes on the other side — the one thing its name promises it will not do, and
  the read `_prepare` uses for the originals it hashes for approval and copies into the backup. The POSIX cells cannot
  see this, which is why it survived every run here. **Fix**: when the platform has no flag, refuse with an explicit
  `islink` check before the open, raising the same `OSError` so callers do not branch. The remaining exposure is the
  check-then-open window, which D-050 already records and which this does not close. The reader also moved to
  `proposal.read_nofollow` and `sha256_file` now uses it, because the hash of the file that was approved and the hash
  of the file about to be replaced were being taken by two different rules — the write path nofollow, the hash path
  follow. **Tests**: `tests/test_repair_workflow.py::test_reading_an_original_does_not_follow_a_link` (asserts *which*
  mechanism refused) and `::test_without_the_descriptor_flag_the_refusal_is_a_check`, which takes the flag away on any
  platform and therefore runs the Windows path in CI's Linux cells too.

- **defect 51 — the applier promised a private backup directory on a platform that cannot express one.** Same run, same
  three cells: `test_the_backup_directory_is_inside_the_repository_and_private` failed on
  `(os.stat(root).st_mode & 0o777) == 0o700`. The product was not lying by accident — `_backup_root` chmodded to `0700`
  and did not re-check — but on Windows `os.chmod` only toggles the read-only attribute and `os.stat` synthesises
  `0o666`/`0o777` from the ACL, so the directory the product called private was whatever the repository's ACLs make it.
  That is a privacy claim the platform cannot support, and the honest answer is to stop making it rather than to relax
  the test. **Fix**: `_private_dir()` enforces 0700 only where mode bits mean something, re-reads the mode after
  `chmod` and **refuses** when a filesystem that has modes ignores the change — a place this feature cannot keep its
  promise is not a place to put the user's source code. Where modes do not exist it returns `False`, which flows into
  `Applied.private_by_mode`, a `backup` block in `apply-manifest.json` naming the weaker guarantee, and one line of
  stderr from `repair apply`. `MODES_ARE_REAL` is the capability flag, and the Windows branch is exercised on POSIX by
  clearing it. **Tests**: `::test_the_backup_directory_is_inside_the_repository_and_private` (platform-true),
  `::test_a_platform_without_modes_says_so_instead_of_claiming_0700`,
  `::test_a_filesystem_that_ignores_chmod_is_refused_rather_than_trusted`.

- **defect 52 — the rollback test could not index its own snapshot on Windows.** Same run: `KeyError: 'src/b.py'`. This
  one *is* a fixture, and the evidence is that the assertions before it passed: the apply failed, the message contained
  `COULD NOT restore` and `app.py`, and `manual intervention required` was present — the rollback did what it promises.
  `_snapshot` keyed its dictionary with `str(p.relative_to(root))`, which is `src\\b.py` on Windows, while the test
  looked the file up as `src/b.py`. **Fix**: `.as_posix()` in the helper, so the comparison is about bytes and not about
  a separator. Worth recording precisely because it looks like the two above: a Windows red cell is not evidence of a
  product bug until the failure line says so.

- **defect 53 — every cleanly sealed run was reported `SEAL_INTERRUPTED`, in its capsule and in the summary line.**
  Found while measuring P3B-03's acceptance line: all four cells of a fresh `bench/accept_p3b03.py` run exited 0 and every
  capsule still said `run.status: SEAL_INTERRUPTED, status_cause: SEAL_NEVER_FINISHED`. The cause was one line of the
  scanner: `sealed_segments` was filled only by the loop over the segments *before* the active one (`n < n_active` by
  construction), so the seal that `TapeWriter.close()` writes for the last segment — the normal end of every run — was
  never read. Measured directly: a tape written and closed in one process has `00000001.seal` on disk,
  `seal.read()` returns a valid record for it, and `scanner.scan(...).sealed_segments` was `[]`. §14.4's rule is
  "`RUN_END` present but no seal ⇒ `SEAL_INTERRUPTED`", so every clean run told the user its capsule build had been
  interrupted and was "safe to re-run" — the one sentence a re-run would change nothing about. **Fix**: the scanner also
  reads the active segment's seal and counts it when `seal["segment_no"] == n_active` **and**
  `seal["end_seq"] == head.head_seq`, i.e. only when the seal describes exactly the bytes the committed head names; a
  seal left over from before a later append does not match and is ignored, so a reopened tape cannot inherit a clean
  verdict. **Two tests had the behaviour written down as a rule**:
  `tests/test_tape.py::test_roll_produces_two_sealed_segments_and_a_full_scan` asserted `sealed_segments == segs[:-1]`
  *after* calling `w.close()`, and `tests/test_launcher.py::test_the_plan_acceptance_line_leaves_a_recovered_run`
  asserted `SEAL_INTERRUPTED` for `sh -c 'kill -9 $$'` — where the *child* dies and the launcher's own tape closes
  cleanly, so the assertion was about the scanner's blind spot and not about the killed child (whose death is
  `exit_code: 137`/`signaled: 9` in the same test). Both now assert the contract. **New test**:
  `tests/test_tape.py::test_a_clean_close_is_sealed_and_a_cut_off_tape_is_not` writes one tape with `close()` and one in
  a child that `os._exit`s after `publish()`, and asserts `[1]` against `[]` — so `SEAL_INTERRUPTED` stays reachable and
  is still what a genuinely cut-off writer (the SDK's tape under `SIGKILL`, `tests/test_day14_kill.py`) reports.

- **defect 54 — a clean run's `graph.json` listed no nodes while the same tape's manifest counted one.** `step_graphed`
  built `graph["nodes"]` from `classify_nodes`, which answers §14.4's question and therefore returns *only unclosed*
  nodes; the manifest's `counts.nodes` is `node_count`, which counts `NODE_OPEN` slots. Defect 41 fixed exactly this
  disagreement for the manifest and left the graph holding the other half, so after the seam was installed (defect 49)
  the two artefacts described different runs: capsule `counts.nodes: 1`, `graph.json` `nodes: []`, `open_nodes: 0`.
  Nothing in `src/` reads `graph.json`, which is why no test caught it. **Fix**: `graph_nodes()` writes one entry per
  `NODE_OPEN` slot — `node_slot`, `name`, `kind`, `parent`, `opened_ns`, and a status taken from the `NODE_CLOSE`
  payload (`outcome`, `duration_us`) or, for a node that never closed, from the interrupted classification — sorted by
  slot; a `NODE_CLOSE` whose `NODE_OPEN` is not in the scan keeps its slot and says so rather than being dropped.
  `open_nodes` keeps its meaning: the number still open. **Tests**:
  `tests/test_recovery.py::test_the_graph_lists_every_node_the_run_had_not_only_the_unclosed_ones` (a closed node and an
  interrupted one in one graph, and the count agrees with `node_count`),
  `::test_the_capsule_of_a_clean_run_does_not_call_itself_interrupted`. Measured end to end in the acceptance report:
  the `sdk-nodes` case now has `graph_nodes: 1` next to `counts.nodes: 1`.

- **defect 55 — `graph.json` carries no edges, so "linked nodes" cannot be read from the graph the ticket names.**
  **Narrowed 2026-11-16 by D-053; still open, and the residual is one sentence.** `step_graphed` used to write
  `"edges": []` unconditionally, and the cross-vantage relation lived only in `links.json`. D-052 then wrote the edges
  it could *justify* — run and tape level — and refused the node-level claim for want of a per-call identifier. D-053
  supplies that identifier: the seam mints `x-blackbox-call` per outbound request, records it as `call_id` on its own
  `MODEL_REQUEST`, and the proxy reads the header, records the same id on its own frame, and strips it before the
  upstream; `rec/links.py::resolve_calls` matches records by that id alone, and `rec/recovery.py::graph_edges` writes
  them as `level: "call"` edges whose `evidence` says `same call id` / `carried_by: x-blackbox-call`. The graph is now
  a declared capsule part, so what it says can be re-derived from the raw frames and checked (`blackbox capsule
  verify`, `BBX-E412`). Measured with real clients in
  `bench/results/2026-11-16-p3b03-acceptance.json`: `run_level_edges_recorded: 10`, call-level edges present in both
  the `sdk` and `sdk-nodes` cases, `edges_naming_a_node: 2` — the two call edges in the capsule that recorded a node
  name the slot the call ran in (`nodes.from: 1`) — and `node_level_edges_recorded: 0`.

  **What is still missing, and why it is not a bug to patch but a decision to take.** Every one of those call edges is
  `node_join: "partial"`: the proxy's vantage records no node, because slot 0 on its tape is the run itself and D-052
  rule 2 forbids inventing one for a proxied call. So no edge joins two *nodes*, and `CallLink.level` — which returns
  `node` only when both ends name a slot — has no real-client witness, only the synthetic one in
  `tests/test_call_links.py`. Closing that needs a node identity both vantages can write: the seam knows which node it
  is inside, so the wire can carry it (`x-blackbox-node`, the plan's own header name) and the proxy can record what it
  was told instead of a slot it guessed. That is defect 55's remainder, and it is a decision record first.

  **Evidence for the fix, named so a reader can check it**: `tests/test_call_links.py::test_two_concurrent_calls_stay_two_links_each_with_its_own_node`
  and `::test_a_node_to_node_edge_appears_only_when_both_sides_have_a_node` (the resolver: the second is the branch with
  no real-client witness, and it says so), `tests/test_launcher_proxy.py::test_two_concurrent_calls_get_two_ids_and_two_graph_edges_each_naming_its_own_node`
  (a real proxied child, two concurrent calls, two ids, two edges naming the slot each call ran in) and
  `::test_the_call_id_lands_on_both_tapes_and_never_reaches_the_upstream`,
  `tests/test_recovery.py` (the merge into `edges`, the `call_level`/`ambiguous` counters),
  `tests/test_capsule_graph.py` (the part is inside the capsule, its hash matches, an edited graph is reported and not
  repaired, an old capsule verifies as `absent`). `tests/test_capture_failopen.py` pins the interface half: a receiver
  written before the new `call_id` hint keeps recording instead of failing quietly.

- **defect 56 — two repair tests gave `verify` a POSIX command and read the shell's refusal as the product's.** Run
  `43931027526` (`e007259`, the first run with defects 50–52 fixed): the three Windows cells failed
  `tests/test_repair_workflow.py::test_verify_reports_that_it_executes_the_command_it_was_given` and
  `::test_the_banner_comes_before_anything_runs` with `assert 3 == 0`, and nothing else. Exit 3 from `repair verify`
  means one sentence: *the command you gave me exited non-zero* (`return 0 if report["verified"] else 3`), and the
  command was `echo ran > '<tmp>/executed.txt'`. `verify_change` runs it with `subprocess.run(..., shell=True)`, which is
  `cmd.exe /c` on Windows, and cmd does not strip single quotes: the redirect target became a token beginning with `'`
  and containing `:` — a name Windows rejects (*"The filename, directory name, or volume label syntax is incorrect"*),
  so `echo` failed and the product reported `FAIL exit 1` exactly as it should. **The product was right and the fixture
  was writing a command for a different shell**; the failure is the same family as C29/defect 48 (text mode) and
  defects 50–52, one level out: a test that pins behaviour by asking a platform to do something only another platform
  does. **Fix**: `_shell_echo()` builds the command for the shell that will run it — `echo ran> "<path>"` and
  `echo run>> "<path>"` on Windows (no space before the redirect, or cmd writes it into the file), the old
  single-quoted form on POSIX — and `::test_the_verify_command_is_quoted_for_the_platform_shell` pins both forms by
  patching `os.name`, so the POSIX-only form cannot come back. **Uncertainty, stated**: the job log for this run is not
  reachable from this sandbox (GitHub redirects it to a blob host), so the cause above is read from the exit code, the
  command text and cmd's quoting rules. **Closed 2026-11-16 by a controlled pair, which is the check that settles it**:
  `f3fd801` (the same tree without the fix, run `43935414757`) failed **exactly** those two tests on all three Windows
  cells, and `7d55ba1` (run `43935889831`) has all three Windows cells **green** with every other executed job green
  too — so the two tests were the only Windows failure on the branch and are now not. The macOS jobs in both runs were
  `cancelled` with `steps: 0` (*"The job was not acquired by Runner of type hosted"*), i.e. runner capacity, not a test
  result; that is why those runs are red at all, and macOS stays unmeasured.

- **defect 57 — the resolver decoded the tape's model frames as CBOR, so the call-link fields it was written to read did
  not exist.** Found while measuring D-053, by a probe that ran two concurrent calls through a real proxy and got *no*
  call links back: not an ambiguous link, none at all. `correlate._payload()` called `cbor.decode_exact()` on every
  frame payload, and a `MODEL_REQUEST` payload is **JSON** (§4.0, D-030) — so the decode raised
  `CBORError: text string too large` for every model frame, the except branch returned `{}`, and every field read off it
  (`call_id`, `request_fp`, `linked_run`) came back `None`/absent. That is the shape of a quiet failure this project has
  a name for: the code said "looked and found nothing" while it had never looked. Two consequences, both real: the new
  call links could never appear on a real run (the first measurement would have been a false zero), and
  `links.resolve`'s fingerprint path compared two empty strings, which match each other — so two unrelated calls with
  no fingerprints were one `inferred` link away from being joined by the *absence* of evidence. **Fix**: `_payload()`
  tries CBOR and falls back to `json.loads` (the same order `rec/recovery.py` uses for the same reason), and
  `links.resolve` refuses to compare an empty fingerprint before comparing any. **Tests**:
  `tests/test_call_links.py::test_the_resolver_reads_json_model_request_payloads` builds a tape whose `MODEL_REQUEST`
  is JSON and asserts the id and fingerprint arrive, and `::test_two_calls_with_no_ids_are_not_matched_by_fingerprint_or_time`
  asserts two id-less, fingerprint-less calls are **not** linked. Measured after the fix: two concurrent calls →
  `2` ids, `2` call edges, `node_join: partial`, `call_level: 2` in `edge_summary` (before: `0` and `0`).

- **defect 58 — the first macOS cell that actually ran failed the watchdog budget by 4.7 ms, and the message could not
  say whose 4.7 ms they were.** Run `43973451998` (`823d08a`) is the first run in this branch's history where a macOS
  runner was *acquired*: `py3.12 / macos-latest` executed (11 steps, not the `steps: 0` cancellation of the earlier
  runs) and failed
  `tests/test_proxy_supervisor.py::test_every_watchdog_rebind_lands_inside_the_budget_not_the_median_of_them` with
  `worst watchdog outage 104.7 ms over the 100.0 ms budget; outages [96.5, 76.7, 29.4, 100.7, 44.2, 14.4, 104.7, 22.1]
  ms, rebinds [1.49, 1.55, 1.52, 1.5, 1.11, 1.57, 1.18, 1.34] ms`. Two of eight samples are near the whole budget and
  **every rebind took between 1.1 and 1.6 ms**, so the product's own work is two orders of magnitude inside the budget
  and the outage is detection: a watchdog tick that woke 80–100 ms late on a shared, capacity-constrained arm64 runner
  (GitHub's own annotation on that job says "Due to capacity constraints, jobs targeting macOS arm64 runners may
  experience longer queue times"). The failure message could not distinguish that from a slow rebind, which is the part
  worth fixing first: a row that reports one number for two different things invites the answer defect 40 got (quote a
  median until it is green). **Fix (measurement, not a relaxed gate)**: `ProxyThread.wake_late_ms` records how late each
  watchdog tick woke, and the failure message now prints the worst lateness over the number of ticks, beside the
  outages and the rebinds. **The assertion is unchanged** — 100 ms, every sample — so the gate stays red on this commit
  and macOS stays unmeasured for the *product's* latency until a run on a runner that is not starved passes it. The
  open question, for the owner, is whether §11.8's promise is "a rebind is repaired within 100 ms" (which the rebinds
  column already shows, by 60x) or "the *outage* is under 100 ms on any machine", which no sleeping thread can promise
  and which would need detection that does not depend on being scheduled — an event-driven watch on the listener's
  socket rather than a 20 ms tick. Both readings are written down here rather than resolved by editing the test.
  **Second measurement, which is the one that settles what the first one meant** (run `43977497614` at `37f5fbf`): all
  ten jobs were scheduled, **both the `py3.11` and the `py3.13` macOS cells passed**, and only `py3.12 / macos-latest`
  failed — with `outages [75.5, 29.4, 33.3, 113.5, 65.9, 91.6, 17.6, 18.7] ms`, `rebinds [1.27, 1.08, 1.23, 0.46, 1.78,
  1.25, 0.64, 0.76] ms` and, from the field this fix added, **`worst watchdog tick lateness 113.2 ms over 13 ticks`**.
  The outage and the lateness are the same number to within 0.3 ms: the watchdog thread did not run for 113 ms, and the
  rebind it then did took 1.78 ms. So macOS is measured now, twice, on two different commits; the product's own rebind
  there is 0.5–1.8 ms, and the only red assertion on the branch is a wall-clock bound on a thread the runner stopped
  scheduling. That is also why this entry is open: the bound may be the right one to keep and the test may need to
  measure the machine's share separately, and that choice belongs to the owner, not to a green build.
  **Fix, in two parts, and the gate is untouched** (D-055). (1) *Measure the three components separately*: the failure
  message prints the rebinds and the worst tick lateness beside the outage, so a red row says which half was ours; the
  bench writes `outage_ms`, `rebind_ms`, `detection_ms` (outage − rebind, every sample) and the supervisor's own
  `noticed_ms` with its own `n`, per column. (2) *Notice earlier*: `ProxyServer.close()` now calls a one-shot `lost_cb`,
  `ProxyThread` turns it into a `threading.Event` the watchdog waits on instead of sleeping out the whole tick, and a
  close is detected in microseconds. The tick remains as the **backstop** for a listener that dies without saying so,
  and `tests/test_proxy_supervisor.py::test_a_listener_that_dies_without_saying_so_is_still_restored_on_the_tick`
  removes the notice to prove that path still restores service on the same port. Measured on this box
  (`bench/results/2026-11-16-rebind-detectors.json`, n=40 per column, Linux 6.1 x86_64, CPython 3.11.2, 2 CPUs, idle and
  one busy process per CPU): notice columns outage p50 0.72–0.92 ms / max 1.70–3.30 ms with `noticed` p50 0.02–0.08 ms;
  tick columns outage p50 11.9–12.7 ms / max 20.5–21.1 ms with `noticed` n=0. **160 samples, zero over the 100 ms
  budget, and the assertion is still per-sample on the end-to-end outage.** **Still open**, deliberately: what the macOS
  runner did to this branch is 113 ms of *lateness*, and no notice helps a thread that is not scheduled — so the entry
  stays open until a run of the four-column bench on a runner that starves threads shows the notice columns inside the
  budget while the tick columns show the stall. The universal-100 ms claim is not made anywhere: `bench_rebind.py`'s own
  `method.bound` string says the budget is a gate on the machine that ran the file, not a promise about any machine.

## Defect 59 — the verifier's own report died on a Windows console (2026-11-16, P4-16)

**Found by** CI run `44005929845` at `4a1f629`: three Windows cells red, all on
`tests/test_verify_cli.py::test_nothing_is_written_by_a_failed_verification`, with
`UnicodeEncodeError: 'charmap' codec can't encode character '\u2260'`.

**Cause.** `blackbox verify` printed `≠` between the declared and recomputed values of a failed check. A Windows console
is cp1252 and cp1252 has no `≠`, so the command that exists to report a finding crashed while reporting one — on the
platform where the report is most likely to be read by a human. The failure only appeared in the *failure* path: a green
report never prints that glyph, which is why the other Windows tests stayed green.

**Fix.** Every line the command prints is ASCII (`!=` in place of `≠`, and the em dashes in the argparse description
replaced), and two tests hold the line: one asserts a failing report's stdout is ASCII, the other asserts `--help` is
too. `docs/DECISIONS.md` D-057 records the rule — a verifier that cannot print a finding on the platform it runs on has
not verified anything.

**Not the same class, checked deliberately.** The *existing* `capsule verify` prints `…` and is left alone: cp1252 can
encode it, so it is not a defect, and changing a working command to satisfy a rule about a different one would be churn.

## Defect 60 — the watchdog spent its budget waiting for its own loop thread (2026-11-17, P3B-03/resubmission of PR #17)

**Found by** CI run `44132154945` at `127d187` (push) and `44132192208` (pull request), ubuntu py3.11:

```
FAILED tests/test_proxy_supervisor.py::test_every_watchdog_rebind_lands_inside_the_budget_not_the_median_of_them
AssertionError: worst watchdog outage 103.2 ms over the 100.0 ms budget;
outages [103.2, 0.8, 0.7, 0.8, 0.7, 0.7, 0.8, 0.7] ms,
rebinds [103.04, 0.36, 0.33, 0.34, 0.35, 0.33, 0.42, 0.26] ms,
worst watchdog tick lateness 86.2 ms over 9 ticks
```

**What the numbers say.** The outage and the rebind are the same 103 ms: the time was inside `rebind()`, not in the
tick that precedes it (the 86.2 ms lateness is a different, earlier tick, recorded by the same run). Inside `rebind()`
there is exactly one thing that can take 100 ms on an idle proxy: `asyncio.run_coroutine_threadsafe(old.close(),
loop).result(5)` — a **cross-thread round trip** from the watchdog thread to the loop thread and back. Every other
sample on the same runner took 0.26–0.42 ms, so this is not the socket work; it is the OS not scheduling the loop
thread for ~100 ms while the watchdog waited on it. The machine is also visibly loaded: 86.2 ms of tick lateness.

**Why it is a defect and not a runner story.** A design that needs *two* thread hops to repair a listener charges the
machine's scheduling jitter twice: once to wake the watchdog, once to get back onto the loop. The second hop is
avoidable, because the early notice (`lost_cb`, defect 58) already fires **on the loop thread**, which is running and
which is the thread that must serve the child anyway.

**Fix.** `ProxyThread._listener_lost` now schedules the repair on the loop (`loop.call_soon` when it is already on that
thread, `call_soon_threadsafe` otherwise) and `_restore_on_loop` closes and re-binds there, with `_restoring` keeping
the watchdog's tick off a second, racing rebind. The port is remembered in `_port` rather than read off a listener that
has just been closed — that was the first attempt's bug: `_server` was `None` at that point, `port` fell back to `0`,
and the restore took a fresh port (`every rebind must keep the port` caught it in one run). The watchdog tick is
unchanged and still the only path for a listener that dies **without** saying so
(`test_a_listener_that_dies_without_saying_so_is_still_restored_on_the_tick` sets `lost_cb = None` and asserts the
tick restores it).

**The gate is not weakened.** `REBIND_BUDGET_MS` is still 100.0 and still asserted on **every** sample, not a median or
a p95. Two things did change in the *measurement*, both stated in the test's docstring: `outage_ms` now starts when
`close()` has **returned** (before that the listener was still accepting, so a child saw no outage), and the harness's
own `dispatch_ms` is printed beside it instead of being silently counted as the product's latency. What the child
experiences from the moment the listener stops is still asserted; the test's scheduling is now visible rather than
folded in.

**Measured after the fix.** `bench/bench_rebind.py --n 60`, Linux 6.1 x86_64, CPython 3.11.2, 240 samples over four
columns (`bench/results/2026-11-17-rebind-after-loop-restore.json`): the gate is **PASS**, worst outage 21.185 ms,
**0 over budget, 0 port moves, 0 not restored**. The notice path, before → after on the same machine
(`2026-11-16-rebind-detectors.json` → this file): `notice_idle` outage p50 0.919 → **0.459 ms** and rebind p50
0.44 → **0.186 ms**; `notice_loaded` outage p50 0.721 → **0.141 ms** and rebind p50 0.226 → **0.06 ms**. The tick
columns are unchanged (p99 20.4 / 21.2 ms) because the tick path was not touched. The claim stays what it was: a budget
measured on the machine that ran it, no universal 100 ms promise — `bench_rebind.py`'s `method.bound` string says so, and
defect 58's open row is about the *universal* claim, not about this one.

**The clock, second revision (2026-11-17): it is measured by requests, not by a coroutine's return.** The revised
clock above starts when `close()` returns, which fixes the harness's hop but is optimistic in the other direction: the
listening socket stops accepting *inside* `close()`, so a request is already refused while the coroutine is still
running — in `wait_closed()`, in a pool drain, or after the loop thread was descheduled before the caller was woken. An
in-process clock cannot see that interval, because it is inside the call whose return it is timing.

`tools/rebind_probe.py` measures it from outside our threads: real requests in a loop through the proxy on the port the
child was told, from the **first request that was not served** to the first one that was. Any HTTP status line counts
as served, including the proxy's own 504 for an unreachable upstream. A raw socket rather than `http.client` for a
measured reason: `http.client` can block for its whole timeout on a connection accepted just before the listener went
away, so the probe sleeps through the outage it is supposed to be measuring and reports "no failure observed".

**Two designs were tried and rejected on measurement, and both are recorded here so they are not retried.**

*A connect-only probe.* Cheap, and it cannot hang — but a listening socket that has been closed keeps completing
handshakes from its accept queue while the queue has room, so connections are **accepted** for tens of milliseconds
after the listener is gone (46 ms of a 60 ms stall, measured) with nothing answering them. "Connected" is not "serving".

*A fixed 10 ms answer timeout* (to stop a hung request blinding the probe). On the Windows runner every attempt came
back `timeout@15.5ms`: a loopback request through this proxy answers in ~16 ms there, so the probe reported that a
serving listener was not serving, and four supervisor tests failed with `no request was served in 2.0 s`. A cap is only
honest above the floor of the machine, and that floor is not knowable from here — so the cap is now **measured at
start-up** (`ServingProbe.calibrate`: three times the median answered request, never below 10 ms, never above the 50 ms
attempt budget) and recorded with every sample as `probe_answer_timeout_ms`. Three requests are in flight at once, so a
request that is never answered cannot blind the probe for more than one attempt.

**What the unit test pins down.** `tests/test_rebind_probe.py` stalls `close()` for **60 ms after the socket has already
stopped accepting**, then measures the same break two ways: the request clock reports the 60 ms as an outage (the
`outage_upper_ms` bound contains it by construction), and a clock that started when `close()` returned reports ~0 ms of
it. The test asserts the request clock contains at least 80% of the stall and that the post-`close()` clock misses at
least half — a red test says which of the two claims broke.

**The probe has a resolution, and it is reported.** One attempt is 0.2–0.6 ms on loopback (median of *served* attempts
— refusals return in microseconds and would fake a 0.01 ms resolution exactly when the number matters).
`probe_cycle_ms` and `probe_answer_timeout_ms` are printed beside every outage, and the budget is asserted on
`outage_upper_ms` — last request served before the break to first served after it — because that bound still covers the
gap when no request happened to be refused.

**Two more things the Windows runner taught, both now in the code.** The probe must be aimed at a **live upstream**
(`LocalUpstream`): the proxy answers a request to a dead port with its own 504, and on Windows that path costs more than
one attempt budget, so the probe measured the failure path and read every attempt as a timeout. And `calibrate` must not
*lower* the answer cap before the machine has answered anything — its first version did, which set the 10 ms floor on
exactly the runner that needed the generous value.

**Measured after the fix, request-level — corrected on 2026-11-18.** The first request-level run failed the gate:
3 of 240 samples over the 100 ms budget, all in `tick_loaded`, worst 3319.604 ms. That was written down here as "the
watchdog thread was not scheduled". **That was wrong**, and the way it was found matters more than the correction:

every sample now also records the supervisor's own tick lateness (`wake_late_ms`, defect 58's record), a no-op ping on
the proxy's loop (`loop_late_ms`, how long the loop takes to answer anything at all), a probe pointed **directly at the
upstream** (so a starved upstream cannot hide as a dead listener), and the *kind* of non-service — `refused` means the
listener was gone, `timeout`/`no-answer` means it was there and the answer did not arrive. In the worst samples:
`wake_late_ms` **5.8 ms**, `loop_late_ms` **14.4 ms**, `rebind_ms` **0.4 ms**, and the upstream itself failing **22 of 64**
direct requests. The watchdog was not late and the rebind was not slow.

A timeline capture then reproduced the cause directly:

```
OSError: [Errno 24] Too many open files
  File "asyncio/selector_events.py", line 165, in _accept_connection
socket.accept() out of system resource
```

The probe shares the process with the proxy. Three threads with no rest issued thousands of connections a second, and
this machine allows **1024** descriptors per process (`ulimit -n`) on **two** CPUs: the probe exhausted the table, the
listener could no longer accept, and the measurement reported seconds of "outage" that were its own load. A measurement
that saturates the thing it is measuring is not a measurement.

**Fix (in the probe, not in the gate).** `ServingProbe` defaults to two threads with a 1 ms rest between attempts — a
few hundred attempts a second, ~2 ms of resolution, far inside the budget — and every measurement reports `open_fds` so
saturation is visible in the artifact instead of being inferred later.

**Result** (`bench/results/2026-11-18-rebind-instrumented.json`, `--n 60`, Linux 6.1 x86_64, CPython 3.11.2, 240
samples, four columns, `open_fds` 12–25 throughout): gate **PASS**, worst upper bound **53.98 ms**, 0 over budget,
0 port moves, 0 not restored. Outage p50 / p99 by column: `notice_idle` 1.213 / 5.891 ms, `notice_loaded` 0.881 /
53.980 ms, `tick_idle` 10.156 / 21.512 ms, `tick_loaded` 8.080 / 23.123 ms.

Three further runs of the same command in the same session passed at **45.781**, **47.043** and **40.754** ms (0 of 240
over budget in each); they are not committed as artifacts — the run above is the one whose samples are in the tree, and
the command that produced it is `bench/bench_rebind.py --n 60` with no other arguments.

The budget (`100.0 ms`), the per-sample assertion, the request-level definition and its limits are unchanged. What
changed is that a slow sample now names its own cause, and that the measurement no longer manufactures the failure it
is supposed to detect.

## Defect 61 — a stopped proxy left its handler tasks to the garbage collector (2026-11-18, P3B-03)

**Found by** the Windows cells of `9a42b0f`, which printed one of these per shutdown (the job log is unreadable from
here, but the annotation carries it):

```
ERROR asyncio:base_events.py:1833 Task was destroyed but it is pending!
task: <Task pending name='Task-4' coro=<ProxyServer._handle() ...> wait_for=<Future pending>>
```

**Invariant.** After `ProxyThread.stop()` the proxy owns nothing: no listener, no connection, no task that can wake up
later and touch a closed loop.

**Cause.** `stop()` closed the listener and called `loop.stop()`, but never closed or drained the loop. A handler still
waiting on an upstream was therefore a *pending task* that nobody owned: destroyed whenever the collector reached it —
one warning per shutdown — with its two sockets handed to the GC instead of closed. Windows showed it first because a
request to a dead upstream there waits for a SYN retry, which is exactly the window in which the loop stops.

**Fix.** `ProxyThread._drain_and_close()`: cancel every pending task **in rounds**, because cancelling *runs* the loop,
and running the loop runs the `connection made` callbacks that were already queued — each of which creates a new handler
the first round cannot have seen (measured: one round left three handlers behind per shutdown under a probing client).
Then three short settle turns for connections accepted in the last microseconds, then `loop.close()`. It records
`pending_at_stop` and `undrained_at_stop`, so "the drain converged" is a number and not a promise.

**Test.** `test_stopping_while_a_handler_is_pending_does_not_abandon_it` — a black-hole upstream (accepts, never
answers) so a handler is pending on every platform, a client, a probe hammering during shutdown, and the assertion that
nothing the loop knew about was left (`undrained_at_stop == 0`).

**Residual, recorded rather than retried away.** A connection accepted between the loop's last turn and its close still
produces a handler nobody owns, so the *warning* is not asserted; the contract asserted is the drain. Removing the
residue needs the accept path to be closed before the drain, which is what `close()` does — the window left is the
queue between the kernel and the loop.

## Defect 62 — nothing caps concurrent connections, so a burst stops the listener accepting (2026-11-18, open)

**Found by** defect 60's investigation: `socket.accept() out of system resource` with `OSError: [Errno 24] Too many open
files` at `ulimit -n 1024`, while the proxy held one descriptor per in-flight client **and** one per in-flight upstream
connection.

**What happens.** Past the descriptor limit the loop's accept fails, asyncio logs the traceback, and the listener keeps
failing until descriptors free up. Children then see refusals or timeouts even though the process is alive and the
listener is "serving" as far as `is_serving()` is concerned.

**Why it is not fixed here.** A cap is a policy — how many concurrent calls a recorder accepts, what it answers past the
cap (503 with a reason, which is a visible event in the capsule, or a queue with a timeout), and whether the cap is a
flag. That is a decision for the owner, not a drive-by change in a PR about rebinds. Recorded with its reproducer (the
probe defaults are now gentle, so a burst has to be issued deliberately).

**Proposal for the next increment.** A `ProxyServer(max_connections=...)` with an `asyncio.Semaphore`, refusals counted
in `stats` and recorded as a `proxy.overloaded` event so the capsule says what happened; a bench row that runs at the
cap; and a test that drives it at 2x the cap and asserts every child got either service or a documented 503.

## Defect 63 — the probe's honesty test asserted the runner's mood, and only macOS said so (2026-11-18, P3B-03)

**Found by** run `44223453730` at `f57c2ac`, the first time a macOS cell executed the probe tests on this branch:

```
FAILED tests/test_rebind_probe.py::test_no_outage_is_reported_when_nothing_is_refused
AssertionError: {'outage_upper_ms': 43.5..., 'observed_failure': True, 'refused_details': ['timeout'],
                 'probe_answer_timeout_ms': 10.0, ...}
```

**What the sample says.** The break in that test takes nothing away — it sleeps 10 ms and returns — so the listener never
went anywhere. The probe recorded one attempt whose answer did not arrive inside the calibrated 10 ms cap and therefore
called it a 43.5 ms outage. That is the definition working as written: the cap is `max(10 ms, 3 x this machine's median
loopback answer)`, so on a shared macOS runner one scheduler hiccup lands outside it, and the child-side story ("no
answer for 43.5 ms") is true.

**The defect** is in the assertion, not the probe: it demanded `outage_upper_ms <= max(12 ms, 9 x cycle)`, a ceiling
written for a quiet Linux box, so it failed exactly on the machine whose *speed* it was measuring. Its own comment
already admitted a stray failure was "the probe being honest".

**Fix.** The test now asserts what it means and nothing about the machine's speed: a listener that was never closed may
**not** be refused (`"refused" not in refused_details` — a refused connect with the listener up is a defect), a slow
answer is allowed, and the ceiling is the budget (100 ms) rather than a healthy-machine constant. The magnitude claims
stay where they were: the bench, and
`test_every_watchdog_rebind_lands_inside_the_budget_not_the_median_of_them`; the clock's honesty stays in
`test_the_clock_counts_downtime_inside_close_before_it_returns`. Linux and Windows had passed this test all along —
there were simply no macOS samples of it until now.

## Defect 64 — the hook path imported all eleven commands to run one (2026-11-18, P3B-07)

**Found by** measuring `blackbox ingest` the way an agent pays it — a whole interpreter, stdin to file — after macOS 3.11
failed `test_the_ingest_command_costs_the_agent_tens_of_milliseconds` at 948 ms against its 900 ms ceiling:

```
console script ingest  p50 100.8  p95 108.8  max 117.3 ms
```

**Cause.** The front door's dispatch table imported **every** command module eagerly, and `blackbox.ui.server` alone
drags in `http.server`, `ssl`, `socketserver`, `email.*`, `html` and `mimetypes`. Measured as isolated processes on this
box: bare interpreter 10.1 ms, `import argparse` 17.7 ms, `import blackbox.cli.ingest` (everything the command needs)
24.3 ms, **importing the whole dispatch table 96.6 ms**. The hook paid ~72 ms for ten commands it never ran, on the
agent's critical path, for every tool call.

**Fix.** `COMMANDS` is a name → (module, attribute) table and `_load(name)` imports one entry point; nothing is imported
until a command is chosen. Measured after, same command path: **p50 30.3, p95 38.3, max 42.0 ms** — inside the plan's
40 ms p95 gate for the first time on this box (the bench-box row is still owed, so the ticket stays open).

**Tests.** `test_the_ingest_command_costs_the_agent_tens_of_milliseconds` now asserts the **marginal** cost over a bare
interpreter (≤ 80 ms) rather than an absolute ceiling on a shared runner, and
`test_the_ingest_path_imports_only_what_it_uses` asserts the import list itself (`http.server`, `ssl`, `socketserver`,
`email.parser`, the UI, the proxy launcher, the repair/verify/ls/hook/link commands must not be in `sys.modules` after an
ingest). A wall-clock bound on a runner is a claim about the runner; the import list is a claim about our code.

## Defect 65 — a capsule's manifest described a recording it had not made (2026-11-18, P4-03/P3B-03)

**Found by** running the documented four-command path from a **wheel install** against a mock provider that truncates a
stream after 3 of 6 chunks:

```
-- c_1f5b…: 5 events, 0 model frames, capture={'coverage': 'FULL', 'providers': []},
              redaction={'content_mode': 'full', …}
-- c_9ea9…: 11 events, 3 model frames, capture={'coverage': 'FULL', 'providers': []},
              redaction={'content_mode': 'full', …}
    MODEL_REQUEST  {'bbx.model.content': 'hash', 'bbx.model.partial': False}
    MODEL_RESPONSE {'bbx.model.content': 'hash', 'bbx.model.partial': False}
```

Two fields were constants in `rec/recovery.py` rather than facts about the recording:

* `capture.coverage` was `FULL` unless the tape's tail was torn — so a capsule holding **no model call at all** claimed
  full coverage, and `blackbox diagnose` called its completeness `full` and said nothing. A user whose app sets its own
  base URL (the launcher *adopts* a set base URL instead of overwriting it) gets exactly that capsule: the run reports
  `recovered`, the capsule lists five frames, and nothing anywhere says "your calls were never recorded".
* `redaction.content_mode` was `full` for every capsule, while the model frames carried `content: hash` — the report
  told a user "content mode `full`" (the text is in the capsule) when only a hash was. The diagnostic output and the
  evidence disagreed, and the report is the half a user reads.

**Fix.** Both blocks are derived from the frames (`cap/manifest.py` grew `content_modes_of`, `provider_hosts_of`,
`model_call_count`): `coverage` is `NONE` when no model call was recorded, `PARTIAL` when the tail was torn, `FULL`
otherwise; `content_mode` is the single mode the frames carry, `mixed` where they disagree, `unknown` where there are
none; `providers` lists the upstream hosts the requests were addressed to, and `capture.model_calls` counts them. On the
same run afterwards: `coverage: NONE, model_calls: 0` for the empty capsule, and `content_mode: hash, providers:
['127.0.0.1'], model_calls: 1` for the captured one — with the report now printing `content mode hash`, and `diagnose`
saying **"no model call was recorded in this capsule"** as an observed line, plus the actionable cause
(`blackbox run --force-proxy`).

## Defect 66 — a stream the provider cut short was recorded as a finished one (2026-11-18, P3-05/P5-pre)

**Found by** running the documented path against a mock provider that truncates after 3 of 6 chunks, with a real
`openai` client: the SDK saw a clean end of body, the script exited 0 and printed three chunks, and the capsule recorded
`MODEL_RESPONSE {..., 'partial': False}` followed by `blackbox diagnose` saying *"nothing in these frames indicates that
anything was cut short"*.

**Cause.** `cap_sdk/tee.py`: a clean `StopIteration` on the inner stream meant `completed=True`, so `partial` stayed
`False` no matter what the SSE body did or did not contain. An OpenAI stream that never carried `data: [DONE]`, and an
Anthropic stream that never carried `message_stop`, were byte-identical, from the seam's point of view, to streams that
finished. **This is the failure mode the recorder exists for**: a provider timeout, a gateway 502 mid-stream or a dropped
link leaves an application holding a short answer with no exception, and the capsule used to agree with it.

**Fix.** `TerminatorWatch` in the tee: it keeps the last 128 bytes of the stream (a marker can straddle two chunks, which
chunk-by-chunk testing would miss), knows only two conventions — `[DONE]` for `/chat/completions` and `/completions`,
`message_stop` for `/messages` — and on a clean end **without** the marker sets `partial = True` and records why in a new
`end_reason` (`ModelCall.end_reason`, written on the response frame as `bbx.model.end_reason`). *Deliberately
conservative*: a generic `text/event-stream` has no terminator in its specification, so an unknown path is never marked —
that would be a guess about somebody else's protocol.

**Diagnosis side (defect 67 below).** `diagnose` now emits an **observed** finding for a `MODEL_RESPONSE` whose own
`partial` flag is set (`stream-ended-short-<node>`), quoting the recorded `end_reason`, and the report's headline says
"a model stream ended before the provider finished it" instead of denying that anything was cut short. Measured on the
same reproduction afterwards: `partial: True`, `end_reason: the stream ended without the provider terminator ([DONE]);
the body was closed by the other end, not finished`, and two observed findings in the report.

**Tests.** `tests/test_stream_termination.py` (11): a stream without `[DONE]` is partial and says why; with it, complete;
a marker split across two chunks is still seen; the same for `message_stop`; four unknown paths are never marked; the tail
stays 128 bytes over 1 MB of stream; the table names what it covers. `tests/test_diagnose.py` adds the observed finding,
the quiet case (`partial: false` produces nothing) and the manifest assertions below.

## Defect 67 — the diagnosis read every model frame as an empty dict (2026-11-18, P5-pre)

**Found by** looking for the finding defect 66 should have produced and not finding it: `diagnose/evidence._decode` tried
`cbor.decode_exact` only, while the model frames (request, chunk, response, error) carry **JSON** payloads. Every rule
that reads those attributes — interrupted streams, provider errors, capture seams, `partial` — was reading `{}`, and the
report's timeline rendered `MODEL_REQUEST — 312 B` with no provider, path, model or stream flag: the frames the report is
*about* were the ones it could not read.

**Fix.** `_decode` tries JSON first (when the payload starts with `{`) and falls back to CBOR, the same order
`cap/export_jsonl._payload_of` has always used. After: the timeline prints
`provider=127.0.0.1; model=gpt-4o-mini; path=/v1/chat/completions; stream=True; partial=False; capture_seam=httpx-hooks`
for the request and `partial=True; status=200` for the response.

**Test.** `test_a_json_payload_is_evidence_and_was_invisible_before_defect_67` builds a capsule whose model frames are
JSON (as the seams write them) and asserts the attributes and the rendered summary.

## Defect 68 — a stream whose ending this recorder does not recognise was reported as one it had verified (2026-11-16, B1 follow-up)

**Found by** asking what defect 66's fix does for the providers it does not name. It knows two conventions and refuses
to guess at any other, which is right — a generic `text/event-stream` has no terminator in its specification, so marking
one would be a guess about somebody else's protocol. What was wrong is what the capsule *said* while refusing: the only
recorded fact was `partial: False`, and `partial: False` is exactly what a stream the provider finished says. So a Gemini,
Responses-API or Ollama stream, or any gateway in front of them, was indistinguishable from a verified complete one —
the same failure this project fixed for OpenAI and Anthropic in defect 66, surviving in the providers nobody had tested.
A recorder that cannot tell is allowed to say "I cannot tell". It is not allowed to say "complete".

**Fix.** `cap_sdk/tee.py` records an `ending` with three values, plus the provider whose convention was consulted:

| `ending` | when | `partial` | capsule `end_reason` |
|---|---|---|---|
| `terminated` | a marker of the request path's own convention was seen | `False` | the terminator, named |
| `unterminated` | the convention is known and the marker is missing | `True` | which marker was expected and not seen |
| `unverified` | no convention is known for this path | `False`, and no claim made | "whether the stream finished is not recorded" |

`TERMINATORS` is a table of `(path suffix, marker, provider)`: OpenAI chat and legacy completions `data: [DONE]`,
OpenAI Responses `response.completed`, Anthropic `message_stop`, Gemini `"finishReason"`, Ollama `"done": true` for both
`/api/chat` and `/api/generate`. A marker belonging to *another* provider's convention in the body does not count. The
ending and the provider travel on the response frame (`bbx.model.ending`, `bbx.model.ending_provider`), so `diagnose`
can act on them without re-reading bytes: a new finding `stream-ending-unknown-<slot>` (verdict `CANNOT_TELL`, confidence
`LOW`, title *"whether the stream at node N finished is not recorded"*) names the provider that was not recognised and
lists what would settle it.

**Why the third state and not a boolean.** The user's requirement is the sentence, not the flag: *never claim a stream is
complete when its ending is unknown*. A boolean forces "complete" and "cut short" to be the only two things a capsule can
say, so the honest answer for an unrecognised provider has nowhere to go. The cost is one extra value on a frame; the
benefit is that `diagnose` can print "cannot tell" instead of nothing, and a future provider is one table row, not a
change to the frame format.

**Tests.** `tests/test_stream_termination.py` (23): every row of the table terminated by its own marker; a missing marker
is `unterminated` and partial with a reason; a marker from another provider's convention is `unterminated`, not
`terminated`; a non-streaming JSON body is not a stream and is not judged; an unrecognised path is `unverified`, is not
partial, and its reason says the ending is not recorded. `tests/test_diagnose.py` (38) adds
`test_an_unknown_stream_convention_is_a_cannot_tell_not_a_silence`, which writes a capsule whose response frame carries
`ending: unverified` and asserts the finding and the "not recorded" wording.

## Defect 69 — at the connection ceiling the proxy had no answer at all, only whichever failure came first (2026-11-16, B3)

**Found by** measuring the burst that B3 describes instead of reasoning about it: at 450 concurrent requests with the
stock 1024-descriptor soft limit, `accept` failed with `socket.accept() out of system resource` and a request issued
during the burst **timed out after 4 s with no answer**. Raising the limit (`proxy/limits.py`) moved the cliff to 1366
descriptors and served the probe in 2 ms, which is why that half was not the blocker: what remained was that the ceiling
was still whatever the descriptor table happened to be, and what happened at it was a log line the child never sees.

**Decision.** Backpressure, with an explicit ceiling above it — the recommended option, because the proxy's promise is
pass-through and a recorder that refuses a request the provider would have answered has broken the thing it exists for.
A request past `MAX_INFLIGHT` (200, §15.1's concurrent-stream count) waits for a slot and is counted; a wait past
`QUEUE_WAIT_S` (60 s) is answered `503` with `x-blackbox-error: queue-timeout`; a client past `MAX_CONNECTIONS` (512) is
answered `503` with `x-blackbox-error: connection-limit` in milliseconds, because that is the one case the recorder
genuinely cannot serve. The numbers are derived, not preferred: `512 + 200 + 96 = 808 < 1024`, so the caps hold on a host
where `limits.py` could not raise anything. `tests/test_proxy_backpressure.py` asserts the arithmetic.

**Measured after** (`bench/bench_connection_limit.py` → `bench/results/2026-11-16-b3-connection-limit.json`): 450 of 450
requests answered `200`, 0 refused, 444 queued, longest queue wait 887 ms, origin peak 192 in flight; the probe during
the burst answered `200` in 1053.8 ms; 96 clients beyond the connection cap each answered `503` with the reason, median
4.4 ms, max 7.7 ms.

**What the user reads.** `ProxyStats` gains `queued`, `max_queue_wait_ms` and `refused_reasons`, and the launcher prints
one sentence after the run's summary when any of them is non-zero — naming what was refused, what waited, and that the
numbers are a descriptor budget rather than a preference. Deliberately silent when nothing happened.

**What is still open, and is the owner's.** Whether the caps become flags (a safe range is one budget interval, so a flag
needs its own validation story), and whether a `503` the recorder issued belongs in the capsule as a frame — today it is
in `stats` and in the run's sentence only.

## Defect 70 — a convention was matched by substring, so a listing endpoint was judged as a stream (2026-11-16, defect 68 follow-up)

**Found by** writing the test for an unfamiliar format and asking what `/v1/messages/batches` would be. It answered
`anthropic`. `expected_terminators` tested `suffix in path`, so:

* `/v1/messages/batches` — Anthropic's batch-listing endpoint, which returns a JSON object and no stream at all — was
  judged against `message_stop`. If it had arrived as a stream and ended without one, the capsule would have said the
  stream was **cut**, which is a claim about a call that has no stream to cut. The mirror image of the defect 68 mistake,
  in the other direction, and the same root cause: a convention belongs to an *endpoint*, and the endpoint is the end of
  the path.
* `/v1/chat/completions` matched both the `/chat/completions` and the legacy `/completions` row, so one path carried two
  markers and `expected_terminators` returned `[b"[DONE]", b"[DONE]"]`. Harmless by luck (same marker, same provider) and
  wrong by construction.

**Fix.** `convention_for(path)` strips the query string and requires `path.endswith(suffix)`, choosing the longest matching
suffix. One path, one row, one provider. `/v1/messages/batches`, `/v1/chat/completions/extra` and `/v1/responses/input_items`
now resolve to **unrecognised**, i.e. `unverified`, i.e. no claim.

**Tests.** `tests/test_stream_termination.py` (30): the table test asserts one marker per path and that a query string does
not change the answer; `test_a_path_that_only_contains_a_known_suffix_is_not_judged_by_it` covers the three paths above;
and `test_an_unfamiliar_format_is_recorded_as_unknown` adds four endpoints this build has never had a row for — Cohere
`/v2/chat`, Mistral `/v1/chat` (which does emit `[DONE]`), a Bedrock
`invoke-with-response-stream` envelope, and a gateway path — each asserted `unverified`, `unrecognised`, not partial, and
with the reason that says the ending is not recorded.

## Defect 71 — the caps were a plan for one host, printed on every host (2026-11-16, B3 platform review)

**Found by** walking the arithmetic across the platforms this project actually runs on. `512 + 200 + 96 = 808 < 1024` is
true of a Linux box with the stock soft limit, and it was written down as though it were true everywhere. It is not:

* a **macOS** shell starts at a soft limit of **256** (the number in `proxy/limits.py`'s own docstring), and
  `raise_fd_limit` can be refused — for instance under a hard limit the user cannot pass;
* a container or CI sandbox can have any hard limit at all, including ones below the slack;
* **Windows** has no `RLIMIT_NOFILE` at all, so the number the sentence was derived from does not exist there.

On the first two, the proxy would have promised 808 descriptors it did not have: the caps would have *been* the cliff, with
a documented number in front of it.

**Fix.** `limits.fd_budget()` reads the soft limit the process has *after* the raise attempt (`None` on Windows and for an
unlimited soft limit), and `server.plan_caps()` derives the pair from it — shrinking to fit, stopping at floors (16
connections, 8 in flight) when the budget is below what the proxy needs, and saying so in `ProxyStats.caps["why"]`. The
documented 200/512 is now what a 1024-descriptor host gets, not a constant every host is told.

**Measured, end to end, on a host that cannot raise its limit** (hard 200, so `setrlimit` returns 200→200): caps planned at
**62 connections + 42 in flight + 96 = 200**, 8 concurrent requests through the real listener, **8 answered `200`**.

**What the user reads now.** The run's sentence is built from `stats.caps`, so it prints the numbers that were in force —
`the ceilings are 62 in flight and 42 connections…` — or, on Windows, that they are policy numbers because the platform
publishes no budget. A rejection also carries a body line for the developer whose request it was
(`this is the recorder's ceiling, not the provider's: retry when its in-flight requests finish, or run fewer at once`), and
a delay is labelled a delay rather than folded into the same count.

**Tests.** `tests/test_proxy_backpressure.py` (7) covers the plan table row by row (65536, 1024, 256, 128, 64, `None`),
asserts `connections + inflight + slack <= budget` wherever a fitting pair exists, and asserts that a server built on a
256-descriptor host uses the plan and publishes it; `tests/test_proxy_limits.py` (7) reads the budget in a subprocess at
200 descriptors and asserts the plan fits. `tests/test_launcher_capture_warning.py` (5) asserts the sentence separates
delay from rejection and that the Windows wording makes no arithmetic claim.

## Defect 72 — the plan spent descriptors the process had already opened (2026-11-16, B3 review round 2)

**Found by** asking what `plan_caps` means on a host that is already busy: a `blackbox run` process holds the tape, the
child's inherited handles, the SDK's sockets and everything the application opened before the listener exists. The plan
subtracted `PROXY_LOCAL_SLACK_FDS` (96) and nothing else, so on a 256-descriptor host whose process already held 60 the
arithmetic was `96 + 64 + 96 = 256` — of a budget with 60 already gone. The slack was being spent twice, and the caps
would have been the cliff again, this time with the arithmetic to prove it.

**Fix.** `limits.open_fds()` reads the count the platform exposes (`/proc/self/fd` on Linux, `/dev/fd` on macOS and the
BSDs, `None` on Windows), and `plan_caps(budget, in_use=...)` subtracts it: `room = budget - slack - in_use`. The count is
a lower bound — the scan holds one descriptor of its own — which errs in the safe direction, and `ProxyStats.caps`
publishes `open_at_start` plus a sentence that names the number when it is non-zero.

**Measured** (this sandbox): 40 real descriptors held open plus the interpreter's own 10, budget 320 →
`plan(320, in_use=50) = 104 connections / 70 in flight` (`104 + 70 + 96 + 50 = 320`), 12 concurrent requests through the
listener, **peak descriptor count 87, all 12 answered `200`**. The test asserts the peak against the budget rather than
against a constant, so a future change that spends the slack twice fails here.

**Tests.** `tests/test_proxy_backpressure.py` (14): `test_descriptors_already_open_come_out_of_the_same_budget` (the
arithmetic, including that a host with room to spare keeps the documented pair however much is open),
`test_the_listener_counts_descriptors_already_open` (the wiring and `stats.caps.open_at_start`) and
`test_a_host_with_files_already_open_stays_inside_its_budget` (40 real pipes, a real burst, the peak measured).

## Defect 73 — overload behaviour was tested at hand-picked numbers, and disconnects were not tested at all (2026-11-16, B3 review round 2)

**Found by** reading the test list against the ways a real overload arrives. The suite proved the *behaviour* at
`max_inflight=2` and `max_connections=2` — good, and necessary — but never at a **planned** ceiling, never with the
process already holding descriptors, and never with a client that **leaves** while queued, at the ceiling, or mid-stream.
Three of those are the cases a hung recorder would produce if a slot or a connection leaked, and nothing would have caught
it.

**What the new tests measure, and what they found.**

| case | result |
|---|---|
| 80 concurrent requests at the 256-descriptor plan (96 connections / 64 in flight) | **80 of 80 answered `200`**, 0 refused, 73 queued, longest wait **50 ms**, 119 ms wall |
| 12 concurrent requests on a 320-budget host with 50 descriptors already open | **12 of 12 answered `200`**, plan 104 / 70, **peak 87 descriptors** |
| a client that closes while queued for the only slot | slot returned; the next request is served `200`; nothing refused |
| a client that closes at the connection ceiling (before reading its `503`) | connection count drains to 0; the next request is served `200`; `refused_reasons["connection-limit"] == 1` |
| a client that closes mid-stream (one in-flight slot) | `client_gone` recorded; the next request is served `200` |

No leak was found. One **limitation** was: a client that disappears while queued cannot be noticed until its turn comes,
because the recorder is parked on the semaphore rather than reading that socket — so the slot is returned only then, and
the abandoned request may already have reached the provider. That is now written down in the README and in
`docs/RELEASE-BLOCKERS.md` rather than left as an implication.

**Tests.** `tests/test_proxy_backpressure.py` (14), including the mid-stream case, which needed an origin that streams
slowly (`GatedOrigin(stream=True)`: chunked SSE, one chunk every 50 ms) so a disconnect can land inside the response.

## Defect 74 — the verifier refused the command a user types after `ls` shows two capsules (2026-11-16, B8)

**Found by** following the blocker list rather than the code: `blackbox verify capsules/*.bbx` — the natural next command
after a run leaves two capsules with one run id — was `error: unrecognized arguments`, because the positional was
`nargs="?"`. On a shell that does not expand patterns (`cmd.exe`, PowerShell) the same habit arrives as a literal path and
would have been read as a missing file.

**Fix.** The positional is `nargs="*"`, and the command expands a pattern itself with `glob`, so one line works on every
platform. Every capsule is verified and printed; the exit code is the **worst** outcome of the batch (9 any check failed,
2 any target unreadable, 0 all verified), which keeps the plan's three codes meaningful when more than one capsule is
named. A pattern that matches nothing is reported as a warning when another argument matched — a typo must not silently
shrink the set being verified — and is a `2` on its own.

**Measured** on a real run's two capsules: quoted pattern → `2 capsule(s) verified`, exit 0; shell-expanded paths → the
same; one tampered capsule in the batch → exit 9 and only that capsule named; one unreadable path → exit 2 with
`cannot read` and the path.

**Tests.** `tests/test_verify_cli.py` (23, five new).

## Defect 75 — every sink-written `NODE_OPEN` reached `graph.json` with no name, no kind and no owner (2026-11-18, B4)

**Found by** reading the first B4 measurement back from the tape instead of from a counter: the proxy's spans were on the
tape, `ProxyStats.spans` said `2`, and the graph listed two nodes as `{kind: None, owner: None, name: None}`.

**Cause.** `TapeSink._safe` encodes payloads with `json.dumps`, while `TapeWriter.emit` — used directly by the interpreter
writer and the hook folder — stores CBOR. `recovery._payload` decoded **CBOR only** and returned `{}` for everything the
sink wrote, so a sink-written node lost its identity at read time. The frames were never wrong; the reader was.

**Fix.** `_payload` tries CBOR, then JSON, and returns `{}` only when neither reads it. A payload that is neither is still
an empty dict rather than an exception: a graph entry is not worth a crash, and an unreadable node is visible as a node
with no name.

**Why it mattered for B4 specifically.** A node-level edge whose ends have no name or kind is an edge naming two numbers
— indistinguishable from an invented node to anyone reading `graph.json`. A proxy span that reads as `{kind: null}` would
have been the blocker's own failure mode wearing a passing count.

**Measured** by `tests/test_proxy_spans.py` (the tape is scanned and `recovery.graph_nodes` is asked for the span's name,
kind, owner, outcome and duration) and by the launcher suites, which read `graph.json` from a real run.

## Defect 76 — a span's own `name` field made `SinkRecorder` fail silently, and fail-open hid it (2026-11-18, B4)

**Found by** the same measurement: the tape carried a `RUN_START` and no span, and the proxy's own stats said
`spans: 0, record_errors: 1` while every request still answered `200`.

**Cause.** `SinkRecorder._forward(self, name, *args, **kwargs)` — a span's payload has a field called `name`, so
`_forward("span_open", slot, name="POST /v1/chat/completions", …)` raised `TypeError: got multiple values for argument
'name'`. The sink counts every exception as a failed write and never re-raises (fail-open is the design), so the request
succeeded, nothing was recorded, and no test failed: the only symptom was a number in a stats dict that nobody asserted
on.

**Fix.** The parameter is `attr`, and `_forward` now returns whether the frame reached a sink; the proxy treats `False` as
"no span" instead of handing the call a slot no frame carries.

**Measured** by `tests/test_proxy_spans.py::test_a_span_wraps_the_call_and_every_frame_carries_its_slot` (frame order and
one slot per frame) and `::test_the_proxy_counts_the_spans_it_opened` (`spans: 1`, `spans_capped: 0`, no record errors),
both read from a real tape written by a real proxy with a real upstream.

## Defect 77 — every proxied call was recorded as a cut stream (2026-11-18, found by the fresh-install journey)

**Found by** running the user journey: a proxied 404 arrived complete under its `content-length`, and `blackbox diagnose`
reported — as `observed`, confidence high — *"the response stream at node N ended before the provider finished it"*.

**Cause.** `ProxyServer._open_call` passed `partial=True` for every call it opened, unconditionally, and nothing ever
cleared it. `partial` is the flag that says "the writer saw part of this call"; on a body read to its own framing it is
false, and the proxy is one of the few writers that *can* know: `_relay_body` already returned whether the body ended
inside its `content-length` or at a chunked terminator (it used the answer only to decide whether the upstream socket
could be pooled).

**Fix.** The proxy closes each call with what it observed: a body that ended inside its framing is
`partial: False, ending: "terminated"`; a body that ended at the connection's close — no length, no chunked terminator —
is `partial: True, ending: "unverified"` with a reason that says the question is unanswered rather than answered; a
client disconnect and an upstream cut each carry their own reason. `Receiver.close_call` grew the three optional fields,
and `FailOpenReceiver` probes the inner receiver's signature so a receiver written before them still works (defect 80).

**Measured** by `tests/test_proxy_spans.py::test_a_body_that_ended_inside_its_content_length_is_not_a_partial_call` and
`::test_a_body_that_stopped_before_its_content_length_is_partial_and_unverified` (a real proxy, a real upstream that
promises 100 bytes and sends 10), and by the journey report, which lost the false finding.

## Defect 78 — a plain JSON response was judged against the SSE terminator table (2026-11-18, found by the journey)

**Found by** the same run, one layer down: the **app's** capsule, from the SDK seam, recorded `partial: true` and
`end_reason: "the stream ended without the provider terminator ([DONE])"` for a `chat.completions.create(...)` that was
not a stream at all — so the report said a stream had been cut that never existed.

**Cause.** `TeeStream._finish` ran `TerminatorWatch` — the SSE table — over the body of *every* response, because the
tee wraps `response.stream` whether or not the call asked for a stream. A JSON body has no `data: [DONE]`, so the watch
concluded the terminator never came.

**Fix.** The rule is about the call, not the bytes: `call.stream` (which the seam sets from the response's
`content-type: text/event-stream`) decides. A non-streaming body that reached the end of its iteration ended inside its
framing — httpx raises rather than returning a short read of one — so it is recorded `partial: False, ending: None`, which
is what "this was not a stream" means in the format. A streaming call keeps the three-valued answer unchanged.

**Measured** by `tests/test_stream_termination.py::test_a_non_streaming_json_body_is_not_a_stream_with_a_missing_terminator`
and `::test_the_same_bytes_are_still_judged_as_a_stream_when_the_call_says_so`, and by a real `openai` 3.51.0 call in the
journey.

## Defect 79 — the report did not mention the failure the user had (2026-11-18, found by the journey)

**Found by** reading the journey's incident report: the app had crashed on a 404 with the exception text on the tape, and
the report's findings were about *identifiers* — nothing said the provider had answered 404, and nothing said the step
had ended with an error. The evidence table showed `status=404` and `outcome=error`; no rule turned either into a finding.

**Fix.** Two producers in `diagnose/findings.py`: `provider-status-N` (`observed`, high) when a terminal `MODEL_RESPONSE`
carries a status ≥ 400 — with the model named, the provider's-body caveat, and three alternatives — and
`node-outcome-error-N` (`observed`, high) when a node's own `NODE_CLOSE` says `outcome: error`, quoting the recorded
error text. Their checks do not advise a blind retry: a 4xx repeated unchanged is refused unchanged, and a failed node's
cause is the finding above it, not a second attempt.

**Measured** by `tests/test_diagnose.py::test_a_provider_error_status_is_an_observed_finding`,
`::test_a_node_that_closed_with_an_error_is_an_observed_finding`, and the journey, where the two reports now name the 404
and the failed step — and where the *post-repair* run's report names neither.

## Defect 80 — a receiver written before the ending fields failed open into a `TypeError` (2026-11-18, same increment)

**Found by** the fail-open suite after defect 77's change: `FailOpenReceiver` passed `partial`/`ending`/`end_reason` to
any inner receiver, so one with the older `close_call` signature raised `TypeError`, which the wrapper counted as a
recorder failure while the application carried on — a call that closed with no ending recorded and a blame line pointing
at the recorder.

**Fix.** The wrapper asks the signature which of the three names the inner receiver takes and passes only those, the same
capability probe it already used for `call_id` and `node_slot` on `open_call`.

**Measured** by `tests/test_capture_failopen.py` (`test_a_receiver_written_before_d053_still_records_its_calls` and the
hostile-receiver cases, which now report no failures for a receiver that raises somewhere else).

## Defect 81 — a test slept instead of waiting, and CI found it on the one runner that was busy (2026-11-18, CI `228097f`)

**Found by** run 44417127703, one red cell out of ten: `py3.13 / windows-latest` failed
`tests/test_proxy_supervisor.py::test_stopping_while_a_handler_is_pending_does_not_abandon_it`. The product was not at
fault and no bound in it changed: the test began with `time.sleep(0.05)` to "let the handler start", then asserted that a
stop does not abandon a pending handler. On that runner 50 ms was not enough for the handler thread to be *in* the
handler, so the assertion was reached with nothing pending — the failure was in the precondition, and a pass would have
been equally meaningless (it would have proven nothing about abandonment).

**Cause.** A sleep used as a synchronisation primitive: it races on a loaded machine and says nothing when it loses.

**Fix.** The fixture sets `held` from inside the handler, and the test waits on that fact with a ceiling
(`Event.wait(timeout)`) and an explicit `assert` if the wait times out, so a slow machine now fails with "the handler
never started" instead of mis-reporting the product. `tests/test_proxy_supervisor.py` is 12 tests, 0 failures, on three
consecutive local runs.

**Not the fix.** Raising the sleep, or relaxing the abandonment assertion. The rule from `docs/STATUS.md` holds: a
timing-shaped failure is re-run and diagnosed before it is called a flake, and an assertion is never weakened to make CI
green.

## Defect 82 — a test whose own timeout was longer than the harness's could only ever be killed, never report (2026-11-18, CI `d842397`)

**Found by** run 44461898689: `py3.11 / macos-latest` failed after the 120 s watchdog killed the step inside
`tests/test_proxy_limits.py::test_a_burst_larger_than_the_old_soft_limit_is_still_served`, at
`selectors.py:415 in select`. The Windows py3.13 cell that reddened `228097f` passed in the same run, so the supervisor
repair worked; this was the next thing underneath it.

**Cause.** Two bounds in the wrong order. The test spawns a child that makes 40 sequential
`socket.create_connection(..., timeout=5)` calls — 200 s worst case — and the test's own `subprocess.run(timeout=180)`
sat *above* the 120 s per-test watchdog in `tests/conftest.py`. So when the burst stalled on that runner, the harness
killed the process before the child's timeout could fire, which means the one thing that would have explained the stall —
the child's stderr — was never collected. A bounded test that cannot say what it saw is a hang with extra steps.

**Fix.** One deadline bounds every wait in the child (connections, the wait for the upstream to hold them, the probe), a
failed connection is **recorded rather than raised** so the assertion carries the numbers, the `time.sleep(0.5)` before
counting `upstream_holds` became a bounded poll on the fact it was waiting for (defect 81's lesson), and the child's
report now includes `clients`, `connect_failures`, `upstream_holds`, `probe` and `elapsed_ms`. The outer timeout is 60 s —
**below** the watchdog — so an overrun is reported by an assertion that knows what the burst was doing. No assertion was
relaxed: 40 held upstream, a probe served from the middle of the burst, and the soft limit above 64 are still required.

**Measured.** `tests/test_proxy_limits.py` is 7 tests, 0 failures, on three consecutive local runs, with the burst row
taking 0.17 s here. What the macOS stall *was* is still unknown: the platform is not available in this sandbox.

**What the CI runs then showed, and what they did not.** The same test hung at the same 120 s watchdog in `d842397`'s
py3.11/macos cell, then in `fa5d616`'s **PR** run — while `fa5d616`'s **push** run was ten of ten with that cell green. One
commit, two runs, two outcomes: that is the strongest available evidence that the defect is a test that can hang rather
than a product that hangs. With the repair in, `af6023f`'s push and PR runs are both ten of ten, macOS included. That does
**not** prove the stall is gone — it is intermittent, and two green samples cannot show a race is fixed. What it does prove
is the property this defect was about: the child now bounds every wait with one deadline, prints what it saw, and its
timeout sits below the watchdog, so the next stall arrives as a failing assertion with numbers instead of a kill.

## Defect 83 — a hostile manifest reached the reader as a Python exception, not as a code (2026-11-19, P4-04)

**Found by** writing the attack corpus rather than reading the validator. Two capsules got past `read_capsule`'s
promise that a hostile capsule is refused **with a code**:

* `manifest.json` = `[]`, `"x"` or `null` — valid JSON, so `json.loads` returned a list, a string or `None`, and the
  `.get("format")` on the next line raised **`AttributeError`**. `blackbox verify` printed
  `cannot read …: AttributeError: 'list' object has no attribute 'get'` and exited 2: the right exit code with the wrong
  reason, and nothing a user could look up.
* `manifest.json` nested 20 000 deep — valid JSON, and `json.loads` raised **`RecursionError`** out of the reader. The
  same shape of failure: a Python exception where the format's table says `BBX-E407`.

**Cause.** The manifest path assumed its input had the shape the code needed. `cap/manifest.validate` already refuses a
non-object (its `isinstance` guard was marked "unreachable, `read_capsule` parses JSON"); the caller had no such guard,
so the check that exists was never reached.

**Fix.** `read_capsule` catches `RecursionError` alongside the decode errors, and refuses a non-object manifest with
`BBX-E407` **before** any `.get`. The validator's own guard is now reachable rather than ornamental.

**Test.** `tests/test_capsule_attacks.py`, the param case `manifest that is not an object` (six bodies including the two
above), plus a CLI-level case asserting that `blackbox verify` prints `BBX-E407` and never `AttributeError`.

**Also added in the same corpus**, because writing the corpus is what a corpus is for: `BBX-E406` for an encrypted or
bzip2 member (previously decodable), `BBX-E403` for a symlink/device/fifo/socket member (previously accepted, harmless
because nothing is extracted, and still a capsule pretending to be a filesystem), `BBX-E408` for a manifest whose
`derived_parts` names an entry the archive does not carry, and a bounded per-entry read (`_read_entry`) that is the
§13.4 check-9 counter — a second net behind `zipfile`'s own declared-size bound, tested against a lying archive because a
real one cannot reach the branch.

## Defect 84 — a test asserted a machine-wide invariant inside a shared window (2026-11-19, P4-01)

**Symptom.** `test_launcher_attach.py::test_the_temp_dir_is_removed_even_when_the_child_is_killed` failed in a full-suite
run — and only there: it passed 3/3 on its own, 2/2 with the four other launcher files in one process, and again in the
next full run. The assertion that failed was `_sdk_dirs() == before`, where `_sdk_dirs()` globs **the machine's** temp
directory for `blackbox-sdk-*`.

**What actually held.** Every claim about the thing under test was true in the failing run: `exit_code` was the kill
code, `result.sdk["removed"] is True`, and the launcher's own `site_dir` did not exist. The directory the assertion
tripped over (`/tmp/blackbox-sdk-or5c5xmy`) was gone by the time the run ended — it belonged to a launch that was still
in flight in another process, which removed its own directory a moment later.

**Measured.** Instrumenting `tempfile.mkdtemp`/`shutil.rmtree` for a whole suite run: 41 `blackbox-sdk-*` directories
created in the pytest process, 41 removed, **0 orphans**. The litter the assertion feared is not what it found.

**Fix.** The three tests that asserted `== before` now call `_no_new_sdk_dirs(before)`, which waits up to 3 s for the
machine to settle and then asks whether anything *remained*. A real leak does not clean itself up: it still fails. What
changed is that a concurrent process's short-lived directory is no longer this test's failure.

**Not fixed, because it is not a defect:** the launcher's own cleanup path. `cleanup_sdk` keeps the path and reports
`removed`, so "where it was, and whether it is gone" stays checkable afterwards.

## Defect 85 — the format document was not enough to read the format (2026-11-19, P4-12)

**Found by** writing `tools/readers/bbx-read.mjs` from `docs/format.md` alone. Three places cost real time, and each is a
place where a second implementation would have gone wrong in the same way:

1. **§4's table puts the trailer at the wrong offset.** The rows read `48+payload_len | 4 | BBXT` and
   `52+payload_len | 4 | tail_len`, but the frame's payload is zero-padded to a multiple of 8 and the write order is
   `payload+padding → trailer → header`, so the trailer is the frame's **last 8 bytes**. The first real frame a reader
   sees is 80 B with a 21 B payload: `48 + 21 = 69`, where the table says the trailer starts, and `80 - 8 = 72`, where
   it is. The document now says so, with the arithmetic.
2. **§8.1 never names the manifest's identity values.** `format` is the string `bbx.capsule` and `format_version` is the
   integer `0`; a reader that assumed `bbx` and `1` refused a capsule this build writes, and one that assumed nothing
   could not tell a newer writer from a corrupt file. The values are now in §8.1, with the rule that a reader refuses a
   different format or a higher version as `BBX-E407`.
3. **§8.1 does not specify `INTEGRITY.sha256`.** The line format (64 lower-case hex, two spaces, the name as it appears
   in the archive, `LF`, sorted, final newline) and the fact that `manifest.integrity.entries` counts **all** members —
   not the lines — are both written down now. A reader that had assumed "entries = lines" was off by two.

**Also amended:** §5's segment header checksum was described as "header checksum (profile)" without naming the
function. It is CRC-32/ISO-HDLC over bytes `[0, 96)`, and the Node reader now verifies it on every segment.

**What this defect says about the others.** Every one of these was invisible from inside the Python implementation,
because the Python reader does not consult the document — it *is* the document. `tests/test_independent_reader.py`
(16 tests) now holds them together: same entries, same root, same frames, same refusal codes on ten hostile inputs, and
one disagreement about severity that turned out to be a disagreement about design — a bad root is exit 4 with a reason
in the Node reader and `root_ok: false` on a capsule that still opens in the Python one, which is what a user with a
drifted file actually needs.

## Defect 86 — two tests written on Linux broke the Windows cell (2026-11-19, `47ecb8c`)

CI on `47ecb8c` was green on all three Ubuntu cells and green on `docs`, and **py3.11/windows-latest failed in the unit
step** with two errors that neither the new reader nor the corpus has anything to do with on POSIX:

* `test_the_reader_runs_on_a_machine_with_no_blackbox_installed` built its environment by hand
  (`PATH=/usr/local/bin:/usr/bin:/bin`) to prove the Node reader leans on nothing of ours. On Windows `node` is in
  `C:\Program Files\nodejs`, so the hand-built PATH found no interpreter and the test failed on process start. The
  environment is now this host's minus every `PYTHONPATH`/`BLACKBOX*` entry: what the test is about is the absence of
  *our* variables, not the absence of PATH.
* `test_check_7_…[deeply nested-[[[[[…` — pytest derived the test id from the parameter **body**, and the 40 kB
  bracket string became a test name long enough that collection errored on Windows. Every case in that table now carries
  an explicit id.

**What this says about the corpus and the reader:** their Ubuntu cells were green, and neither failure would have been
visible from this machine. It is the third time a POSIX-shaped assumption in a *test* (not the product) cost a Windows
cell — after defect 81's sleep and the `SIGKILL`-as-137 assertion — which is why the Windows cells are read as part of the
signal rather than as a formality.

## Defect 87 — the fallback announced its URL after the wait it was waiting for (2026-11-19, the desktop shell)

Two bugs in the new `blackbox desktop`, both found by its own tests before either shipped:

* **The browser fallback deadlocked.** `run(path, window=False)` served the page and then waited for a fetch, while the
  URL — the only thing a user needs — was printed by the caller *after* `run()` returned. So the fallback could only be
  used by someone who already knew the URL: it waited for a fetch nobody could make, for the full 900 s timeout. The
  test that caught it hung, which is what a deadlock looks like from the outside. Fixed by passing an `announce(url)`
  callback into `run()`, called before the wait, and by printing that line flushed — the same failure `blackbox serve`
  had (defect 40) arriving again in a new caller.
* **The byte count was a character count.** `report["bytes"]` was `len(page)`, and the page carries em dashes and
  ellipses: 56 536 characters, 59 951 bytes on the wire. A test comparing the served body with the reported size is what
  noticed; the number is now `len(page.encode("utf-8"))`, because a size a user reads should be the size of the file they
  would save.

Neither reached a release. Both are recorded because the pattern is the one worth remembering: a shell around an
existing component reintroduces the bugs that component already paid for, unless it is tested the same way.

## Defect 88 — the library's fast path was a second, weaker reader (2026-11-19, the desktop library)

`read_manifest` exists so the library can list a hundred capsules without decompressing a hundred archives. Its first
version made every **header** check `read_capsule` makes and then stopped, and `tests/test_cap_read_manifest.py` — which
runs the whole hostile corpus through both readers and compares the refusal code — found the gap on its first run:

* **A manifest that lists a part the archive does not carry was accepted by the library reader.** `read_capsule` refuses
  it as `BBX-E408` (§13.4 check 8: the hashes verify over the entries that exist, so only a check of the *claim* about
  what exists can catch it). The library would have drawn that capsule as a healthy row and the window would have failed
  when it was opened. The check is a set lookup, so `read_manifest` now makes it too, through one shared function; the
  test that caught it is `test_both_readers_refuse_with_the_same_code[manifest lists a part the archive lacks]`.

Two more bugs the same tests found before the pages shipped:

* **A readable row carried an "unreadable" reason.** `_row_from_manifest` computed the attention list while the row still
  said `readable=False`, so every healthy capsule in the library was labelled `unreadable: no reason given`. The flag is
  now set in the row's constructor and the reasons are computed once, after it.
* **A capsule file opened a library of nothing.** `blackbox desktop path/to/capsules/x.bbx` took the capsule's own
  directory as the data directory, so the app scanned `capsules/capsules/` and served 404 for the run it had just been
  pointed at. `library.data_root()` now walks up from a file inside a `capsules/` directory, and a capsule outside any
  data directory is served by path instead.

**What this says.** The optimisation was correct about *which bytes it may skip* and wrong about *which checks are
therefore skippable* — a distinction no amount of reading the diff would have settled. The corpus that proved it already
existed for `read_capsule`; pointing both readers at it was the whole test.

## Defect 89 — a proxy shutdown could wait forever for a task that refuses to be cancelled (2026-11-19)

Found by the full suite, not by reading: `tests/test_proxy_limits.py::test_a_burst_larger_than_the_old_soft_limit_is_still_served`
opens 40 connections at the proxy against a black-hole upstream and then stops it. It failed twice — once under load, once
alone — with the *subprocess* timeout (60 s) as the only thing that ended it, and a hand-written reproduction showed where
the time went:

```
Thread ... (most recent call first):
  File "asyncio/base_events.py", line 1884 in _run_once
  File "asyncio/base_events.py", line 640 in run_until_complete
  File "supervisor.py", line 269 in _drain_and_close
  File "supervisor.py", line 242 in stop
```

`_drain_and_close` cancelled the pending handler tasks and then `gather`ed them. `gather` waits for a task that catches
`CancelledError` and awaits again — which a handler parked mid-request can do — so `stop()` had no bound at all. The drain
now uses `asyncio.wait(tasks, timeout=…)` under **one** deadline for the whole drain (2 s, `DRAIN_S`), and counts what is
still pending in `undrained_at_stop` instead of waiting for it.

The second half of the same measurement matters: the first fix bounded each round at 2 s, and eleven rounds made 22 s. A
per-round timeout is not a shutdown budget; the deadline is computed once. `test_shutdown_is_bounded_even_when_a_handler_
ignores_its_cancellation` builds the worst case by hand (a task that swallows its cancellation and parks on an event that
is never set) and asserts both halves: the drain returns in bounded time **and** the surviving task is counted.

**Not fixed, and stated:** a task that ignores cancellation is left for the collector, which may print `Task was destroyed
but it is pending!`. That is a message about a handler that was already broken; the alternative was a recorder that would
not exit. The condition is counted, and `tests/test_proxy_supervisor.py` asserts `undrained_at_stop == 0` for the normal
shutdown paths.

## Defect 90 — the Windows installer named an icon that did not exist (2026-11-19, the Windows definition)

`blackbox.iss` refers to `blackbox.ico` in four places — the `[Files]` entry, `UninstallDisplayIcon`, the Start Menu
shortcut and the desktop shortcut — and the repository contained no such file. `iscc` fails on the first unresolvable
`Source:`, so the Windows definition was broken before it could ever be compiled, and nothing on this machine would have
noticed: there is no `iscc` here, so the failure would have cost a Windows machine round-trip to discover.

`tests/test_windows_definition.py` (11 tests) now checks the definition the way a compiler would, as far as that can be
done without Windows: every `Source:` exists or is produced by the staging script, the two halves agree on the launcher,
`[Registry]` writes only under `HKCU`, the uninstaller deletes only its own directory, no `#define` is dead, and the icon
is a real ICO. The icon is generated by `tools/package/windows/make_icon.py` from the same primitives as
`debian/blackbox.svg` — a near-black rounded square, a gold ring, a gold waveform — with no image library and no network,
at 16/32/48/64/128/256 pixels, and the test decodes the 32-pixel entry to check the field, the ring, the mark and the
transparent corner.

**Two smaller things the same review found, both fixed:**

* `#define AppExe "win64\python\pythonw.exe"` was never used, so a reader would believe the install's entry point was
  `pythonw.exe` directly. It is not: the entry point is `blackbox.cmd`, which sets `%LOCALAPPDATA%\BlackBox` and points the
  interpreter's stderr at a log. The unused define is gone, and a test fails on the next one.
* The staging script's launcher ran `python.exe`, which puts a console window beside a desktop app, and on a non-zero exit
  the window closed with the error inside it. It now runs `pythonw.exe`, appends stderr to
  `%LOCALAPPDATA%\BlackBox\launcher.log`, and on failure prints the log's tail and waits.

## Defect 91 — a `GET` could render the apply route (2026-11-19, the repair page)

The repair pages added two `POST` routes. `GET /<token>/repair` is the review sheet, so the router matched `repair` and
passed the rest of the path to the same renderer — which meant `GET /<token>/repair/apply?proposal=…&confirm=yes`
returned **200** and a review page. Nothing was written (the write only happens in `do_POST`), so this was not a
vulnerability; it was worse than that in one narrow way: a link that *looks* like an approval and answers `200` teaches a
reader that approval can be driven by a link. `tests/test_ui_safety.py::test_a_get_cannot_apply_a_proposal` failed on it,
and the router now answers 404 for any `repair/<sub-path>` reached by `GET`.

**What the safety file covers now**, because "the window is safe" is a claim that needs tests and not a tone of voice: no
secret from the environment on any of seven pages; no page referencing anything outside the loopback server (script bodies
scanned with comments stripped, since the viewer's own header *says* "no `fetch()`, no `XMLHttpRequest`"); a redacted value
staying redacted through a real recorded run; four hostile capsules refused with `BBX-` codes and no traceback anywhere,
including on the check page and the run page; a capsule *name* from the URL never reaching outside the data directory; the
data directory byte-identical after loading every page twice; a non-form `POST` refused; and a run whose process was killed
still listing, opening, verifying and diagnosing.

## Defect 92 — a test of the journey script asserted a Windows-impossible file mode (2026-11-16, CI)

`tests/test_desktop_journey.py::test_the_script_is_valid_posix_shell` asserted `SCRIPT.stat().st_mode & 0o111` — that
`tools/desktop_journey.sh` is executable. On a POSIX filesystem with this checkout it is. On GitHub's Windows runners Git
does not store or restore a POSIX permission bit, so the file arrives mode `0o666` and the assertion failed in **all three
Windows cells** (`py3.11`, `py3.12`, `py3.13`; run 44570138085, and the PR run 44570119233) while Ubuntu was 3/3 green. The
failure is real evidence and it is not a product defect: every invocation of the script here, in `docs/desktop.md` and in
the test file itself is `sh tools/desktop_journey.sh`, which needs no bit at all.

What was wrong is the shape of the check: it tested what the checkout preserved rather than what makes the script runnable.
It now asserts the two things that are the script's own — that `sh -n` parses it, and that its first line is `#!/bin/sh` —
and asserts the permission bit only when `os.name == "posix"`, with the reason in the docstring so the next person does
not read the conditional as an attempt to go green. Nothing was weakened on Linux: the bit assertion still runs here and
still fails if it is cleared (`chmod -x` was checked after the change).

**Kept as a record of the cost:** the branch's previous two commits were green on Ubuntu and Windows and were read as
"Windows 3/3" in this PR — that reading was right for those commits and wrong for this one, which is why each commit's row
in the CI table below names the commit rather than the pull request.

## Defect 93 — the Markdown report quoted recorded text without escaping it (2026-11-16, the 20-payload corpus)

`tests/test_xss_corpus.py` puts twenty payloads through a **real capsule** — a tape whose node names, model text, marks
and exception messages all contain them — and then reads what three renderers do with it: the offline viewer page, the
desktop library and run pages, and `blackbox diagnose`'s Markdown report and repair brief.

The viewer was already safe: the payloads arrive as characters in a JSON data block and `app.js` builds text nodes.
The **report was not**. Its timeline table escaped only `|`, so a capsule containing `<script>alert('script')</script>`
produced a line like

```
| 2 | `NODE_OPEN` | 1 | name=<script>alert('script')</script>; kind=step |
```

and the same raw text appeared in bullet lines ("what the evidence shows", alternatives, checks) and inside single
backticks for the recorded `argv`. Markdown renderers differ on raw HTML — several pass it through, some sanitise —
which is exactly the wrong thing to depend on for a document whose whole purpose is to be attached to a ticket and opened
by someone else. The report is a rendering of untrusted text; it needed the same rule the viewer has.

**Fixed** with one function, `inline()`, applied at every place recorded text sits outside a fence: it masks credentials
(`scrub`) and then escapes `&`, `<`, `>`, backtick and `|`, flattening newlines. Fenced blocks keep `fence()`, which is
the right shape for multi-line evidence. The test now fails if any unescaped `<` from a payload appears outside a fence,
if the escaping is applied twice, or if a Markdown image or link target is built from recorded text.

**What this defect was not:** it was not exploitable on the viewer page, in the library, on the run page, in the
page's data block, or in the capsule itself — the corpus checks all five. It was a sink in the one output designed to
leave the machine.

## Defect 94 — a torn last frame hid the whole active segment (2026-11-16, the four-fault table)

Building the fault table for `tests/test_fault_codes.py` turned up a recovery defect with a much worse shape than the
row asked about. A tape whose last committed frame was partly lost — the ordinary result of a power cut during a write —
scanned as **zero frames**. The scanner walks backwards from the offset the header commits; when that offset lands inside
a frame, the trailer it looks for is not there, the walk stops at its first step, and everything earlier on the segment
became invisible. One interrupted write in the last 88 bytes hid the entire run: a recorder failing precisely when the
machine crashed.

**Fixed** by `frame.last_valid_frame_end`: when the backward walk from the committed cursor yields nothing, find the last
frame *before* it that decodes with a good CRC, walk backwards from there, and record what was lost —
`truncated_tail`, `truncated_bytes` (a new field, so the size of the torn tail is a number rather than a word) and
`lost_from_seq`. The resync only runs on that failure path; an intact tape still takes the single backward walk.

`tests/test_fault_codes.py` now asserts the recovery: five of six frames kept, the last one reported lost, the capsule
sealing with `root_ok`, `truncated_tail: true` and `lost_from_seq` naming the first missing sequence number.

## Defect 95 — `retention.apply(dry_run=True)` committed the GAP it was only supposed to describe (2026-11-16)

A dry run that mutates the tape is the one bug a dry run exists to prevent, and it was there: `apply` appended the `GAP`
frame and published it *before* the `dry_run` branch inside the per-segment loop, so asking what retention would delete
declared a retention hole over frames that were still on disk. Every later scan of that tape would have reported a hole
in evidence that had never been deleted — a tape that lies about itself, written by the command whose job is looking
without touching.

**Fixed**: `dry_run` returns before any write, reporting the same plan plus `would_declare` — the span the real pass would
commit. `tests/test_retention_dry_run.py` runs a dry run and then asserts the segment files, the frame count, the tail seq
and the absence of a `GAP` are all unchanged, and separately that the real pass declares exactly the span the dry run
named.

The same file adds what the gate asked for in the first place: `retention.explain()` and the new `blackbox retention`
command give one row per segment — `delete`, `keep`, `pinned` or `active` — with the reason and the numbers behind it
(the keep window, the budget, the tape's own size). It cannot delete: deletion has to commit a `GAP`, and only the tape's
writer can do that.

## Defect 96 — five paths a recording writes were readable by every user on the machine (2026-11-16, the permissions row)

Measured on a real run rather than reasoned from the code, and the code was the problem: `capsules/` and `tape/<uuid>/`
were created `0700` and the capsule and segment files `0600`, but `meta`, `seal` and `runs/<id>/index.sqlite` were
`0644`, and the two directories that hold everything — `tape/` and `runs/` — were `0755`. `os.makedirs(child, mode=0o700)`
does not tighten an implicitly created parent, and a plain `open(path, "wb")` takes the umask. So the promise covered the
tree's leaves and not its branches: another user on the same box could list the tape directory, read the metadata page,
read every seal, and read the index — which carries run ids, node names and timings.

**Fixed** with `platform/perms.private_dir` and `private_file`: create the parent explicitly at `0700` and `chmod` it
(POSIX) rather than trusting the mode argument, and tighten the files SQLite and `open()` make. A second pass found one
more, `<data>/index/` at `0755` the first time `blackbox index` ran; the test now runs `blackbox index` as part of its
fixture, so the audit covers the index directory and not only the run that precedes it. **Verified** by
`tests/test_permissions.py`, which walks **every** path a real run wrote and requires exactly `0600`/`0700` — not "at
least as tight", which would accept `0640` and with it a second reader. The same measurement now runs inside the first-run
doctor, so a data directory unpacked with the wrong modes warns its owner instead of failing silently.

Windows is a different question and is not claimed here: mode bits there do not describe access, so the test reads the
ACL with `icacls` and fails on `Everyone`/`BUILTIN\Users`/`Authenticated Users`/`INTERACTIVE`. **That arm has never run.**

## Defect 97 — `install(content=…)` swallowed a capture policy the wrapper could not pass (2026-11-16, serverless)

Building `serverless.wrap` exposed a fail-open trap in the API it wraps: `api.install()` accepts only writer keywords, so
`install(content="full")` raised `TypeError`, and `install`'s own fail-open caught it and disabled the whole tape. The
user asked for content capture, got no error, no warning and no content — a recorder that had stopped recording. **Fixed**
in the wrapper, which does not pass capture policy through `install`; the policy reaches the sink instead. The lesson is
kept in `docs/serverless.md`: a constructor argument that cannot exist is worse than one that is refused, because
fail-open hides it.

## Defect 98 — every cold start re-emitted the previous invocation's capsule, quadratically (2026-11-16, serverless)

`recover_leftovers()` scanned the whole data directory and sealed every unsealed tape it found, including tapes it had
already sealed on an earlier cold start: the number of callbacks grew with the square of the invocation count (100
invocations produced 5 050 `on_capsule` calls in 28 s). Beyond the cost, the semantics were wrong — a sealed invocation
was being handed to the user's durability callback again, as if it had just finished. **Fixed**: `recover_leftovers` prunes
sealed tapes before it scans, and `tests/test_serverless.py` asserts one callback per invocation across the 250-invocation
random-failure test; the 1 000-invocation bench (`bench/results/2026-11-16-serverless-1000.json`) reports exactly 1 000
documents for 1 000 invocations.

*(Numbers 91 and 92 were already used for two other findings; these two were reported in the session's notes under
those numbers and are recorded here as 97 and 98 so the log has no duplicate. Both are also described in
`docs/serverless.md`.)*

## Defect 99 — a hook-latency test measured the runner, not the product (2026-11-16, CI on Windows)

`44581057445`'s `py3.11 / windows-latest` cell failed at `1628d5a` with
`one hook event cost 160 ms over a bare interpreter (191 vs 31 ms)`. The product was fine: `ingest` imports only what it
uses — `test_the_ingest_path_imports_only_what_it_uses` asserts that from `sys.modules` and has never failed — and what
the test actually compared was "a bare interpreter" against "an interpreter plus the whole package layout", which on that
runner costs 160 ms before `ingest` runs at all. A threshold on that difference is a claim about the runner's disk.

**Fixed** by measuring against the product's own floor: `blackbox --version` imports the front door and the constants and
nothing else, so `ingest - --version` is the part `ingest` owns. The bound (80 ms) and the defect-64 rationale are
unchanged, the number in the failure message now names all three measurements, and the machine-independent half of the
same assertion (the import list) sits beside it.

## Defect 100 — a determinism needle with a zero byte in it (2026-11-16, CI on Windows)

In the same cell: `b'\x00\x00\x05\x00' is in the capsule`, from
`test_no_clock_no_pid_and_no_path_reaches_the_capsule`, which asserted that the process's pid, in four and eight big-endian
bytes, appears nowhere in a capsule. On that runner the pid was 1280, so the "needle" was `00 00 05 00` — a sequence any
archive contains by coincidence. The test was asserting the absence of a byte pattern the format itself produces, not the
absence of a pid.

**Repaired three times, which is the honest history.** First by refusing a needle with a zero byte in it; that failed
again on Ubuntu `py3.11` at `b949e62` with `b"2596" is in the capsule` — four decimal digits occur inside deflated data by
chance, and 11 KB of archive has room for several. Then by trying the only needle that could not collide — the same frames
recorded by two *different processes*, which must produce identical capsules — and that is impossible for a reason worth
knowing: `CHAIN` and `SEG_END` frames carry real `wall_ns`/`mono_ns`, so two recordings are never byte-identical and no
capsule can be, whatever the pid. The test now proves each property where it lives: `created_ns == 0` for the clock; a
40-character random token in the data directory's path for the path; and for the pid, the manifest's exact key set and the
archive's exact entry names — there is no field for a pid, and entry names are the only other place it could travel.
`test_the_same_tape_seals_to_the_same_bytes_wherever_it_is_sealed` remains the byte-identity proof; it copies the tape,
so the clocks are the same by construction.

## Defect 101 — six concurrent recoverers crashed on Windows instead of electing a winner (2026-11-16, CI)

`c0684a9`'s `py3.11 / windows-latest` cell failed the new `test_six_concurrent_recoverers_elect_one_winner` with "a
concurrent recoverer failed without a code" and a traceback. The claim path caught only `FileExistsError`, which is the
POSIX answer. On Windows a lock file another process has open fails differently: `O_EXCL` raises a sharing violation
(`PermissionError`, WinError 32), and unlinking the stale lock while the owner still has it open raises the same. So the
loser of the race — the ordinary outcome the test is *for* — escaped as an unhandled exception instead of `BBX-E210`.

**Fixed** with one predicate, `_lock_conflict`: `FileExistsError`, or WinError 5/32/33. Every claim path that meets it now
returns `False`, which `step_claimed` turns into the coded refusal — "the lock is held by a live recoverer" — and an
unlink that fails because the owner still has the file is treated as the same race rather than as an error. Nothing is
stolen, and nothing escapes uncoded.

**Verified on Linux** (the test, three consecutive runs) and **not yet on Windows**: the fix is in this branch, and the
cell that found the defect will be the cell that confirms it. Until it is green there, `docs/STATUS.md` says so.

## Defect 102 — the suite outgrew the Windows harness budget, and the cells timed out instead of failing (2026-11-16, CI)

At `940577a` both `py3.11` and `py3.12 / windows-latest` were killed with "The action 'Unit + property tests' has timed
out after 15 minutes", while `py3.13` on the same runner fleet *passed* in 14.6 and 13.2 minutes. Nothing was wrong with
the code: the suite had grown from 1 414 to 1 531 tests, and the 117 new ones are subprocess-heavy on purpose — a runtime
netguard that runs eight CLI paths under a refusing `sitecustomize`, a concurrency test that starts six recoverers, a CLI
contract that measures exit codes by invoking the binary, a permissions test that records a real run and indexes it. Linux
runs the whole suite in 7.6 minutes here; a Windows runner is roughly twice as slow, which put every Windows cell within a
minute of the old 15-minute bound and two of them over it.

**A timeout is the worst failure mode available**: it names no test and looks like a hang. **Fixed** by raising the step
budget to 30 minutes with the measurements in the comment, and by leaving `faulthandler_timeout` where it is, so a genuine
hang still dumps its stacks long before the bound. This is headroom, not a licence: if the suite doubles again the answer
is to shard the job, not to raise the number again.

**Not claimed**: no Windows cell is green at this commit yet — the timeout fix is in the branch and the next run is what
measures it.

## Defect 103 — two concurrent recoverers both claimed to have finished the run (2026-11-16, CI)

`8ba986e`'s `py3.12 / ubuntu-latest` cell failed `test_six_concurrent_recoverers_elect_one_winner` with "2 recoverers
each claimed to have done the work"; Windows cells failed the same test. The lock made the *claim* exclusive, but the
claim was checked before the lock in one place and after it in another: a recoverer that lost the race to a winner which
had already run the tape would take the claim, re-check nothing, and finish the run itself — so two callers each came back
with a succeeded step and a different capsule. On a runner with three times the cores the window is wide enough to hit
reliably, which is why only the fast cells found it.

**Fixed** by making the post-claim state the authority: after taking the lock, `step_claimed` asks whether the run is
already archived and, if it is, raises `_ArchivedElsewhere`, which `run()` catches and turns into the adoption result —
the winner's capsule, marked as somebody else's work. The unlink on release now also suppresses `OSError` rather than
only `FileNotFoundError`: on Windows a releasing peer's handle is a sharing violation, and losing that race to *yourself*
is not an error.

**Verified here**: the concurrency test eight consecutive times without a failure, plus the recovery and determinism
modules whole. **Verified on Windows** at `1d42e0c`: the `py3.11`/`py3.12`/`py3.13` Windows cells and all three Ubuntu
cells are green on both workflow runs of that commit, and the test that failed with "2 recoverers each claimed to have
done the work" is the test they ran. Confirmed, not inferred — the fix is what those cells measured.

## Defect 104 — the hung-test watchdog bounded a test that was long by design (2026-11-16, CI)

The `py3.12 / windows-latest` cell dumped `test_250_invocations_with_random_failures_leave_no_open_run` past 120 s inside
`platform/windows.py:160 fdatasync`, with "hung yes". It was not hung: 250 invocations each seal a capsule, which is 40 s
here and roughly three minutes on that runner, where every seal ends in an `fdatasync` that is the slow part. One flat
number served two purposes — naming a hang, and capping a measurement — and the measurement lost.

**Fixed** by letting a test declare its own budget: `@pytest.mark.slow_budget(300)` extends the watchdog for that node
only, the marker is registered in `pyproject.toml`, and the watchdog's message prints the budget it actually used. The
test stays at 250 invocations everywhere, so the breadth the row measures is unchanged; nothing else in the suite asks for
more than 120 s. A marker is the narrow fix: if more than a handful of tests need one, the suite is too slow and that is
the problem to fix.

**Confirmed at `1d42e0c`**: the Windows cells ran the 250 invocations and finished, with no `hung yes` line and no
`fdatasync` dump in the annotation. The bound was the defect; the seal time was not.

## Defect 105 — a concurrent recoverer died with a traceback on Windows instead of a code (2026-11-16, CI, partly diagnosed)

At `4afba25` the three Windows cells still failed `test_six_concurrent_recoverers_elect_one_winner`, but with a different
assertion from defect 103's: "a concurrent recoverer failed without a code". One of six processes exited non-zero with no
`BBX-E…` in its stderr — a traceback, and the annotation's 600 characters carried a `^^^` anchor line from it and nothing
that named the exception, so the *cause* is not yet measured.

**What is measured**: the Ubuntu cells were green at the same commit (defect 103's fix holds), and this is a
Windows-only, concurrency-only, uncoded failure. The one construct on this path that behaves differently between the two
systems is `os.replace` onto a file another process has open: CPython opens files without `FILE_SHARE_DELETE`, so on
Windows the rename is refused (`WinError` 5/32) for as long as that handle lives, while on POSIX it is atomic and
invisible. Recovery renames its own state files — markers, `recovery.json`, the capsule — and every other recoverer in the
race is *reading* exactly those files. That is the likely mechanism, not a proven one, and this entry says so.

**Fixed as far as it can be without a Windows machine**:

* `blackbox.platform.replace` replaces the bare `os.replace` in the recovery state writer and the capsule writer: the two
  Windows codes are retried with a doubling delay inside a 2 s budget, and if the peer never lets go the refusal is coded
  `BBX-E210` ("writer is alive; recovery refused" — the plan's own meaning for "another process owns this run") instead of
  escaping as a `PermissionError`. Any other error, `ENOSPC` included, is raised untouched and never retried.
* Three tests measure that here by simulating a sharing violation: it is retried and then succeeds; a permanent one ends
  in `BBX-E210`; `ENOSPC` is neither retried nor relabelled.
* The concurrency test's own failure message now names the **exception line** rather than the last 1 200 characters of a
  traceback — the third CI cycle spent on this defect was spent reading a caret.

**Not claimed**: the mechanism is still an inference — no Windows machine here can prove that the rename was the
`PermissionError` the cell saw, only that the one Windows-only construct on the path is now waited out and, failing that,
refused with a code.

**Outcome at `1d42e0c`**: all three Windows cells and all three macOS cells are green, so the failure did not recur with
the retry in place. That is consistent with the inference and is not a proof of it: a race that does not happen leaves no
evidence of which side fixed it. The `replace` shim and its three tests stay regardless — the Windows rename behaviour it
handles is real whether or not it was this failure.

## Defect 106 — the retry was on the wrong side of the rename: recovery markers failed to *read* (2026-11-16, CI)

`e86e14a` says exactly what `1d42e0c` could not, because the concurrency test now prints the exception line: two Windows
cells failed with `PermissionError: [Errno 13] Permission denied: …\runs\r_…\recovery.done.DISCOVERED` and
`…\recovery.done.CLAIMED`. So defect 105's inference was half right and half wrong. The gate really is a Windows sharing
refusal on recovery state — but it is the **read** that is refused, not the rename. `DISCOVERED` runs *before* the claim,
so all six processes publish that marker and read it at the same time; a reader that arrives inside a peer's
`MoveFileEx` window is refused with `ERROR_ACCESS_DENIED` (`errno` 13, naming the path rather than a source/destination
pair, which is why 105's guess pointed at `os.replace` and this one is not a guess). Nothing retried it, so it escaped as a
traceback — exactly what the test forbids.

**Fixed** with `blackbox.platform.read`: the same predicate and the same 2 s budget as `replace`, applied to the open.
`recovery._read_json` and `_artifact_hash` use it, so every marker, the lock file and every artifact hash is read through
the retry. A file that is still refused after the budget reads as **absent**, which is the conservative direction — absent
means the step runs again — and `_artifact_hash` returns `None` for the same reason. Anything that is not a sharing
refusal (`FileNotFoundError`, `ENOSPC`) is raised or swallowed exactly as before, untouched.

**Measured here**: two tests simulate the refusal — one that clears after three attempts and one that is an ordinary
`FileNotFoundError` and is neither retried nor swallowed. **Not yet measured on Windows**: the cells that produced this
traceback are the cells that will confirm it, and until they are green this entry stays open.

## Defect 107 — the adopting recoverer still said it had done the work (2026-11-16, CI)

`bc26dce`'s `py3.11 / ubuntu-latest` cell failed the concurrency test with "2 recoverers each claimed to have done the
work" — defect 103's symptom, back at a commit whose Windows and macOS cells were green. The cause is in defect 103's own
fix: `_adopt_archive` reported the winner's capsule and filled `steps_skipped` with every step, but it did not touch
`steps_run`, which the adopting process had already appended to. `DISCOVERED` runs **before** the lock, so a process that
loses the race legitimately has `["DISCOVERED"]` in `steps_run` by the time it adopts. Two processes then each returned a
capsule *and* a non-empty `steps_run`: the duplicate claim the whole test exists to forbid, moved one layer up from "two
capsules" to "two workers".

**Fixed** by emptying `steps_run` in `_adopt_archive`: adopting somebody else's archive means this process did none of the
work, and saying otherwise is a lie the caller cannot check. The `state`, `steps_skipped`, capsule path and root are
unchanged.

**Measured here**: a new single-process test forces the first `_already_archived()` to answer "not yet", so the machine runs
exactly the loser's sequence — `DISCOVERED`, then the claim, then adoption — and asserts `steps_run == []`, every step in
`steps_skipped`, the winner's capsule, and still one capsule on disk. Six consecutive runs of the whole file are green, and
this is the kind of defect that only a single-process test can hold down: the concurrency test found it twice in eleven
cells and missed it in the rest.

**Confirmed at `ce32b86`**: all twenty-two check runs are green on both workflow runs of that commit — the read retry is in
it, and the Windows cells that produced this traceback are the cells that ran it. This one is a measurement rather than an
inference, because the annotation named the call site before the fix was written.

## Defect 108 — one Windows cell failed the rebind budget with a 686 ms outlier, and the message hid which half was ours (2026-11-16, CI)

`daa7471` (docs only, on top of `ce32b86`) had twenty-one of twenty-two cells green; the one red cell was
`py3.11 / windows-latest` failing `test_every_watchdog_rebind_lands_inside_the_budget_not_the_median_of_them` with a worst
request-level outage of **690.5 ms** against a 100 ms budget. The sample is informative in a way the failure is not: eight
outages `[20.6, 18.5, 19.3, 15.0, 12.3, 9.1, 686.2, 20.1]` ms with rebinds `[0, 0, 0, 16, 0, 0, 0, 15]` ms — so of the
686 ms, at most 15 was a rebind, and the same cell logged an asyncio `ProactorEventLoop._start_serving` callback exception
(`<_OverlappedF…>`, a socket the loop was still serving). The test has been green on every other Windows cell at this
commit and at the four before it; this is the first time in eight Windows cells.

**Two honest candidates, and the annotation could not tell them apart**: a shared runner that stalled the watchdog thread
for the length of the sample (a runner, not a proxy — the way defect 99 failed), or a Windows-specific stall in the
proactor loop when a listener it is closing is mid-accept (ours, rare, and the logged callback exception points this way).

**Changed**: the failure message now puts the split first — worst outage, **how late the watchdog tick woke**, that
sample's rebind, dispatch and probe cycle — inside the characters an annotation keeps, before the full lists. Nothing about
the budget moved: the budget is the requirement, and a red cell stays red. **Not claimed**: the cause. The next Windows
cell that fails this test will say which half it was, and that is when a fix — a starved-runner guard, or a proactor
close-race repair — can be written on evidence rather than on this entry.

`docs/STATUS.md` carries the same paragraph. **Did not recur at `382c80f`**: the same cell ran the same test and passed,
which is what a stalled runner looks like and is still not proof of it — one green sample after one red one is a coin
flip, not a diagnosis, and this entry stays open until either the test fails again with the new message and names a starved
tick, or a month of green cells makes it moot.

## Defect 108 (update, 2026-11-20) — the recorded sample says the rebind did none of it, and the tool that says so is now in the tree

The sample was already in this entry; what was missing was the arithmetic. `tools/outage_split.py` reads it and splits
each sample into the rebind, the harness's dispatch, the tick's lateness and the remainder, under a stated rule:

```
budget 100 ms · 8 sample(s)
  6: outlier    686.200 · rebind   0.000 (  0.0 %) · unexplained  686.200 · not-ours:tick-or-loop
  1 of 8 sample(s) over 100 ms; the worst put 0.0 % of its outage in the rebind
```

(`bench/results/2026-11-20-defect-108-split.json`, reproduced field by field by `tests/test_outage_split.py`.) The worst
sample recorded **no rebind work at all**: the 686.2 ms was spent before the code that rebinds ran, which is consistent
with both candidates this entry has always named — a starved runner thread, or the proactor loop parked mid-accept — and
it rules out the third reading, "the rebind was slow", which is what the failure message invited.

Two changes came from that reading, neither of them a weakening of the budget:

* **`ProxyThread.loop_errors`**: the loop's exception handler now *records* what a callback raised (bounded, with the
  handle's type and the message) before printing it as before. The Windows cell's evidence included a
  `ProactorEventLoop._start_serving` callback exception that existed only in a job log this sandbox cannot fetch; the next
  occurrence will be a field in the process's own record. `tests/test_proxy_supervisor.py` holds both halves: a callback
  that raises is recorded, and a clean twelve-rebind storm records nothing.
* **The test's failure message** already led with how late the tick woke (this entry's previous change); it now has a
  number to put beside the budget in the tool's own vocabulary.

**Not claimed**: the cause, and not fixed — a starved runner is not something the product can fix, and a proactor
close-race has not been reproduced anywhere this build can run. The entry stays open until a Windows cell fails with the
new record and names which half it was, or a month of green cells makes it moot.

## Defect 109 — the proxy promised `chunked` over a body it was relaying raw, and `curl` refused the stream (2026-11-20, found while closing §24a's curl row)

**What a user hits.** A provider that streams with neither `Content-Length` nor `Transfer-Encoding` — a body that ends
when the connection closes, which is the shape of a plain SSE endpoint and of this repository's own mock provider —
answers through the proxy with a head that says `Transfer-Encoding: chunked` while the proxy forwards the bytes
untouched. `curl` stops with `(56) Malformed encoding found in chunked-encoding`. The SDK clients did not look, so the
capture tests were green over a response that a plain HTTP client could not read.

**Reproduced** by issuing the same request twice, once straight at the mock provider and once through `ProxyThread`:

```
DIRECT: b'HTTP/1.1 200 OK\r\n...\r\nconnection: close\r\n\r\ndata: {...}\n\ndata: {...}'
PROXY : b'HTTP/1.1 200 OK\r\n...\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\ndata: {...}'
```

**Fixed** in `ProxyServer._client_head`, which now writes exactly one of three things and lets the third decide the
connection: a forwarded `Content-Length`, `Transfer-Encoding: chunked` only when the upstream framed the body that way, or
no framing at all *and* `Connection: close` — because a body whose end is the connection's end cannot be promised past it.
The caller computes the same rule before it decides whether to keep the client connection, and `204`/`304` now carry no
framing instead of a chunked head over nothing.

**Tests**: `tests/proxy/test_response_framing.py` (5) holds the head to the body in both directions; `tests/test_attach_curl_kill.py`
(2) is the client that found it — a real `curl`, a real `SIGKILL` mid-stream, and a recovered capsule whose verify now
exits 0.

## Defect 110 — a capsule failed its own derived-part check because a sentence used the wall clock (2026-11-20, found by defect 109's test)

**What a user hits.** `blackbox verify` on a capsule from a killed run exits **9** with `BBX-E412 graph.json.nodes`, the
declared node detail reading `last chunk 1854 ms before death` against a recomputation reading `last chunk 49 ms before
death`. Both numbers were true of the clock that made them: `graph.json` is a *derived* part, `verify` recomputes it from
the frames, and `classify_nodes()` measured "before death" against `time.time_ns()` at the moment it ran. A derived part
that is a function of when it was derived cannot be recomputed, so a correctly sealed capsule failed its own integrity
check.

**Fixed** by taking the tape's own clock: `classify_nodes()` now uses the last frame's `wall_ns` when no clock is passed,
and recovery no longer passes its detection time when it builds the graph. The sentence is now a function of the frames —
the same number at seal time and at verify time — and the recovery *result* a user reads is computed the same way, so the
two agree.

**Tests**: `tests/test_attach_curl_kill.py::test_the_capsule_from_a_killed_curl_run_verifies` (the reproduction),
`tests/test_recovery.py`, `tests/test_capsule.py`, `tests/test_verify_cli.py`, `tests/test_content_modes.py` (unchanged and
still green, which is the point: the sentence keeps its meaning, it loses its dependence on a clock).

## Defect 111 — six error messages told the user to run a command that this build did not have (2026-11-20, found while writing `docs/troubleshooting.md`)

**What a user hits.** `docs/troubleshooting.md` is written as "if you see `BBX-E…`, run …". Four of its pages and two
remediation strings in `src/blackbox/errors.py` said `blackbox doctor --capture` / `--hooks` / `--storage` / `--tape`, and
`blackbox doctor` was not in `COMMANDS`. A user following the advice got the unknown-command text and nothing else — the
worst shape of advice, because it reads as a fix.

**Fixed** by writing the command: `src/blackbox/cli/cmd_doctor.py` runs six probes — `environment` (interpreter, the
installed package, the platform), `capture` (whether the site hook would install a seam, and why not), `tape` (counts and
the newest tape's seal state), `hooks` (which adapters are importable), `storage` (free space, plus a real write + `fsync`
+ rename timed) and `locks` (writer leases in this data directory). Exit 0 for ok/warn, 8 for any fail, 2 for a usage
error; `--json` for a machine. Two flags that the prose had invented — `--assert-offline <url>` and `--secrets` — were
**dropped rather than implemented**: egress refusal and redaction are proven by tests that run every sink, and a probe that
merely re-asserts them would be a second, weaker copy. Their strings now point at those tests.

`probe_locks` first walked the data directory with `rglob` and reported seven false leases — it was reading the *repo's*
`package-lock.json` and `.gitignore`d build locks, because a data directory may be any directory, including a repository
root. It now looks only for this layout: `*.lock` and `*lease*` at the top, plus the `locks/`, `tape/` and `runs/`
subtrees.

**Tests**: `tests/test_error_advice.py` (3) — every remediation in `src/blackbox/errors.py` must name a command the binary
dispatches; every `doctor --x` in a remediation must be a probe that exists; and no current user document may name a
command the build does not have (history files are excluded, because they are the record of commands that were removed).
`tests/test_cli_contract.py` (22) and `docs/errors.md`, regenerated.

## Defect 112 — `docs/guarantees.md` pointed at four test files that have never existed (2026-11-20, found while writing `docs/RELEASE-CANDIDATE.md`)

**What a user hits.** The page a cautious user opens before trusting the tool — "here is what is promised and the test that
holds it" — named `tests/kill/test_kill_stages.py`, `tests/test_head.py`, `tests/test_prealloc_sigbus.py` and
`tests/test_recovery_idempotence.py`. None of the four paths is in the tree. Every row also carried the status word
`planned`, under a paragraph that said the table was the current answer, so the page read as evidence while being a plan.

**Fixed** by rewriting the page around tests that exist: 17 claim rows, each naming a file and the specific test inside it,
with §2's durability modes described by the assertion that holds each one, and a "what this page cannot promise" paragraph
that says which claims rest on the OS rather than on our code. The four phantom paths are kept in the page's own history
sentence — deleting them would make the page unable to record that it once had them.

**Tests**: `tests/test_guarantees_links.py` (2) — every `|`-row in the table must name files and `file::name` nodes that
are in the tree (read from each file's AST, so a rename fails it), and no row may say `planned`. The prose above the table
is deliberately not scanned: it is the record of this defect.
## Defect 113 — one Windows CI cell timed out twice on a Node subprocess, on a commit whose sibling cell passed (2026-11-20, CI, open)

**What happened.** `py3.12 / windows-latest` failed on `45a5609` with two 60-second subprocess timeouts in
`tests/proxy/test_client_matrix.py::test_node_gets_the_origins_bytes_through_the_proxy` and
`::test_the_two_process_clients_disagree_about_nothing`. The annotations carry
`subprocess.TimeoutExpired: Command '['C:\\Program Files\\nodejs\\node.EXE', …]' timed out after 60 seconds` for both. The
same commit's `py3.11` and `py3.13` Windows cells passed, and the *same* `py3.12 / windows-latest` job passed on a second
run of the same SHA (`44733709855`). The two runs were started 25 s apart, so the difference between them is the machine.

**What is ruled out.** The framing change in `_client_head` (defect 109) cannot be the cause of these two tests: they
`GET /p/stream`, and the mock provider's `do_GET` answers with `content-length` set, so the head the proxy writes for that
route is the forwarded length — the unframed branch this branch changed is not reached. Reproduced three times locally,
green each time (`tests/proxy/test_client_matrix.py`).

**Not resolved, and not called a flake.** A 60-second timeout for a loopback GET against a local mock is two orders of
magnitude past the work; either Windows runners under this six-cell matrix can starve a Node startup for a minute, or
something in this test's teardown occasionally leaves a socket the child waits on. Recorded here so the next occurrence
has a second data point, with the job URL above.

**One thing was changed anyway, because it makes the next occurrence answerable:** both Node clients in that file now go
through `_run_node()`, which keeps the child's `stderr` and prints it in the failure message instead of letting
`TimeoutExpired` discard the only evidence the test had. That is a diagnostic, not a fix — the assertion is unchanged, the
60-second budget is unchanged, and neither test is skipped.
## Defect 114 — Windows cells now trip the 120-second watchdog in the index-rebuild tests, on commits that touch no index code (2026-11-20, CI, open)

**What happened.** Three Windows cells on this branch were killed by the suite's per-test watchdog inside
`tests/test_index_rebuild.py`, on three different commits:

| Cell | Commit | Test | Where the watchdog fired |
|---|---|---|---|
| `py3.12 / windows-latest` | `65c600e` | `test_a_500_run_rebuild_reproduces_the_incremental_index` | `src/blackbox/index/ingest.py:73 capsule_rows` |
| `py3.13 / windows-latest` | `a259a70` | `test_two_rebuilds_of_an_unchanged_corpus_are_the_same_bytes` | `src/blackbox/index/ingest.py:127 insert_rows` |
| `py3.12 / windows-latest` | `a259a70` | `test_a_500_run_rebuild_reproduces_the_incremental_index` | `capsule_rows` |

`a259a70` is a `docs/STATUS.md` one-liner. Nothing on this branch touches `src/blackbox/index/` or `tests/test_index_rebuild.py`,
and the same files pass on the other eight cells of the same runs, on macOS and Ubuntu, and on the *identical* Windows job
in a second run of the same commit. So this is not a code path this branch changed: it is how close these tests sit to the
limit on a loaded Windows runner.

**How close, measured here.** Whole file on this machine (2 vCPU, ext4): **21.8 s**, of which 15.5 s is the fixture
building 500 capsules and 1.8 s is the rebuild under test; every other test in the file is under 2 s. A Windows runner is
several times slower at creating and syncing 500 small files (NTFS plus Defender on every close), and the matrix runs six
cells at once, so a 8× slowdown puts the heaviest case past 120 s. That is consistent with all three occurrences and with
the passing neighbours, but it is an inference from this machine's numbers, not a measurement on a Windows runner.

**Not changed.** The watchdog stays at 120 s, the test stays 500 runs, and nothing was skipped: a suite that lengthens its
watchdog until Windows passes is a suite that has stopped measuring. What would settle it: a Windows runner with
`--durations=0` on this file and Defender excluded, and the fixture's per-file cost recorded — if capsule creation alone
is most of it, the honest fix is in the fixture's file count or in the way the capsule writer syncs, and either is a real
change with its own evidence.
