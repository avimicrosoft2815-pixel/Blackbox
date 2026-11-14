# STATUS — BlackBox v1.0 build

**Branch** `arena/e10fc603-blackbox` · **base** `30c9433` · **HEAD** `820d595` (+ this file's commit)
**Phase:** P0, P1 (partial), P2 (partial) — *storage and recovery layer only; no SDK, proxy or viewer yet.*
**Not merge-ready.** Six of the plan's P1/P2 tickets are still `pending`; see §7.

---

## 1. What is actually green (each row: a test that ran here)

| Area | Evidence | Result |
|---|---|---|
| Canonical CBOR | `tests/test_codec.py` (round-trip, canonical order, limits) | 42 tests green |
| CBOR decoder hardening (P1-03) | `bench/fuzz_cbor.py` → `bench/results/2026-11-14-fuzz-cbor.json` | 100 000 buffers, **0 non-`CBORError` exceptions** |
| Frame encode/decode, CRC scope | `tests/test_frame.py`, `tests/test_conformance.py` | green |
| Checksum (one algorithm, C4) | `tests/conformance/vectors/crc32.json` (7 vectors) | check value `0xCBF43926`, 0.58 µs/KiB |
| Head, two-slot publish, rebuild | `tests/test_head_creation.py` | green |
| Segmentation, roll, adoption (S15) | `tests/test_adoption.py` (7), `tests/test_tape.py` | green, adoption idempotent in writer **and** `rec/discover.py` |
| Crash points S1–S5, S14, S15 | `tests/test_crash_points.py` | green |
| Crash states read back (seeds 388/209/396) | `tests/test_scan_states.py` | green |
| Randomized parent-`SIGKILL` (P2-09) | 500 trials ext4 **and** 500 tmpfs → `bench/results/2026-11-14-kill-random.json` | **0 violations, 0 not_started**, both columns |
| ENOSPC at preallocation (S17) | `tests/test_enospc.py`, `tests/test_prealloc.py` | app exits 0, stdout identical, tape shows `DEGRADED` |
| Group-commit window + keeper | `tests/test_keeper.py` (6) | window ≤ 5 ms measured at 1 / 1 000 / unbounded ev/s |
| No syscall on the append path (C11) | `tests/test_no_syscall.py` | 100 000 appends, **0** `write/pwrite/fsync/fdatasync/open/openat` |
| Seal footer + chain link | `tests/test_tape.py` seal/chain tests | green |
| Lint + types (C6) | `make fmt` (ruff 0.17.5, mypy 2.4.1) | **0 findings** both |
| Forbidden words (C12) | `tests/test_forbidden_words.py` | green over the tree **and** `git log` |

Total: **157 tests**, all green (`make test`).

## 2. §15.1 rows — gate column is ext4 (C1), tmpfs is the labelled second column

Same protocol on both filesystems, both written into one JSON. Gate: **p50 ≤ 25 µs, p99 ≤ 250 µs,
≥ 40 000 ev/s** for a full event with `content=hash`, CHAIN included.

| Row | ext4 (gate) | tmpfs (label only) | Source |
|---|---|---|---|
| writer event, group commit 16 KiB, chain on | **p50 4.28 µs · p99 42.0 µs · 80 620 ev/s** | p50 4.22 µs · p99 32.8 µs · 86 477 ev/s | `2026-11-14-writer-throughput.json` |
| writer event, publish per event | p50 4.26 µs · p99 16.6 µs · 86 619 ev/s | p50 4.23 µs · p99 16.2 µs · 83 557 ev/s | same |
| kernel mode (`sync_ms=250`, `sync_bytes=1 MiB`) | p50 4.27 µs · p99 57.3 µs · 79 492 ev/s · **sync stall max 2.88 ms** (28 syncs) | p50 4.35 · p99 55.7 · stall max 0.17 ms | same |
| kernel mode, sync-dense (`sync_bytes=64 KiB`) | p50 4.23 · p99 47.4 · **stall max 1.88 ms** (454 syncs) | p50 4.39 · p99 36.5 · stall max 0.20 ms | same |
| group-commit window | 4.45 ms @ 1 ev/s · 4.999 ms @ 1 000 ev/s · 3.85 ms unbounded (121 ev/commit) | 0.0 / 5.0 / 1.65 ms | same |
| recovery, 10 k frames | p50 134.9 ms (**56.3 ms per 10k**), p99 139.6 ms | p50 137.0 ms (50.8 ms/10k) | `2026-11-14-recovery.json` |
| disk amplification | 1.219× per-event · 1.263× 4 KiB · **1.231× 16 KiB** · 1.222× 64 KiB | identical (frame-size property) | same |
| RSS above baseline at a full 16 MiB segment (C8) | **5.15 MiB** (anon +0.37, file +4.79) | 5.30 MiB | same |
| stage table | crc32 1 KiB 0.58 µs · blake2b 1 KiB 1.06 µs · cbor encode 8.62 µs · frame encode 3.07 µs · memcpy 0.22 µs · head publish 1.21 µs | within noise | `2026-11-14-frame.json` |
| mmap vs `write()` (P1-19, ADR-001) | p50 ratio 1.143 / 1.212 / 1.223 at 1 k / 10 k / 40 k ev/s | 1.020 / 1.193 / 1.191 | `2026-11-14-writer-ab.json` |

**C2 — the measured interval, stated once:** it starts from a Python `dict` holding a 1 KiB `content` field,
with `content=hash` (BLAKE2b-256 of the body), and includes the CBOR encode, the frame encode, the append into
the active segment and the amortised **publish + CHAIN**. Redaction arrives at G-Capture, so this is a
**writer-level lower bound for the §15.1 row**. The bench does not pre-encode, and the stages are **not
additive** (the CBOR encode allocates, the frame writes into the mapping) — `bench_frame.py` does not claim to
sum to the event number, and the JSON says so.

## 3. The thirteen corrections (C1–C13), one line each

| # | What was asked | State |
|---|---|---|
| C1 | ext4 is the gate column; both columns in one JSON; kernel rows | **done** — §2, `bench/_fs.py`, kernel rows with `sync_ms`/`sync_bytes`/syncs/stall |
| C2 | define the measured interval | **done** — §2, and in the JSON's `interval` field |
| C3 | bounded group-commit window + keeper, measured | **done** — 4 exits (bytes, ms, priority, flush/close), `tape/keeper.py`, maxima in §2 |
| C4 | one checksum, `DV-005`, `ADR-004`, rename, delete CRC32C, fix P1-01 row | **done** — `cd415c1` |
| C5 | S17 is testable here; relabel S16/S17 | **done** for the injected case; the **sparse control case is `not testable in this environment`** (needs `mount -t tmpfs -o size=2m`; no superuser). Claim is exactly "injected fault, control case pending" |
| C6 | `ruff` + `mypy --strict` allowed and run | **done** — 0 / 0; mypy has two knobs off (`type-arg`, `no-untyped-def`) → row `P1-02b` |
| C7 | CI must actually run | **not run — run status unknown, check the Actions tab.** Nothing was pushed this run (the owner pushes), so there is no run to report; the clock for P2-09's nightlies starts at merge |
| C8 | RSS above baseline, anon/file split, threshold 24 MiB | **done** — 5.15 MiB, so no `P1-08b` sub-ticket is needed |
| C9 | one line per P2-09 defect | **done** — §4 |
| C10 | F1–F13 decided, not waited on | **done** — §5 |
| C11 | syscall evidence for "no syscall in process mode" | **done** — counting shim (strace is not installable: no root) |
| C12 | merge-ready trunk | **partly** — §6; the trunk checks pass, the *product* is not merge-ready |
| C13 | this order of work | followed through P1-19; the rest is in §7 |

## 4. Defects found and fixed (C9 format: seed → invariant broken → root cause → fix → regression test)

1. **seed 352** → recovery demanded a `SEG_END` frame → adoption refused a legal crash state (roll skips
   `SEG_END` when it does not fit) → seal exactly `[4096, head_off)` → `test_adoption_does_not_require_a_seg_end_frame`.
2. **seed 388** → a segment was created in place, so a kill left a half-written header (`E133` forever) → tmp +
   `fdatasync` + `replace` + `fsync(dir)` → `test_segment_creation_is_atomic`.
3. **seeds 209 / 307 / 386** → the scanner called initialisation corruption → an empty tape before `meta` is a
   legal state → `test_scan_of_an_empty_tape_is_not_corruption`.
4. **seed 396** → "holds frames" was judged by file size → a preallocated empty segment looked full → decode a
   frame instead → `test_segment_without_frames_is_not_adopted`.
5. **seed 120 (tmpfs)** → a kill during head creation left `segments exist but the head is unreadable` → (a) the
   head file is created tmp + `ftruncate` + `fsync` + `replace` + `fsync(dir)`; (b) `repair.rebuild_head`
   reconstructs the head from the newest segment, continuing from the highest readable `gen` →
   `test_head_file_is_created_atomically`, `test_unreadable_head_is_rebuilt_from_the_segments`,
   `test_rebuild_is_idempotent`.
6. **bench-found, no seed** → the keeper thread and the app thread could both enter `roll()`, creating segment
   `n+1` twice (`BBX-E133`) → publish/flush/roll/close are serialised by a re-entrant lock →
   `test_keeper_and_app_thread_never_roll_twice`.
7. **P1-03 fuzz** → `struct.error` escaped the CBOR decoder on short inputs (`\x1b`, `\x5b`, `\xfb…`) → every
   multi-byte field is bounds-checked before unmarshalling →
   `test_truncated_headers_raise_cbor_error_not_struct_error`.
8. **S17** → `roll()` raised `BBX-E132` into the app and had already unmapped the active segment → reserve space
   for `n+1` **before** closing `n`; degrade in place on `PreallocError` → `tests/test_enospc.py` (DV-006).

## 5. F1–F13 — decided, one line each (C10)

| # | Decision | Why |
|---|---|---|
| F1 | **hold** | Redaction on compressed bodies is a P3B decoder question; nothing in v1 writes compressed bodies yet |
| F2 | **hold** | Retention must not delete an open run: that is P1-09b, still pending; recorded there, not silently dropped |
| F3 | **hold** | `GAP` carrying the chain link is a P1-09b byte decision; it must land with the retention code and a `format.md` update in the same commit |
| F4 | **apply** (DV-001) | `.seal` `body_len`/`body_crc32`/`body_hash` are defined over the **uncompressed** body; layout unchanged |
| F5 | **hold** | Seam C coverage claim is P3-capture; no capture exists yet |
| F6 | **hold** | SDK stream-object contract is P3; nothing to change before the seam exists |
| F7 | **hold** | Trigger capsule naming collides with §9.3/§13.3; P3/P6 territory |
| F8 | **apply** (D-003) | Keeper pre-creation of `<n+1>.seg` is the default; the synchronous roll stays measurable |
| F9 | **apply** | A frame larger than a segment is rejected at append with `BBX-E225` (test green) instead of being undefined |
| F10 | **reject** | Plan arithmetic is the owner's ledger; recomputing P10 hours is not this build's business |
| F11 | **hold** | Residual 1.0 wording lives in the frozen plan; editing it is the owner's call |
| F12 | **reject** | `head.sealed_seq` stays: it is a hint the scanner cross-checks against the `.seal`, not a second truth |
| F13 | **hold** | `blackbox run` does not exist yet (P5); when it does, the skip must be a warning, not silence |

None of the held items changes a frozen byte or reverses a §3 decision, so nothing here needed a
`DEVIATIONS.md` entry; each held item is pinned to the ticket that will decide it.

## 6. C12 — merge-ready trunk checklist

| Check | State |
|---|---|
| `make test` from a clean clone | green, 157 tests |
| `make kill` (500 trials, ext4) | 0 violations |
| `make bench` into `bench/results/` | green, both columns |
| `make fuzz` | 100 000 iterations, 0 non-`CBORError` |
| `python -m blackbox --version` | `blackbox 0.1.0.dev0 (format v2)` |
| `README.md` ≤ 60 lines + status line | **40 lines**, exact status line present |
| `pyproject.toml` version | `0.1.0.dev0` |
| no `LICENSE` file, no `license` field | confirmed — the owner has not chosen a licence, and this file says so |
| `docs/STATUS.md`, `docs/progress.md`, `docs/kill-matrix.md`, `bench/results/*.json` committed | yes |
| no absolute sandbox paths or usernames | checked by `tests/test_forbidden_words.py`-adjacent review; paths in JSONs are scratch dirs that are deleted |
| forbidden words, tree + commit messages | green (`tests/test_forbidden_words.py`) |

The trunk is mergeable. **The product is not**: the tickets in §7 are the difference.

## 7. Not done — and why that is the honest state

| Ticket | State | Note |
|---|---|---|
| `P1-09b` retention + `GAP{RETENTION}` | **pending** | needs F2/F3 decisions above; nothing deletes anything today, which is safe but not a feature |
| `P1-13` seal verifier + compaction | **partial** | footer writer and verifier exist and are tested; `arch/` compaction is not written |
| `P1-14` / `P1-15` `store/blobs.py` | **pending** | blob write/read, compression queue |
| `P1-16` small-model state machine | **pending** | the randomized kill matrix covers the same invariants empirically for 1 000 trials, but it is not the model |
| `P2-03` / `P2-04` `platform/windows.py`, `platform/macos.py` | **pending** | POSIX path is done; Windows/macOS writers are not |
| `P2-06` / `P2-07` / `P2-10`…`P2-13` | **pending** | see `docs/progress.md` |
| `P1-02b` mypy `type-arg` + `no-untyped-def` | **pending** | 75 strict findings, all in the two disabled categories |
| P2-09 week of nightlies | **requires time (nightly CI × 7 days)** | GitHub `schedule` only runs on the default branch: the clock starts at merge |
| P11-05 five-minute test, P11-12 diagnosis benchmark | **requires humans** | they need a human operator and a real incident |
| sparse-file `SIGBUS` control case (C5) | **not testable in this environment** | needs `mount -t tmpfs -o size=2m`; this sandbox has no superuser |
| Windows/macOS rows of every OS-specific gate | **not testable in this environment** (Linux 6.x, x86-64 only) | the CI matrix is written but has never run (C7) |
| P10, P12 | **not started** | P10 only after every v1.0 gate is green; P12 never without instruction |

## 8. Next three things, in order

1. `P2-03`/`P2-04`: platform modules for Windows and macOS, because two of the pending gates are only blocked
   on "no writer for that OS".
2. `P1-09b` retention with F2/F3 decided in the same commit (format + `DEVIATIONS.md`).
3. `P1-16` small-model state machine, then `P1-14`/`P1-15` blobs.
