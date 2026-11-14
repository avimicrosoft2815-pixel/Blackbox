# BlackBox build status — 2026-11-14 — trunk `arena/e10fc603-blackbox` — gates: G-Promise **not green**, others not started

## Fulfilled (test exists, ran, passed / number measured)

| Gate or budget row | Plan ref | Evidence (test path, results JSON, or CI run URL) |
|---|---|---|
| A1 ledger generated from the plan | A1 | `tools/gen_progress.py` → `docs/progress.md`; 148 plan rows parsed |
| A2 publish via a mapped head, no syscall in process mode | §8.2 | `tape/head.py::HeadMap`, `tests/test_crash_points.py` (S4) |
| A3 per-stage microbench protocol | P1-17 | `bench/bench_frame.py` → `bench/results/2026-11-14-frame.json`, N=100 000, warm-up 10 000 |
| A4 S15 adoption, twice, writer and discover | §16.2a S15 | `tests/test_adoption.py` (7 tests) |
| A5 one meaning per code, registry vs plan appendix | P0-11 | `tests/test_errors.py` (8 tests), `docs/errors.md` generated |
| A6 host fingerprint hashes a hostname | §8.2 `0x048` | `tests/test_codec.py::test_host_fingerprint_hashes_a_real_hostname` (`'e2b.local\|Linux\|x86_64'`) |
| A7 magics frozen in hex | §8.3 | `docs/format.md`, plan §8.3, `tests/test_codec.py::test_magic_fields_are_exactly_their_width` |
| A8 kill evidence | P2-08, P2-09 | `docs/kill-matrix.md`, `bench/results/2026-11-14-kill-random.json` |
| A9 regression tests for the two fixed bugs | A9 | magic width; `tests/test_tape.py::test_roll_exactly_at_chain_emission_keeps_seq_unique_and_dense` |
| content=hash 1 KiB full event p50 ≤ 25 µs, p99 ≤ 250 µs, CHAIN included | §15.1 | **p50 11.7 µs / p99 46.7 µs at 16 KiB group commit**; 27.8 µs / 77 µs when every event is its own commit — `bench/results/2026-11-14-writer-throughput.json` |
| ≥ 40 000 events/s single thread | §15.1 | **69 254 ev/s** (group commit, CHAIN on); 30 175 ev/s per-event commit — same file |
| recovery ≤ 120 ms per 10 k frames | §15.1 | **63.5 – 66.3 ms per 10 k frames** (16–29 k frames on tape) — `bench/results/2026-11-14-recovery.json` |
| RSS ≤ 24 MiB | §15.1 | **21.4 – 21.5 MiB** peak in a write-only process, 10 k events, 16 MiB segment |
| disk amplification ≤ 1.25× | §15.1 | **1.231×** at 16 KiB group commit (1.263× at 4 KiB, 1.843× per-event) — same file |
| S1–S5, S14, S15 | §16.2a | `tests/test_crash_points.py`, 7 stages green (process mode, 64 KiB segments) |
| randomized kills, 0 invariant violations | P2-09 | 500 parent-`SIGKILL` trials, 4 configs × 2 batch settings, 0 violations |
| 500-trial matrix found and fixed 4 defects | P2-09 | seeds 352, 388, 209/307/386, 396 — `docs/kill-matrix.md` |

## Labelled

| Item | Label | Reason / fix attempts |
|---|---|---|
| P2-09 "0 violations over a week of nightly runs" | requires humans | 500 trials ran here in 128 s; a week of nightly CI is the remaining evidence |
| S16 (`poweroff -f`), S17 (`SIGBUS`) | not testable in this environment | needs a VM with `dm-flakey`/`dm-error`; the `SIGBUS` claim stays unmade until S17 runs |
| Python 3.12 / 3.13, macOS, Windows cells | not testable in this environment | this sandbox has CPython 3.11.2 on Linux only; CI matrix is written but has not run |
| `ruff`, `mypy --strict` | not testable in this environment | not installed (no-install rule); D-002 |
| Recovery ≤ 120 ms row on Windows/macOS | not testable in this environment | named OS/hardware |
| P1-19 A/B (mmap vs `write()`), P1-16 model test, P1-14/15 blobs, P1-09b retention, P2-03/04, P2-06 … P2-13 | blocked (not started) | ordered work remains; no attempt made yet, so nothing is "three attempts failed" |

## Measurements

Intel Xeon @ 2.60 GHz · Linux 6.1.158 x86_64 · CPython 3.11.2 · tmpfs · N and warm-up per file

- frame stages — `bench/results/2026-11-14-frame.json` · N=100 000, warm-up 10 000: CRC32C 110.5 µs/KiB, IEEE CRC 0.45 µs/KiB, CBOR event encode 8.6 µs, frame encode 2.6 µs (26 B) / 2.8 µs (1 KiB), head publish 7.0 µs, append 2.6 µs, append+publish with CHAIN 12.9 µs
- writer — `bench/results/2026-11-14-writer-throughput.json` · 200 000 events, warm-up 20 000: full event + CHAIN p50 11.7 µs / p95 / p99 46.7 µs at 69 254 ev/s
- recovery/RSS/amplification — `bench/results/2026-11-14-recovery.json` · 10 000 events: recovery 63.5 ms/10 k frames (p50), RSS 21.51 MiB, amplification 1.231×
- kills — `bench/results/2026-11-14-kill-random.json` · 500 trials, seed 20261114, 0 violations

## Deviations applied (DEVIATIONS.md) and held findings

- DV-001 F4 applied (P1-13, layout unchanged). DV-002 magics in hex. DV-003 `host_fingerprint` via `platform.node()`. DV-004 `BBX-E140` → `BBX-E132`/`E133`.
- Held, untouched, awaiting a word: **F1, F2, F3, F5, F6, F7, F8, F9, F10, F11, F12, F13.**

## Decisions taken with plan defaults

- D-001 `>=3.11` + setuptools. D-002 dev tools installed: pytest, hypothesis only. D-003 F8 re-scoped. D-004 contract files. D-005 head mapped once. D-006 magic padding. D-007 IEEE CRC default. D-008 16 KiB group commit. D-009 generated ledger.

## What is next

1. P1-10 (degraded modes beyond `ENOSPC`: `EACCES`, `IOError`), P1-03 (100 k-buffer CBOR fuzz), P1-13 re-seal idempotence and `BBX-E224` offset reporting.
2. P1-16 (small-model state machine), P1-19 (mmap vs `write()` A/B → ADR-001 verdict), P1-09b retention + `GAP`.
3. P1-14/P1-15 blobs, then P2-06/07/10/11/12/13 (recovery state machine, capsule v0, launcher, idempotence) → G-Promise re-run.
4. P2-03 `platform/windows.py` and P2-04 `platform/macos.py` written against the documented API and labelled.
5. Then L2/L3 (capture seams, proxy, launcher) — the first phase that needs sockets, and therefore the first that touches `src/blackbox/proxy/server.py`.
