# ADR-001 — rolling segments, active segment mmap'd

**Status:** accepted 2026-11-14 · **Reopen condition tested:** P1-19 (see §4) · **Affects:** §8.1, §8.6, §8.7

## 1. Decision

The tape is a directory of fixed-size segment files. Exactly one — the **active** segment — is `mmap`'d and
appended by a slice assignment into that mapping. When it fills, the writer appends `SEG_END`, seals it with a
256-byte `.seal` footer (immutable from that moment), and rolls to a freshly preallocated `<n+1>.seg`.
Retention deletes whole sealed segments, oldest first, never bytes inside an open one.

## 2. Alternatives and why they lost

| Alternative | Why it was rejected |
|---|---|
| Single mmap ring + spill (the 1.0 design) | In-place overwrite of the oldest run destroys evidence before anyone reads it; the keeper had to *copy* closed runs out of the ring (2× write amplification, a second code path to kill-test) |
| Plain `write()`-append segments | Measured, not assumed — see §4. Close, but slower than the mapping at two of the three target rates, and it buys nothing the mapping does not already give: a `write()` lands in the page cache exactly like a mapping store |
| SQLite / single-file DB | A database is a second storage engine with its own crash semantics, its own locking, and it rewrites pages — the frame-level CRC/chain/fail-open design would be layered on top of someone else's recovery, and every reader would need SQLite |
| JSONL append | No length or CRC to distinguish a torn tail from a short line; one process per file at best; no room for the head/frame split |

## 3. What mmap buys, and what it does not

It buys no-syscall appends (C11 counts zero `write`/`pwrite`/`fsync`/`openat` over 100 000 appends in
`process` mode) and cross-process visibility of the active segment through a shared second slot in `head`.

It does **not** buy durability. Any `write()` to a regular file also lands in the page cache and survives
process death; `fdatasync` on the group-commit boundary is what upgrades `kernel`/`paranoid` mode.

It also does not give permission to make segments sparse: a page fault inside a sparse mapping with no space
left raises `SIGBUS` and kills the user's process. Segments are therefore fully preallocated
(`posix_fallocate`, or zero-fill where that is unavailable) — S17 / `tests/test_enospc.py`.

## 4. P1-19 verdict (measured 2026-11-14)

`bench/bench_writer_ab.py`, JSON in `bench/results/2026-11-14-writer-ab.json`. Both arms use the same encoder,
the same 16 KiB group commit, the same CHAIN frame and the same two-slot head publish; only the store differs.
Gate column is ext4; tmpfs is the labelled second column.

| offered rate | p50 ratio (write/mmap) | p99 ratio | CPU ratio |
|---|---|---|---|
| 1 000 ev/s | 1.143 | 0.979 | 0.958 |
| 10 000 ev/s | **1.212** | 1.132 | 0.898 |
| 40 000 ev/s | **1.223** | 1.257 | 1.085 |

The reopen condition was "`write()` within 20 %". It is not: at 10 000 and 40 000 ev/s the `write()` arm is
1.21–1.22× the mmap p50, and its p99 at 40 000 ev/s is 1.26×. CPU is within noise in both directions (0.90–1.09),
so the difference is the syscall, not the hashing.

**Verdict: keep mmap.** Honest caveats: this is a close call (1.223 against a 1.20 threshold), it was measured
in one environment (this sandbox's ext4 on a virtual disk), and the pacing loop (`time.sleep(0)` on the
deadline) adds several microseconds to *both* arms — which is why the absolute p50 here (≈12 µs) is larger than
the unpaced §15.1 writer number (≈4.3 µs). The ratio, not the absolute value, is what this ticket decides.

If a future measurement on real hardware puts `write()` inside 20 %, the reopen condition is met and the
mapping should be dropped: it would remove the mapping lifetime, the `SIGBUS` class and the Windows
`FlushViewOfFile` path from the design.
