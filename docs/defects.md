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

20. **found by writing `SUPPORTED.md`** → both seams implemented the async path (`AsyncClient` hooks, the async
    tee) but **no test exercises it**: no async HTTP harness is installed here, so an async client is a claim, not
    evidence → recorded in `SUPPORTED.md` as a gap in that word rather than left to read as verified → the fix is a
    test, so this entry stays open until `tests/test_seam_async.py` exists and runs (P3-14 / the `anyio` decision,
    whichever lands first). Until then `SUPPORTED.md`'s async row says so, and no gate may cite the async path.
