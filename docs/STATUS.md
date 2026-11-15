# STATUS — BlackBox v1.0 build

**Branch** `arena/e10fc603-blackbox` · **base** `30c9433` · **HEAD** `8e2256e`+ (pushed after every ticket)
**trunk: mergeable** — every C12 check passes from a clean clone and the README says what exists.
**product: pre-alpha — storage and recovery layer only; no SDK, proxy or viewer yet.**

---

## 0. No mapping anywhere: ADR-005 plus C21

**ADR-005** removed the segment mapping; **C21** removed the last one, the `head`. `HeadFile.publish` writes the
64 B slot with one `pwrite` at the alternate offset, readers `pread`, and `import mmap` no longer appears in
`src/`. The head had been created sparse (`ftruncate`) and then mapped, so the first publish on a full
filesystem would have had to allocate a block, failed, and killed the user's process with `SIGBUS` — the exact
class S17 and P1-20 exist to remove, sitting under every publish. `half_hook` now splits body and CRC into two
writes, which is how S4 reproduces a *machine-level* tear (a process kill cannot tear a 64 B `pwrite`).

`mode=process` therefore costs **two syscalls per batch** — segment data and head slot — and nothing else;
`tests/test_no_syscall.py` asserts exactly that over 100 000 appends. Consumers of positional I/O go through
`platform.pwrite`/`platform.pread`: POSIX and macOS delegate to the `os` calls, Windows does `lseek` + `write`/
`read` with the reason stated in its docstring. DV-010 records it.

### The store decision behind it: ADR-005 supersedes ADR-001

`P1-19` was decided under the plan's own rule (§8.2 / D13: if a plain `write()`-append writer is within 20 % of
the mmap writer at the target rates, the simpler store wins). Measured with 10 interleaved pairs per rate on
ext4: **median p50 ratio 1.059 at 10 k ev/s and 1.065 at 40 k ev/s**, `write` p99 **71.9 / 68.4 µs**, one
`pwrite` per ~52 events. Both decision rates are inside the rule, so **the active segment is written with
`pwrite`, not mapped**. Per event it is ~6 % *slower*, and ADR-005 says so instead of pretending otherwise; what
it removes is a hazard the recorder cannot refuse (a page fault on a segment whose blocks are gone kills the
process; `pwrite` returns `ENOSPC` and the writer degrades), plus `FlushViewOfFile` and the
mapped-file-cannot-be-deleted problem. Every number below was re-measured on the new store.

## 1. What is actually green (each row: a test that ran here)

| Area | Evidence | Result |
|---|---|---|
| Canonical CBOR | `tests/test_codec.py` | 42 tests green |
| CBOR decoder hardening (P1-03) | `bench/fuzz_cbor.py` → `-fuzz-cbor.json` | 100 000 buffers, **0 non-`CBORError`** |
| Frame encode/decode, CRC scope | `tests/test_frame.py`, `tests/test_conformance.py` | green |
| Checksum (one algorithm, C4) | `tests/conformance/vectors/crc32.json` | check value `0xCBF43926` |
| Head, two-slot publish, rebuild | `tests/test_head_creation.py` | green |
| Segmentation, roll, adoption (S15) | `tests/test_adoption.py` (7), `tests/test_tape.py` | adoption idempotent in writer **and** `rec/discover.py` |
| Crash points S1–S5, S14, S15 | `tests/test_crash_points.py` | green **on the new store** |
| Crash states read back | `tests/test_scan_states.py` (seeds 388/209/396) | green |
| Randomized parent-`SIGKILL` (P2-09, C19) | `-kill-random.json`: **500 effective trials ext4 + 500 tmpfs** | **0 violations, 0 `not_started`** — the timer starts at the child's `READY`; cells include a 20 ev/s window cell and a `kernel` cell |
| ENOSPC (S17) | `tests/test_enospc.py`, `tests/test_prealloc.py` | both segments **and the first one**: exit 0, identical stdout, `DEGRADED`; no first segment ⇒ sink (`recording=False`), never an exception |
| Group-commit window + keeper | `tests/test_keeper.py` (6) | window ≤ 5 ms at 1 / 1 000 / unbounded ev/s |
| Syscalls on the append path (C11) | `tests/test_no_syscall.py` | 100 000 appends ⇒ **exactly two `pwrite`s per batch** (segment data + 64 B head slot), nothing else |
| Seal footer + chain link | `tests/test_tape.py` | green |
| Platform shims (P2-03/P2-04, C20) | `tests/test_platform_shims.py` (21) | contract green on Linux; Windows start-time path exercised through a fake `kernel32`; `is_current` is computed, so exactly one shim claims the machine |
| Retention + declared holes (`P1-09b`) | `tests/test_retention.py` (13), `tests/property/test_retention_random.py` (9) | **8 seeds × 60 randomized steps: 133 kills, 65 retention passes, 34 segments pruned, 0 violations of I1 or I9** (`bench/results/p1-09b-retention.json`) |
| Chain walk across a hole (`P1-09b`, I9) | `tape.scanner.verify_chain` | per-segment `prev_chain` links + every `CHAIN` frame's `prev`; every declared hole's `prev` must be a value on that walk |
| Blob store write/read (`P1-14`) | `tests/test_blobs.py` | hash → tmp → fsync → replace; **a kill at `S9` leaves a tmp file and no visible blob**, `S9b` leaves a complete one; every read re-verifies CRC and hash and refuses a blob stored under another name (`BBX-E162`) |
| Blob compression queue (`P1-15`) | `tests/test_blobs.py`, `tape/keeper.py` | keeper-driven, 1 job per tick in the test; the rename is observed with the old file still raw and the new one whole; `ChunkCoalescer` turns 2 000 chunks into 3 blobs |
| Recovery state machine (`P2-06`) | `tests/test_recovery.py` | 8 steps × 3 kill modes (before/mid/after) all resume to the **identical capsule root**; each step writes `recovery.done.<STEP>` with input/output hashes; an archived run is a no-op |
| Interrupted nodes (`P2-07`) | `tests/test_recovery.py` | the five §14.4 strings exactly; mid-stream carries `chunks`, `partial_stream_bytes`, `last_chunk_age_ms`; `TOOL_REQUEST` without result says the side effect may have happened |
| Capsule v0 (`P2-10`) | `tests/test_capsule.py` (9) | raw frames + `chain.json` + manifest + `INTEGRITY.sha256`; `.partial` → `os.replace`; re-seal is byte-identical, one changed byte breaks the root, traversal/duplicate/newer-manifest rejected |
| Recovery idempotence (`P2-12`) | `tests/test_recovery.py` | 10 recoveries ⇒ 1 capsule hash, 1 `.bbx`; a second recoverer process gets `BBX-E210`, and a dead holder's lock is taken over |
| Lint + types (C6) | `make fmt` (ruff 0.17.5, mypy 2.4.1) | **0 findings** both |
| Forbidden words (C12) | `tests/test_forbidden_words.py` | green over the tree **and** `git log` |

Total: **281 tests** — 280 pass here and 1 is skipped on POSIX by name (`test_an_open_view_blocks_the_unlink_only_on_windows`: POSIX permits unlinking a file another fd still holds, so the open-view case only exists on Windows). ruff and mypy: 0 findings.

## 2. §15.1 rows — ext4 is the gate column (C1); every row carries its verdict

Gate: **p50 ≤ 25 µs, p99 ≤ 250 µs, ≥ 40 000 ev/s** for one full event with `content=hash`, CHAIN included.

| Row | ext4 (gate) | tmpfs (label only) | Verdict | Source |
|---|---|---|---|---|
| **full event, group commit 16 KiB, CHAIN on** | **p50 11.057 · p90 18.322 · p95 20.53 · p99 51.662 µs · mean 13.396 · max 244.472 · 73,338 ev/s** (N 200,000, warm-up 20,000, 2.727 s) | p50 11.148 · p99 54.215 · 74,334 ev/s | **PASS** | `-writer-throughput.json` |
| same, keeper thread publishing | p50 11.044 · p99 54.527 · 73,471 ev/s | — | **PASS** | same |
| kernel mode (`sync_ms=250`, `1 MiB`) | p50 11.278 · p99 89.903 · 60,368 ev/s · sync stall max 2866 µs over 28 syncs | p50 10.953 | reported, not gated | same |
| kernel mode, sync-dense (64 KiB) | p50 11.387 · p99 81.232 · stall max 11875 µs over 453 syncs | — | reported, not gated | same |
| group-commit window | 4.603 ms @ 1 ev/s · 4.999 ms @ 1 000 ev/s · 3.572 ms unbounded (120.99 ev/commit) | 0.0 / 5.0 / 1.65 ms | bound 5 ms: **PASS** | same |
| recovery, 10 k frames | p50 117.09 ms (**53.9 ms/10k**) | p50 106.03 ms | no §15.1 row for scan time | `-recovery.json` |
| disk amplification | 1.843× per-event · 1.231× 16 KiB | identical | ≤ 1.25×: **PASS** | same |
| RSS above baseline | **1.15 MiB** (anon +1.02, file +0.12) at `head_off` 4,723,072 (28.13 % of the segment) | 1.15 MiB | under 24 MiB: **PASS**, no `P1-08b` | same |
| mmap vs `pwrite` (P1-19 / ADR-005) | median p50 ratio 1.028 [0.906, 1.176] · **1.059** · **1.065** at 1 k / 10 k / 40 k | 1.076 / 1.064 | **adopt `pwrite`** (inside 20 % at both decision rates) | `-writer-ab.json` |

**C16 — why the stage table never added up to the event p50, and the distribution that replaces the guess:**
two mistakes, both now fixed. (a) The stage table's `cbor_encode_event` encodes a record with the **1 KiB body
inline** (9.144 µs) while the throughput bench writes `content=hash`, i.e. only the
hash plus the small envelope (7.953 µs) — a new row measures that record and
its frame encode (2.535 µs), and the JSON carries the explanation.
(b) Far more of the gap: the bench's clock started **after** `cbor.encode(...)`, so the gate number was missing
its own encode. The clock now starts at the Python `dict`, as C2 defines the interval; the row above is that
number, and it is ~2× the old one. Now the parts roughly account for the whole: hash-record encode
7.953 + frame encode 2.535 + batch
`pwrite` 1.302 µs *per batch* + head publish 1.237 µs
*per batch*, amortised over 110.0 events per publish.
**Where the tail lives:** mean 13.396 vs p50 11.057 µs, p99 51.662, p99.9
116.94, max 244.472 µs. The 99th percentile is the **batch boundary** — one event in
110.0 pays the 16 KiB `pwrite` + CHAIN + head publish (~5 µs, i.e. ~0.045 µs amortised
across every event but a 5 µs spike on the one that pays it) — plus GC of the encoded payload. The 300 µs max is
the scheduler, not the writer: with `_fs` on ext4 under a shared sandbox, one page-cache writeback can preempt.
**Threads:** the app thread appends *and* publishes (the timed region is its own call, CBOR encode included); when
the keeper is on it publishes from its own thread, and both share **one GIL**. The `keeper` row above is the same
work with that second publisher present and costs 0.052 µs at p50.

## 3. The Stage 2 CHANGE items and the corrections C1–C24

The nine CHANGE items are answered in §3a of the previous revision of this file and still hold, except that item
8's "interleaved P1-19" is now the full C17 protocol (10 pairs, order alternating, both arms fair) — see §0.
Each correction is answered where a reviewer will look:

| # | Asked | State |
|---|---|---|
| C15 | trunk vs product are different properties | **done** — the two header lines at the top |
| C16 | reconcile the stage table with the event p50; full distribution; name the tail and the threads | **done** — §2 (the encode was outside the clock; it is inside now) |
| C17 | one final A/B by the plan's rule, then closed | **done** — ADR-005, `pwrite` adopted, every gate re-run on the new store |
| C18 | show the RSS procedure | **done** — §2 + `-recovery.json`: `head_off`, bytes written, `VmRSS`/`RssAnon`/`RssFile`/`RssShmem` before and after, cgroup limit, and a second run at 39 % of a segment showing the delta is Python heap, not segment fill |
| C19 | 500 *effective* trials; low-rate and kernel cells | **done** — readiness pipe, 0 `not_started`, 20 ev/s window cell, `kernel` cell, 50 trials per cell |
| C20 | P2-03 `OpenProcess`+`GetProcessTimes`; P2-04 `F_FULLFSYNC`; P1-09b is mine to decide; F13 is P2-11; `P1-02b` before G-Promise; ship `tools/s17_control.sh`; keeper maxima stay; name the stall thread; report CI | **done except P1-02b** — both are queued below; the stall is measured in the app thread's own `emit` (the sync happens inside `publish`, which the app thread calls; the keeper's own publishes are not charged to an event) |
| C7 | CI must actually run | **it ran: ubuntu and docs green, macOS and Windows red** — see §6 |
| C21 | no mapping anywhere in `src/`; head written by `pwrite`, S4 as a torn-slot hook, two `pwrite`s per batch | **done** — §0, DV-010, `HeadFile`, `test_no_syscall` |
| C22 | diagnose the red cells from the tree; `is_current`; `pwrite`/`pread` in every shim; `F_FULLFSYNC` claims derived from code | **done in code**; the cells are still red and the remaining cause is not readable from here |
| C23 | compatibility evidence in a document fed by CI | **done** — `docs/compatibility.md`, per cell, with the run URL |
| C24 | print STATUS verbatim with the numbers | **this file** |
| C29 | `O_BINARY` on every descriptor; guard; byte-exact regression test | **done** — DV-012, `platform.open_fd`, `test_positional_io_is_byte_exact_for_all_256_values` |
| C30 | `test_sparse_detection` is false on NTFS, not unmeasurable | **done** — skipped with that sentence; `prealloc.is_sparse` documents the `FSCTL_SET_SPARSE` fact |

## 4. Defects found and fixed (C9: seed → invariant → cause → fix → test)

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

## 5. F1–F13 — decided, one line each (C10)

F1 hold · F2 hold · F3 hold · **F4 apply** (DV-001, layout unchanged) · F5 hold · F6 hold · F7 hold ·
**F8 apply** (D-003) · **F9 apply** (E225) · F10 reject · F11 hold · **F12 reject** · **F13 hold, and it belongs
to `P2-11`** (`blackbox run`), not P5 — corrected here and in `DECISIONS.md`.

## 6. C12 checklist and C7 (CI)

| Check | State |
|---|---|
| `make test` from a clean clone | green, `280 passed, 1 skipped` (8.5 s) |
| `make kill` (500 trials, ext4) | 0 violations, 0 `not_started` |
| `make bench` into `bench/results/` | green, both columns |
| `make fuzz` | 100 000 iterations, 0 non-`CBORError` |
| `python -m blackbox --version` | `blackbox 0.1.0.dev0 (format v2)` |
| `README.md` ≤ 60 lines + status line | 40 lines, status line present |
| no `LICENSE` file or field | confirmed |
| docs + `bench/results/*.json` committed | yes |
| no absolute sandbox paths | grep over `bench/results/` = 0 |
| forbidden words, tree + commit messages | green |
| **C7 CI** | **ran; eight of nine cells green.** Run `43656191825`: **docs, ubuntu × 3 and macOS × 3 green**; **Windows × 3 red**, and now *named* — C27's follow-up turns every failure into a `::error::` annotation, so `gh run view` lists the eight failing test ids without a log or a login. Run `43657997367`: **all ten cells green** (ubuntu × 3, macOS × 3, Windows × 3, docs) after C29's `O_BINARY` fix; run `43659827155` (commit `8e2256e`) repeats it with the blob store, the retention randomizer, the 24 kill-resume trials and the capsule v0 in the tree. Earlier run `43656494686` had named **nine** Windows failures and surfaces the smoking gun: **`BBX-E224: sealed segment does not match its footer`** — a header CRC and a seal body that do not match the bytes the writer just wrote, on the path that now goes through `platform.windows.pwrite` (`lseek` + `os.write`). `docs/compatibility.md` lists the nine ids, the two candidate causes (a buffered `open("wb")` left on the header/seal path, or a caller still doing a single `os.read` and getting a short read) and the one id that is only a POSIX-only assertion (`st_blocks`). That is the next ticket. |

## 7. Not done — and why that is the honest state

| Ticket | State | Note |
|---|---|---|
| `P1-09b` retention + `GAP{RETENTION}` | **done** | `tape/retention.py`; F2/F3 decided as D-016/D-017, tombstone pinning/merging as D-018, `GAP` payload frozen in `format.md` §4.1; acceptance = 8 seeds × 60 steps, 133 kills, 0 violations of I1/I9 |
| `P1-13` seal verifier + compaction | **partial** | footer writer + verifier tested; `arch/` compaction not written |
| `P1-14` / `P1-15` `store/blobs.py` | **done** | container frozen in `format.md` §7.1; crash stages `S9`/`S9b`; compression runs on the keeper, not the app thread |
| `P1-16` small-model state machine | **done** | `tests/property/test_tape_model.py`; the retention ordering (`GAP` before unlink) is modelled now and is the spec `P1-09b` has to satisfy |
| `P1-02b` mypy `type-arg` + `no-untyped-def` | **pending, closes before G-Promise** | 75 strict findings in the two disabled categories |
| `P2-03` / `P2-04` OS gates | **ticked — CI, run `43657997367`** | all ten cells green: Windows ran `OpenProcess`+`GetProcessTimes`, `winreg` `BootId`, `lseek`-based positional I/O and zero-fill preallocation, and the lease tests through `platform.current()`; macOS ran `sysctl(KERN_PROC_PID)`, `kern.boottime` and `F_FULLFSYNC`. Benchmark rows stay `not measured` for those OSes |
| `P2-11` / `P2-13` launcher + stall watchdog | **pending** | `rec/launcher.py`: spawn, env, child lock, `stall_ms` watchdog, immediate recovery, exit-code propagation; `SIGUSR1` + `faulthandler` stack dump on POSIX |
| `P2-06` / `P2-07` / `P2-10` / `P2-12` | **done** | recovery machine, node classification, capsule v0 and idempotence, each with the test named in §1 |
| P2-09 week of nightlies | **requires time (nightly CI × 7 days)** | GitHub `schedule` runs only on the default branch; the clock starts at merge |
| P11-05, P11-12 | **requires humans** | a human operator and a real incident |
| S17 sparse control (C20) | **requires humans (root on any Linux box, two minutes)** | `tools/s17_control.sh` builds the 2 MiB tmpfs control and prints the signal; it cannot be run here (no superuser) |
| Windows/macOS **benchmark** rows | `not measured` | the test gates are green (run `43657997367`); no §15.1 number in this repository comes from a runner, and none will |
| P10, P12 | **not started** | P10 only after every v1.0 gate is green; P12 never without instruction |

## 8. Next, in order

1. `P2-11` / `P2-13` — `rec/launcher.py`: `blackbox run -- cmd`, the child lock, the stall watchdog and the
   `SIGUSR1` stack dump.
2. `P1-02b` (mypy `type-arg` + `no-untyped-def`) → **G-Promise on ext4** → STATUS.
3. `P1-13` compaction of sealed segments; the blob GC (§9.9) once segments reference blobs; `P2-09` nights start
   at merge.
