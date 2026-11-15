# BlackBox build status — 2026-11-14 — trunk `af908bc` — gates: **G-Promise pass** (self-signed; see §Labelled), G-Capture, G-Attach, G-Capsule, G-Sec, G-Release not yet opened

**Branch** `arena/a781c68f-blackbox` · **base** `31241d6` · CI for `372006f`: run `43679755477`,
ten jobs, ten `success` (P3-01). Earlier green runs (`43679417991` at `a6f4faa`, `43678447134` at `4619b90`)
belong to earlier commits and are not evidence for this one.
**product: pre-alpha — storage, recovery, launcher and capsule layer only; no SDK, proxy or viewer yet.**
Suite: **Total: 338 tests** — 337 pass here and 1 is skipped on POSIX by name
(`test_an_open_view_blocks_the_unlink_only_on_windows`: POSIX permits unlinking a file another fd still holds, so
the open-view case exists only on Windows). ruff 0.17.5 and mypy 2.4.1 (`strict = true`, nothing disabled):
0 findings.

## Fulfilled (test exists, ran, passed / number measured)

| Gate or budget row | Plan ref | Evidence (test path, results JSON, or CI run URL) |
|---|---|---|
| Canonical CBOR; decoder hardening | §8.4, P1-03 | `tests/test_codec.py`, `bench/fuzz_cbor.py` → `bench/results/2026-11-14-fuzz-cbor.json`: 100 000 buffers, 0 non-`CBORError` |
| One checksum (CRC-32/ISO-HDLC), conformance vectors | §8.3, DV-005 | `tests/test_conformance.py`, `tests/conformance/vectors/crc32.json` check value `0xCBF43926` |
| Frame layout, CRC scope, head two-slot publish + rebuild | §8.3, §8.5 | `tests/test_frame.py`, `tests/test_head_creation.py` |
| Segments, roll, adoption (S15) | §8.7, P1-12 | `tests/test_tape.py`, `tests/test_adoption.py`: adoption idempotent in the writer and in `rec/discover.py` |
| Crash stages S1–S5, S14, S15 green on the new store | §16.2 | `tests/test_crash_points.py`, `tests/test_scan_states.py` (seeds 388/209/396) |
| S17 ENOSPC, first segment included | §16.2, P1-20 | `tests/test_enospc.py`, `tests/test_prealloc.py`: exit 0, identical stdout, `DEGRADED`; no first segment ⇒ sink, never an exception |
| Randomized parent-`SIGKILL`, P2-09 | §14, C19 | `tests/kill/kill_matrix.py --trials 500` in a fresh clone of `af908bc`: **500/500 effective trials, 0 violations, 0 `not_started`**, 10 cells × 50 (includes a 20 ev/s window cell and a `kernel` cell) |
| Append-path syscalls, C11 | §8.6 | `tests/test_no_syscall.py`: 100 000 appends ⇒ exactly two `pwrite`s per batch (data + 64 B head slot) and nothing else |
| Group-commit window ≤ 5 ms; keeper | §8.6, P1-08 | `tests/test_keeper.py`: window measured at 1 ev/s, 1 000 ev/s and unbounded |
| Launcher `blackbox run -- cmd`, stall dump (P2-11, P2-13) | §10.3, F13 | `tests/test_launcher.py`; fresh clone: `run r_40ead954098fb5df · exit 137 · signal 9 · recovered c_40b2ebb29e649534.bbx (SEAL_INTERRUPTED, root 14f1737e50b6)`; a real deadlock yields `stall.json` + both thread stacks in `stall_tb.txt` |
| Capsule v0 root verifies; P2-12 idempotence | §12, P2-10 | `tests/test_capsule.py`, `tests/test_recovery.py`: 10 recoveries ⇒ 1 capsule hash, 1 `.bbx`; second recoverer `BBX-E210`; dead holder's lock taken over |
| Recovery state machine (P2-06) and node classification (P2-07) | §14.4 | `tests/test_recovery.py`: 8 steps × 3 kill modes all resume to the identical capsule root; the five interrupted-node strings exactly |
| Retention + declared holes (P1-09b) | §9.5, D-016/17/18 | `tests/test_retention.py`, `tests/property/test_retention_random.py`: 8 seeds × 60 steps per run, 320 recorded runs in `bench/results/p1-09b-retention.json` (`totals`: 19 200 steps, 5 400 kills, 0 violations of I1/I9); the file is written only with `BLACKBOX_RECORD_RESULTS=1` |
| Seal footer, verifier, compaction into `arch/` (P1-13) | §8.7, F4 | `tests/test_tape.py`, `tests/test_compact.py`: re-seal byte-identical; archived segment reads back frame-identical; second compaction a no-op |
| Blob store (P1-14), compression on the keeper (P1-15) | §9.9, §7.1 | `tests/test_blobs.py`: hash → tmp → write → replace, every read re-verifies CRC and hash, `BBX-E162` on a renamed blob, `ChunkCoalescer` 2 000 chunks → 3 blobs |
| Blob durability per mode, **D-019**: the calling thread never syncs a blob | §7.1, C32 | `tests/test_blob_durability.py`, `tests/test_no_syscall.py`: `process` records no sync at all; `kernel` syncs blob + directory in the keeper tick **before** the tape sync; `paranoid` before the frame is appended; 64 KiB event = open/write/close/replace + 2 `pwrite`s + **0** syncs |
| Platform shims, P2-03/P2-04 | §7 | `tests/test_platform_shims.py`, `tests/test_platform_dispatch.py`: `is_current` computed (no `tested_here` constant); no shim imported by name outside `platform/`; Windows `OpenProcess`+`GetProcessTimes`, macOS `sysctl` + `F_FULLFSYNC` exercised on the real cells — run `43678447134` |
| no mapping anywhere (`import mmap` gone from `src/`) | ADR-005, DV-010 | `tests/test_no_mapping.py`: no module in `src/` imports `mmap` and no `mmap.*` attribute is referenced; the head is published with one `pwrite` per batch |
| Budget: 1 KiB `content=hash` p50/p99, throughput | §15.1 | p50 11.057 · p99 51.662 µs · 73 338 ev/s — `bench/results/2026-11-14-writer-throughput.json` |
| Budget: recovery ≤ 120 ms / 10 k frames | §15.1 | 106.1 ms/10 k, n 30 — `bench/results/2026-11-15-recovery.json` |
| Budget: disk amplification ≤ 1.25× (2× hard fail) | §15.1 | 16 KiB group commit 1.231× (3 150 352 / 2 560 000) — `bench/results/2026-11-15-recovery.json` |
| Budget: RSS ≤ 24 MiB above baseline | §15.1 | 1.56 MiB at 28.13 % of a segment — `bench/results/2026-11-15-recovery.json` |
| Budget: 64 KiB `content=full` incl. blob write (C32) | §15.1 | p50 226.864 · p99 461.281 µs · 4 356.4 ev/s — `bench/results/2026-11-15-payload-blob.json` |
| **P3-01** SDK lifecycle: `install`/`uninstall`/`shutdown` | §10.2, P3-01 | `src/blackbox/api.py`, `tests/test_api_lifecycle.py` (6): double install ⇒ one writer UUID and one `RUN_START`; 16 threads install at once ⇒ one writer and `refs` 16; `uninstall()` during an in-flight append leaves the tape open and the frame lands; `BLACKBOX_DISABLED=1` creates no tape and `emit()` returns 0; an unwritable data dir leaves the app running with the reason recorded; `shutdown()` is idempotent and install works after it |
| Lint, types, forbidden words, egress, stdlib-only | C6, C12, P0-14 | `make fmt` 0 findings, `mypy` strict 0 findings, `tests/test_forbidden_words.py`, `tests/test_no_egress.py`, `tests/test_stdlib_only.py` |
| CI on three OSes × three Pythons + docs | C7 | run `43678447134` at `4619b90` — ten jobs, ten `success`; `docs/compatibility.md` holds the per-cell verdicts |

## Labelled

| Item | Label (requires humans / requires time / not testable here / blocked) | Reason / fix attempts |
|---|---|---|
| **Independent gate review** | requires humans | Every row in `docs/reviews.md` and every gate file is signed `self`: same model, working from a fresh clone (`/tmp/gp` at `af908bc`), from the plan text and from the pushed tree. The plan wants a reviewer from a different model family; none was available in this session. Not counted as independent anywhere. |
| P2-09 week of nightlies | requires time | GitHub `schedule` runs only on the default branch; the clock starts at merge. |
| 3-person stranger test | requires humans | Prepared to one command: `docs/onboarding.md` holds the setup line and three tasks; nobody outside this session has run it. |
| Blinded diagnosis benchmark | requires humans | Prepared: `docs/benchmarks/diagnosis.md` holds the checklist and the capsule set is reproducible with `blackbox recover`. Needs a second and third person. |
| S17 sparse-control run under `sudo` | requires humans | `tools/s17_control.sh` builds the 2 MiB tmpfs control and prints the signal; no superuser here. |
| Windows / macOS benchmark rows | not measured | The **test** gates are green there (run `43678447134`); no §15.1 number in this repository comes from a runner, and none will. |
| Real-provider capture (day-14 rule) | not testable here | No provider keys in the environment; the gate is the mock provider, as the plan allows. |
| Licence decision, release tag, merge to `main` | requires humans | `release.yml` is prepared and **not** triggered; no tag, no `LICENSE`, no licence field. |
| P10, P12 | blocked (not started by design) | P10 starts only after every v1.0 gate is green; P12 never without instruction. |

## Measurements

Hardware for every ext4 row: Intel Xeon @ 2.60 GHz, 2 cores, 4 034 452 KiB RAM · Linux 6.1.158+ x86_64 (glibc
2.36) · CPython 3.11.2 · ext4 (tmpfs is a labelled second column and is never gate evidence, D-010).
One line per §15.1 row: fs · N · warm-up · p50 / p95 / p99 · raw JSON.

- 1 KiB `content=hash`, group commit 16 KiB, CHAIN on · ext4 · N 200 000 · warm-up 20 000 · p50 11.057 · p95 20.53 · p99 51.662 µs · 73 338 ev/s · `bench/results/2026-11-14-writer-throughput.json` (`results.ext4.rows[gate=true]`)
- same row, keeper thread publishing · ext4 · N 200 000 · warm-up 20 000 · p50 11.044 · p95 20.6 · p99 54.527 µs · 73 471 ev/s · `bench/results/2026-11-14-writer-throughput.json`
- kernel mode (`sync_ms=250`, 1 MiB) · ext4 · N 200 000 · p50 11.278 · p99 89.903 µs · 60 368 ev/s · sync stall max 2 866 µs over 28 syncs · `bench/results/2026-11-14-writer-throughput.json` (reported, not a gate row)
- kernel mode, sync-dense (64 KiB) · ext4 · p50 11.387 · p99 81.232 µs · stall max 11 875 µs over 453 syncs · `bench/results/2026-11-14-writer-throughput.json` (reported)
- group-commit window · ext4 · 1 ev/s 4.603 ms · 1 000 ev/s 4.999 ms · unbounded 3.572 ms (120.99 ev/commit) · `bench/results/2026-11-14-writer-throughput.json`
- recovery, 22 312-frame tape from 10 000 events · ext4 · n 30 rounds · min 231.59 · p50 236.83 · p95 248.36 · p99 250.78 ms → **106.1 ms/10 k** (p99 = max because n < 100) · `bench/results/2026-11-15-recovery.json` (`results.ext4.recovery`)
- disk amplification, 16 KiB group commit · ext4 · N 10 000 events, 2 560 000 payload bytes · 1.231× (3 150 352 / 2 560 000) · `bench/results/2026-11-15-recovery.json` (`results.ext4.amplification_by_batch[batch_bytes=16384]`)
- disk amplification, publish-per-event · ext4 · 1.843× (4 718 976 / 2 560 000) · `bench/results/2026-11-15-recovery.json` (`results.ext4.amplification`) — reported, above the 1.25× row, below the 2× hard fail
- disk amplification, 4 KiB batch · ext4 · 1.263× (3 234 416 / 2 560 000) · `bench/results/2026-11-15-recovery.json` (`amplification_by_batch[batch_bytes=4096]`) — reported
- disk amplification, 64 KiB batch · ext4 · 1.222× (3 127 816 / 2 560 000) · `bench/results/2026-11-15-recovery.json` (`amplification_by_batch[batch_bytes=65536]`) — reported
- RSS above baseline · ext4 · 10 000 events, `head_off` 4 723 072 (28.13 %) · anon +1.27 MiB, file +0.29 MiB → 1.56 MiB · `bench/results/2026-11-15-recovery.json` (`rss_write_only_process.ext4`)
- 64 KiB `content=full`, keeper on · ext4 · N 20 000 · warm-up 2 000 · p50 226.864 · p95 325.83 · p99 461.281 µs · 4 356.4 ev/s · 0 tape syncs, 0 blob syncs on the calling thread · `bench/results/2026-11-15-payload-blob.json` (`results.ext4.rows[gate=true]`)
- same row, keeper off · ext4 · p50 167.899 · p99 304.614 µs · `bench/results/2026-11-15-payload-blob.json` (reported)
- same row, `kernel` mode · ext4 · p50 194.982 · p99 477.095 µs · 1 024 blob syncs · `bench/results/2026-11-15-payload-blob.json` (reported)
- randomized parent-`SIGKILL` · ext4 · 500 effective trials, 10 cells × 50 · 0 violations, 0 `not_started` · fresh clone of `af908bc`, `tests/kill/kill_matrix.py`
- tmpfs labelled column for every row above · tmpfs · recovery 79.9 ms/10 k, amplification identical · `bench/results/2026-11-15-recovery.json` (`results.tmpfs`) — never gate evidence

## Deviations applied (DEVIATIONS.md) and held findings (F#)

Twelve deviations, all dated and argued in `docs/DEVIATIONS.md`: DV-005 (one checksum, CRC-32), DV-006/007 (S17
degrades instead of raising, including the first segment), DV-008 (P2-03/P2-04 written with an honest
`capabilities()`), DV-009/010 (ADR-005 store change; the head is no longer mapped), DV-011 (every OS call through
`platform.current()`), DV-012 (`O_BINARY` on every descriptor). Findings F1–F13 are decided, not pending: F4 became
DV-001 (seal hashes over the uncompressed body), F13 lives in P2-11 (launcher stall dump), and the remainder are
recorded in `docs/errors.md` with the defect that motivated each. No finding is "awaiting a word".

## Decisions taken with plan defaults (DECISIONS.md)

Nineteen decisions, D-001 … D-019. The ones taken since the last checkpoint, each with the plan default and the
reason written beside it: **D-016/D-017/D-018** (retention prunes whole sealed segments, a tombstone pins and merges
`GAP`s, deletion is `GAP`-then-unlink), **D-019** (the calling thread never syncs a blob; the keeper syncs blob and
directory before the tape sync that covers the frame — the invariant is a test). The two knobs of `P1-02b` were the
last thing switched off in `mypy`; nothing is switched off now. Format-freeze decisions carry their `DEVIATIONS.md`
row in the same commit.

## What is next

1. **G-Promise is signed** (`docs/gates/G-Promise.md`, verdict pass, self-signed — see §Labelled) and `STATE.json`
   carries `gates["G-Promise"] = "pass"`.
2. **L2 ‖ L3 fan out now**, as the plan orders: L2 = P3-01 → P3-14 (SDK install/uninstall, decorators, context
   vars, seams A/B/C, preflight, streaming tee, `ModelCall`, redaction, buffer, fail-open, mock provider); L3 =
   P5 attach lane (proxy, launcher integration, links, hooks, demo). Both start from `af908bc`.
3. **G-Capture** with the day-14 rule: an unmodified `openai`/`anthropic` script through seams A→B→C against the
   mock provider must keep its in-flight call across `kill -9` with correct `chunks_received`/`bytes_received`.
   Real-provider runs are `not testable here` (no keys) and the mock result is the gate, as the plan allows.
4. **P3-01 is committed** (`api.py` lifecycle + `tests/test_api_lifecycle.py`). **Next ticket: P3-02** — the
   `workflow/agent/tool/retrieval/step` decorators through `functools.wraps`, with a disabled path that costs ≤ 1 µs;
   L3 work takes the rest of the turn.
5. The 2 000-trial kill run starts in the background during L5–L8; it is not needed for L2/L3.
