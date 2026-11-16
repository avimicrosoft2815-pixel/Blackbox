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

