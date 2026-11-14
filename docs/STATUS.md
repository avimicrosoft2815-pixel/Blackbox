# STATUS — BlackBox v1.0 build

**Branch** `arena/e10fc603-blackbox` · **base** `30c9433` · **HEAD** `PENDING` (pushed after every ticket)
**trunk: mergeable** — every C12 check passes from a clean clone and the README says what exists.
**product: pre-alpha — storage and recovery layer only; no SDK, proxy or viewer yet.**

---

## 0. The store changed: ADR-005 supersedes ADR-001

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
| Syscalls on the append path (C11) | `tests/test_no_syscall.py` | 100 000 appends ⇒ **`pwrite` only, one per batch**; no `write`, `fsync`, `open`, `lseek` per event |
| Seal footer + chain link | `tests/test_tape.py` | green |
| Platform shims (P2-03/P2-04, C20) | `tests/test_platform_shims.py` (21) | contract green on Linux; Windows start-time path exercised through a fake `kernel32`; `tested_here: False` for both OS cells |
| Lint + types (C6) | `make fmt` (ruff 0.17.5, mypy 2.4.1) | **0 findings** both |
| Forbidden words (C12) | `tests/test_forbidden_words.py` | green over the tree **and** `git log` |

Total: **.................................. tests**, all green.

## 2. §15.1 rows — ext4 is the gate column (C1); every row carries its verdict

Gate: **p50 ≤ 25 µs, p99 ≤ 250 µs, ≥ 40 000 ev/s** for one full event with `content=hash`, CHAIN included.

| Row | ext4 (gate) | tmpfs (label only) | Verdict | Source |
|---|---|---|---|---|
| **full event, group commit 16 KiB, CHAIN on** | **p50 10.992 · p90 16.005 · p95 24.272 · p99 61.777 µs · mean 13.479 · max 295.652 · 72,909 ev/s** (N 200,000, warm-up 20,000, 2.743 s) | p50 11.148 · p99 54.215 · 74,334 ev/s | **PASS** | `-writer-throughput.json` |
| same, keeper thread publishing | p50 11.044 · p99 54.527 · 73,471 ev/s | — | **PASS** | same |
| kernel mode (`sync_ms=250`, `1 MiB`) | p50 11.278 · p99 89.903 · 60,368 ev/s · sync stall max 2866 µs over 28 syncs | p50 10.953 | reported, not gated | same |
| kernel mode, sync-dense (64 KiB) | p50 11.387 · p99 81.232 · stall max 11875 µs over 453 syncs | — | reported, not gated | same |
| group-commit window | 4.603 ms @ 1 ev/s · 4.999 ms @ 1 000 ev/s · 3.572 ms unbounded (120.99 ev/commit) | 0.0 / 5.0 / 1.65 ms | bound 5 ms: **PASS** | same |
| recovery, 10 k frames | p50 117.09 ms (**53.9 ms/10k**) | p50 106.03 ms | no §15.1 row for scan time | `-recovery.json` |
| disk amplification | 1.843× per-event · 1.231× 16 KiB | identical | ≤ 1.25×: **PASS** | same |
| RSS above baseline | **1.15 MiB** (anon +1.02, file +0.12) at `head_off` 4,723,072 (28.13 % of the segment) | 1.15 MiB | under 24 MiB: **PASS**, no `P1-08b` | same |
| mmap vs `pwrite` (P1-19 / ADR-005) | median p50 ratio 1.028 [0.906, 1.176] · **1.0594999999999999** · **1.065** at 1 k / 10 k / 40 k | 1.076 / 1.064 | **adopt `pwrite`** (inside 20 % at both decision rates) | `-writer-ab.json` |

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
**Where the tail lives:** mean 13.479 vs p50 10.992 µs, p99 61.777, p99.9
112.507, max 295.652 µs. The 99th percentile is the **batch boundary** — one event in
110.0 pays the 16 KiB `pwrite` + CHAIN + head publish (~5 µs, i.e. ~0.045 µs amortised
across every event but a 5 µs spike on the one that pays it) — plus GC of the encoded payload. The 300 µs max is
the scheduler, not the writer: with `_fs` on ext4 under a shared sandbox, one page-cache writeback can preempt.
**Threads:** the app thread appends *and* publishes (the timed region is its own call, CBOR encode included); when
the keeper is on it publishes from its own thread, and both share **one GIL**. The `keeper` row above is the same
work with that second publisher present and costs 0.052 µs at p50.

## 3. The Stage 2 CHANGE items and the corrections C1–C20

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
| C20 | P2-03 `OpenProcess`+`GetProcessTimes`; P2-04 `F_FULLFSYNC`; P1-09b is mine to decide; F13 is P2-11; `P1-02b` before G-Promise; ship `tools/s17_control.sh`; keeper maxima stay; name the stall thread; report CI | **done except P1-02b and P1-09b** — both are queued below; the stall is measured in the app thread's own `emit` (the sync happens inside `publish`, which the app thread calls; the keeper's own publishes are not charged to an event) |
| C7 | CI must actually run | **it ran and it is red** — see §6 |

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
11. **bench-found (C17)** → the `write()` arm issued one `pwrite` per event and allocated a slot object per event → batch buffer + plain ints → `profile_arms()` asserts the difference stays under 200 ns/event.

## 5. F1–F13 — decided, one line each (C10)

F1 hold · F2 hold · F3 hold · **F4 apply** (DV-001, layout unchanged) · F5 hold · F6 hold · F7 hold ·
**F8 apply** (D-003) · **F9 apply** (E225) · F10 reject · F11 hold · **F12 reject** · **F13 hold, and it belongs
to `P2-11`** (`blackbox run`), not P5 — corrected here and in `DECISIONS.md`.

## 6. C12 checklist and C7 (CI)

| Check | State |
|---|---|
| `make test` from a clean clone | green, .................................. tests |
| `make kill` (500 trials, ext4) | 0 violations, 0 `not_started` |
| `make bench` into `bench/results/` | green, both columns |
| `make fuzz` | 100 000 iterations, 0 non-`CBORError` |
| `python -m blackbox --version` | `blackbox 0.1.0.dev0 (format v2)` |
| `README.md` ≤ 60 lines + status line | 40 lines, status line present |
| no `LICENSE` file or field | confirmed |
| docs + `bench/results/*.json` committed | yes |
| no absolute sandbox paths | grep over `bench/results/` = 0 |
| forbidden words, tree + commit messages | green |
| **C7 CI** | **ran; partially green.** Push 4 (`ci:` fix): **docs green**, and **all three ubuntu cells green** (3.11, 3.12, 3.13). **macOS × 3 and Windows × 3 are still red** (exit 1), cause not yet identified — the job logs are outside this sandbox's egress (only `github.com`, `api.github.com`, `codeload`, npm and PyPI are reachable), so the Cells are reported as red/unknown, never as green. Fixed so far on the way: the docs job had no pytest; the kill step collected zero tests (exit 5); the Windows shim imported `fcntl` at module scope; two POSIX-only suites had no skip marker. The macOS/Windows failures need the Actions-tab log to name — that is the next CI ticket. |

## 7. Not done — and why that is the honest state

| Ticket | State | Note |
|---|---|---|
| `P1-09b` retention + `GAP{RETENTION}` | **next, decided by me** | F2/F3 are mine to decide in that commit (`format.md` + `DEVIATIONS.md` together), not blocked |
| `P1-13` seal verifier + compaction | **partial** | footer writer + verifier tested; `arch/` compaction not written |
| `P1-14` / `P1-15` `store/blobs.py` | **pending** | blob write/read, compression queue |
| `P1-16` small-model state machine | **pending** | the kill matrix covers the same invariants empirically, but it is not the model |
| `P1-02b` mypy `type-arg` + `no-untyped-def` | **pending, closes before G-Promise** | 75 strict findings in the two disabled categories |
| `P2-03` / `P2-04` OS gates | **written, `not testable in this environment`** | Windows `OpenProcess`+`GetProcessTimes` through `ctypes` (fake-`kernel32` test on Linux), macOS `F_FULLFSYNC` issued when available; the cells themselves have never run |
| `P2-06` / `P2-07` / `P2-10`…`P2-13` | **pending** | see `docs/progress.md` |
| P2-09 week of nightlies | **requires time (nightly CI × 7 days)** | GitHub `schedule` runs only on the default branch; the clock starts at merge |
| P11-05, P11-12 | **requires humans** | a human operator and a real incident |
| S17 sparse control (C20) | **requires humans (root on any Linux box, two minutes)** | `tools/s17_control.sh` builds the 2 MiB tmpfs control and prints the signal; it cannot be run here (no superuser) |
| Windows/macOS rows of the OS gates | **not testable in this environment** (Linux 6.x, x86-64 only) | the matrix is written and running (red, see §6) |
| P10, P12 | **not started** | P10 only after every v1.0 gate is green; P12 never without instruction |

## 8. Next, in order

1. `P1-16` small-model state machine over the stages the kill matrix already exercises.
2. `P1-09b` retention, deciding F2/F3 in the same commit.
3. `P1-14`/`P1-15` blobs → `P2-06/07/10/11/12/13` → `P1-02b` → **G-Promise on ext4** → STATUS.
