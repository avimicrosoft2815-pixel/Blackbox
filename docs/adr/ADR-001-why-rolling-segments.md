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

**Run 3 — interleaved** (Stage-2 item 8: the arms alternate `mmap, write, write, mmap` per rate, block =
`seconds / 4`, so machine drift hits both arms equally). This is the run the verdict rests on:

| offered rate | p50 ratio (write/mmap) | p99 ratio | CPU ratio |
|---|---|---|---|
| 1 000 ev/s (ext4) | 1.395 | 1.350 | 0.999 |
| 10 000 ev/s (ext4) | **1.233** | 1.339 | 0.891 |
| 40 000 ev/s (ext4) | **1.211** | 1.141 | 1.041 |
| 1 000 / 10 000 / 40 000 (tmpfs) | 0.990 / 1.180 / 1.185 | 0.684 / 1.089 / 0.925 | 0.934 / 0.909 / 0.953 |

Runs 1 and 2 (sequential, not interleaved) gave 1.143 / 1.212 / 1.223 and 1.020 / 1.193 / 1.191. Interleaving
did **not** shrink the effect — it made the direction consistent: at 10 000 and 40 000 ev/s, the `write()` arm
costs 1.18–1.23× the mmap p50 in every interleaved pair on both filesystems. The 1 000 ev/s row is the noisy
one (0.99–1.40 across runs), which is expected: at ~1 ms per deadline the pacing sleep dominates the 12 µs
measurement, so that row says little either way.

The reopen condition was "`write()` within 20 %". At the two rates that matter for §15.1 it is not met.

**Verdict: keep mmap.** Honest caveats:

* the effect at 10 k/40 k ev/s is 1.18–1.23×, i.e. it clears the 20 % bar by a small margin (1.21 vs 1.20 at
  40 k ev/s), so the decision holds but the margin is not overwhelming;
* this is one machine with a virtualised disk; the exact ratio is labelled `requires hardware` in STATUS;
* the pacing loop (`time.sleep(0)` on the deadline) adds microseconds to *both* arms, which is why the absolute
  p50 here (≈12 µs) is larger than the unpaced §15.1 number (≈4.3 µs). Only the ratio is being read.

Nothing about this measurement changes a byte of the format, so no `DEVIATIONS.md` entry is needed; D-013
records the decision and the caveat.

