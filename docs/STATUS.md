# STATUS — BlackBox v1.0 build

**Branch** `arena/e10fc603-blackbox` · **base** `30c9433` · **HEAD** `b3c4e3c`
**Phase:** P0, P1 (partial), P2 (partial) — *storage and recovery layer only; no SDK, proxy or viewer yet.*
**Not merge-ready.** The plan's P1/P2 tickets that are still `pending` are listed in §7, with the reason for each.

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
| Randomized parent-`SIGKILL` (P2-09) | 500 trials ext4 **and** 500 tmpfs → `bench/results/2026-11-14-kill-random.json` | **0 violations** in both columns; 10/500 (ext4) and 6/500 (tmpfs) trials died before the child created its tape and are counted as `not_started`, never as passes |
| ENOSPC at preallocation (S17) | `tests/test_enospc.py`, `tests/test_prealloc.py` | **both segments and the first one**: app exits 0, stdout identical, tape shows `DEGRADED`; a writer that cannot get its first segment is a sink (`recording=False`, drops counted), never an exception into the app |
| Group-commit window + keeper | `tests/test_keeper.py` (6) | window ≤ 5 ms measured at 1 / 1 000 / unbounded ev/s |
| No syscall on the append path (C11) | `tests/test_no_syscall.py` | 100 000 appends, **0** `write/pwrite/fsync/fdatasync/open/openat` |
| Seal footer + chain link | `tests/test_tape.py` seal/chain tests | green |
| Platform shims (P2-03/P2-04) | `tests/test_platform_shims.py` (12) | contract green on Linux; `capabilities()["tested_here"]` is `False` for Windows and macOS, which have **never executed** here |
| Lint + types (C6) | `make fmt` (ruff 0.17.5, mypy 2.4.1) | **0 findings** both |
| Forbidden words (C12) | `tests/test_forbidden_words.py` | green over the tree **and** `git log` |

Total: **171 tests**, all green (`make test`).

## 2. §15.1 rows — gate column is ext4 (C1), tmpfs is the labelled second column

Same protocol on both filesystems, both written into one JSON. Gate: **p50 ≤ 25 µs, p99 ≤ 250 µs,
≥ 40 000 ev/s** for a full event with `content=hash`, CHAIN included.

| Row | ext4 (gate) | tmpfs (label only) | §15.1 verdict | Source |
|---|---|---|---|---|
| **full event, group commit 16 KiB, CHAIN on** (the gate row) | **p50 4.457 µs · p95 9.877 µs · p99 45.057 µs · 75,672 ev/s** | p50 4.41 · p95 9.347 · p99 31.177 · 78,285 ev/s | **PASS** (25 / 250 µs, 40 000 ev/s) | `2026-11-14-writer-throughput.json` |
| full event, publish per event | p50 4.366 · p99 26.957 · 76,871 ev/s | p50 4.329 · p99 14.06 | not the gate row (no group commit) | same |
| kernel mode (`sync_ms=250`, `sync_bytes=1 MiB`) | p50 4.589 · p99 63.628 · 69,770 ev/s · sync stall max 5991 µs (28 syncs) | p50 4.427 · stall max 136 µs | reported, not gated (F2 durability path) | same |
| kernel mode, sync-dense (`sync_bytes=64 KiB`) | p50 4.534 · p99 54.652 · stall max 3573 µs (454 syncs) | p50 4.468 · stall max 440 µs | reported, not gated | same |
| group-commit window | 4.374 ms @ 1 ev/s · 4.998 ms @ 1 000 ev/s · 4.178 ms unbounded (120.93 ev/commit) | 0.0 / 5.0 / 1.65 ms | bound is 5 ms: **PASS** | same |
| recovery, 10 k frames | p50 120.25 ms · p99 124.34 ms (**58.0 ms/10k**) | p50 159.17 ms | no §15.1 row exists for scan time; reported for P1-11 | `2026-11-14-recovery.json` |
| disk amplification | 1.843× publish-per-event · 1.263× 4 KiB · **1.231× 16 KiB** · 1.222× 64 KiB | identical (frame-size property) | ≤ 1.25× at 16 KiB: **PASS** | same |
| RSS above baseline at a full 16 MiB segment (C8) | **5.15 MiB** (anon +0.25, file +4.9) | 5.05 MiB | under the 24 MiB threshold: **PASS**, no `P1-08b` | same |
| stage table | crc32 1 KiB 0.576 µs · blake2b 1 KiB 1.989 µs · cbor encode 9.14 µs · frame encode 3.089 µs · memcpy 0.219 µs · head publish 1.21 µs | within noise | stages are non-additive (see C2) | `2026-11-14-frame.json` |
| mmap vs `write()` (P1-19, ADR-001) | interleaved p50 ratio 1.395 / 1.233 / 1.211 at 1 k / 10 k / 40 k ev/s | 1.18 / 1.185 at 10 k / 40 k | keep mmap; exact ratio `requires hardware` | `2026-11-14-writer-ab.json` |

**C2 — the measured interval, stated once:** it starts from a Python `dict` holding a 1 KiB `content` field,
with `content=hash` (BLAKE2b-256 of the body), and includes the CBOR encode, the frame encode, the append into
the active segment and the amortised **publish + CHAIN**. Redaction arrives at G-Capture, so this is a
**writer-level lower bound for the §15.1 row**. The bench does not pre-encode, and the stages are **not
additive** (the CBOR encode allocates, the frame writes into the mapping) — `bench_frame.py` does not claim to
sum to the event number, and the JSON says so.

## 3a. The Stage 2 brief's nine CHANGE items

| # | Item | State |
|---|---|---|
| 1 | P1-09, P1-13, P1-18 in Stage 2; order; CHAIN in the hot path; retention out | **yes** — roll, seal footer and the CHAIN frame are in, and every gate number includes CHAIN. Retention is still out and labelled "no-retention path" |
| 2 | D1: `pytest` + `hypothesis` pinned in `[project.optional-dependencies].dev`; P0-13 fails if `src/` imports them | **yes** — `pytest==8.3.4`, `hypothesis==6.122.3`; `tests/test_stdlib_only.py` compares every import against `sys.stdlib_module_names` |
| 3 | D2 + D7: floor `>= 3.11`, `tomllib`, compatibility doc, plan deviation note | **yes** — `requires-python = ">=3.11"`, plan P0-01 row + Appendix A amendment, `docs/compatibility.md` row for 3.10 |
| 4 | E3: ubuntu/macos/windows workflow files; those gates never reported as passed | **yes** — `.github/workflows/{ci,nightly}.yml` with a 3.11/3.12/3.13 × 3-OS matrix, `fail-fast: false`; never run (see C7) |
| 5 | D3/D5/D6: attach.md later, commit per ticket, never push | **yes** — no `docs/attach.md`, one commit per ticket, nothing pushed |
| 6 | D4: F1–F13 — F4 only if layout-safe, F8 re-scoped, the rest held | **yes** — §5; F4 is DV-001 (layout unchanged), F8 is D-003 |
| 7 | S17 wording; S17 covers the **first** preallocation too | **yes, now** — `open()` degrades to a sink instead of raising (DV-007/D-014). No report says "hazard class reproducible"; the claim is "injected fault, control case pending" |
| 8 | Measurement discipline: a row per ticket, p50/p95/p99 with N and warm-up and the JSON path, interleaved P1-19, a verdict per §15.1 row | **yes** — `docs/progress.md` §"Hot-path numbers", §2's verdict column, `bench_writer_ab.py` interleaves `mmap, write, write, mmap` |
| 9 | Mid-stage checkpoint STAGE 2A REPORT, then stop | **done earlier** — `docs/stage-2a-report.md`; the owner's reply superseded the stop, so the build continued under A1–A9 and C1–C13 |

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
| C13 | this order of work | followed through P1-19 and `P2-03`/`P2-04`; the rest is in §7 |

## 4. Defects found and fixed (C9 format: seed → invariant broken → root cause → fix → regression test)

1. **seed 352** → recovery demanded a `SEG_END` frame → adoption refused a legal crash state (roll skips
   `SEG_END` when it does not fit) → seal exactly `[4096, head_off)` → `test_adoption_does_not_require_a_seg_end_frame`.
2. **seed 388** → a segment was created in place, so a kill left a half-written header (`E133` forever) → tmp +
   header + `fdatasync` + `replace` + `fsync(dir)` → `test_segment_creation_publishes_the_header_atomically`.
3. **seeds 209 / 307 / 386** → the scanner called initialisation corruption → an empty tape before `meta` is a
   legal state → `test_scan_of_an_empty_tape_is_not_corruption` (`tests/test_scan_states.py`).
4. **seed 396** → "holds frames" was judged by file size → a preallocated empty segment looked full → decode a
   frame instead → `test_a_preallocated_segment_without_frames_is_not_treated_as_full`.
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
| `make test` from a clean clone | green: cloned to `/tmp/clone2` at `e15a680` and ran **171 tests**, 3.9 s, dev venv only |
| `make kill` (500 trials, ext4, from the clean clone) | 0 violations |
| `make bench` into `bench/results/` | green, both columns, from the clean clone |
| `make fuzz` | 100 000 iterations, 0 non-`CBORError` (clean clone too) |
| `python -m blackbox --version` | `blackbox 0.1.0.dev0 (format v2)` |
| `README.md` ≤ 60 lines + status line | **40 lines**, exact status line present |
| `pyproject.toml` version | `0.1.0.dev0` |
| no `LICENSE` file, no `license` field | confirmed — the owner has not chosen a licence, and this file says so |
| `docs/STATUS.md`, `docs/progress.md`, `docs/kill-matrix.md`, `bench/results/*.json` committed | yes |
| no absolute sandbox paths or usernames | bench JSONs label scratch dirs relatively (`.bench/scratch`, `<scratch>/…`); checked by grep over `bench/results/` |
| forbidden words, tree + commit messages | green (`tests/test_forbidden_words.py`) |

The trunk is mergeable. **The product is not**: the tickets in §7 are the difference.

## 7. Not done — and why that is the honest state

| Ticket | State | Note |
|---|---|---|
| `P1-09b` retention + `GAP{RETENTION}` | **pending** | needs F2/F3 decisions above; nothing deletes anything today, which is safe but not a feature |
| `P1-13` seal verifier + compaction | **partial** | footer writer and verifier exist and are tested; `arch/` compaction is not written |
| `P1-14` / `P1-15` `store/blobs.py` | **pending** | blob write/read, compression queue |
| `P1-16` small-model state machine | **pending** | the randomized kill matrix covers the same invariants empirically for 1 000 trials, but it is not the model |
| `P2-03` / `P2-04` OS gates | **written, `not testable in this environment`** | `platform/windows.py` and `platform/macos.py` exist, answer the same eight questions as `posix`, and publish `capabilities()` that says `tested_here: False`. Three decisions matter and are documented there: preallocation is **zero-fill** on both (a sparse segment must never be mapped), directory fsync is a no-op with a reason, and Windows has no process-start token so the lease falls back to mtime staleness rather than trusting a PID. `tests/test_platform_shims.py` (12 cases) tests the contract on Linux; the OS cells themselves have never run (C7) |
| `P2-06` / `P2-07` / `P2-10`…`P2-13` | **pending** | see `docs/progress.md` |
| `P1-02b` mypy `type-arg` + `no-untyped-def` | **pending** | 75 strict findings, all in the two disabled categories |
| P2-09 week of nightlies | **requires time (nightly CI × 7 days)** | GitHub `schedule` only runs on the default branch: the clock starts at merge |
| P11-05 five-minute test, P11-12 diagnosis benchmark | **requires humans** | they need a human operator and a real incident |
| sparse-file `SIGBUS` control case (C5) | **not testable in this environment** | needs `mount -t tmpfs -o size=2m`; this sandbox has no superuser |
| Windows/macOS rows of every OS-specific gate | **not testable in this environment** (Linux 6.x, x86-64 only) | the CI matrix is written but has never run (C7) |
| P10, P12 | **not started** | P10 only after every v1.0 gate is green; P12 never without instruction |

## 8. Next three things, in order

1. `P1-16` small-model state machine over the crash stages that `tests/kill/kill_matrix.py` already exercises.
2. `P1-09b` retention with F2/F3 decided in the same commit (format + `DEVIATIONS.md`).
3. `P1-14`/`P1-15` blobs, then `P2-06`/`P2-07`.
   `P2-03`/`P2-04` stay `not testable in this environment` until a Windows and a macOS runner execute the cells.
