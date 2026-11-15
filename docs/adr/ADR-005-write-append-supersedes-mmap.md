# ADR-005 — the active segment is written with `pwrite`, not mapped (supersedes ADR-001)

*Date:* 2026-11-14 · *Status:* accepted · *Supersedes:* ADR-001 · *Decided by:* the P1-19 rule in plan §8.2, as
restated by C17 · *Raw data:* `bench/results/2026-11-14-writer-ab.json` (`verdict`, `profile`, `results`)

## The rule that was applied

Plan §8.2 / D13: *if a plain `write()`-append writer is **within 20 %** of the mmap writer at the target event
rates, ADR-001 is reopened in favour of the simpler semantics.* C17 fixed the reading: the decision rates are
**10 000 and 40 000 events/s** (at 1 000 ev/s a 5 ms window batches ~5 events, so that row measures syscall
amortisation, not the design), the `write()` arm must be a fair one (one `pwrite` per batch, no per-event copy),
and ties go to `write()`.

## What was measured

Both arms are now **local to the bench** and differ only in the store: the mmap arm encodes straight into a
MAP_SHARED segment; the `pwrite` arm encodes into a preallocated `bytearray` and issues one `pwrite` per batch
plus one for the `CHAIN` frame. Same frame encoder, same 16 KiB group commit, same CHAIN frame, same two-slot
head publish. `pairs` = 10 interleaved A/B pairs per rate on ext4 (order alternating), 1 s blocks:

| offered rate (ext4) | mmap p50 / p99 | `pwrite` p50 / p99 | **p50 ratio** median [min, max] |
|---|---|---|---|
| 1 000 ev/s (reported only) | 8.699 / 124.263 µs | 8.951 / 96.007 µs | 1.028 [0.906, 1.176] |
| **10 000 ev/s (decision)** | 7.560 / 82.133 µs | 8.009 / 71.958 µs | **1.059** [1.030, 1.095] |
| **40 000 ev/s (decision)** | 7.539 / 72.855 µs | 8.027 / 68.380 µs | **1.065** [1.060, 1.074] |

`pwrite` calls per event: **0.0194** (one per ~52 events), i.e. the arm is not paying a syscall per event.
Per-event Python overhead (`profile`, 200 000 events each, publish amortised away): mmap 3 456 ns, `pwrite`
3 623 ns, **difference +167 ns** — under the ~200 ns bar C17 set, so neither arm carries an extra per-event copy.

The ratios are stable across pairs (1.06 ± 0.02 at both decision rates) and the `write` arm's p99 is *lower*
than mmap's at both decision rates. **Verdict: adopt `pwrite`-append.** It is 6 % slower per event and 20 % of
the way inside the rule, which is the trade the plan asked for.

## Why the simpler store is worth 6 %

* **The `SIGBUS` class disappears.** A mapping of a file whose blocks are gone (or beyond EOF) faults on access
  and the kernel kills the process; `pwrite` returns `ENOSPC`/`EDQUOT` and the writer degrades (S17). One
  fail-open path instead of two.
* **One flush path instead of three.** `FlushViewOfFile` + `FlushFileBuffers` on Windows, `msync` + `fdatasync`
  on POSIX, and the page-cache argument — all of that collapses into `fdatasync`.
* **A mapped file cannot be replaced or deleted while the mapping lives.** That was a real constraint on roll
  and on retention, on Windows especially.
* **Nothing about the on-disk bytes changes.** The conformance vectors, the frame layout, the head protocol and
  the scanner are untouched: the same 500-trial kill matrix (plus two new cells) and the crash-stage tests run
  on the new path with zero violations.

## What this does *not* say

It is not a claim that `pwrite` is faster. Per event it is ~6 % slower, and the number is machine-specific
(`requires hardware` in STATUS). The reason to take it is that the *hazard class* it removes cannot be removed
from a mapping, and the plan pre-approved exactly this trade.

## Consequences

* `tape/writer.py` encodes into one batch buffer (`pack_into`) and calls `os.pwrite` once per batch; there is no
  `mmap` in the writer. `mode=process` now means **one syscall per batch**, not "no syscall".
* P1-08's Done-when is "one `pwrite` per batch and no other syscall in process mode" — measured by the counting
  shim in `tests/test_no_syscall.py`.
* §8.2's linearization sentence says the commit is visible **in the `head` file** (it always was; the mapping
  wording is gone).
* The mmap writer is not in `src/` any more. The bench keeps an mmap twin (`MmapAppendTape`) so this decision
  can be re-run and audited; it is a reference, not shipped code.
* ADR-001 stays in the repository as the record of what was replaced and why; it is closed.
