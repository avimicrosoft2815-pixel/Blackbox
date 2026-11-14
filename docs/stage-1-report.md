# STAGE 1 REPORT — 2026-11-14 — commit: NONE — readiness and benchmark fulfilment

**Question this stage answers:** where do we honestly stand against 1.1's gates, and what will Stage 2 actually prove?
**Answer in one line:** nothing is fulfilled today; 21 of 56 gates are reachable in Stage 2; **two scope defects** in the Stage 2 ticket list and **three environment blockers** must be accepted or changed first.

Status vocabulary (brief §3.1): `fulfilled now` · `will be proven in Stage 2` · `later: phase Px` · `not testable in this environment`.

---

## 1. Gate table

### 1.1 §24 — acceptance checklist (v1.0.0), 25 gates

| # | Gate | Status | Landing |
|---|---|---|---|
| G1 | `docs/guarantees.md` exists, every claim links a test ID **and a published result** | will be proven in Stage 2 (file, P0-04); result-links later | P0-04 → P11-06 |
| G2 | Kill matrix S1–S18 × 3 modes × 3 segment sizes green on Linux | will be proven in Stage 2 for **S1–S5 + S17 only** | rest: S6–S13 P2, S14–S15 P1/P2, S16/S18 Stage 3 |
| G3 | 2 000 randomized `kill -9` trials, zero violations | will be proven in Stage 2 at **≥ 500 trials**; 2 000 is nightly | P2-09 / P0-03 |
| G4 | Recovery idempotent 10× ⇒ 1 capsule, identical sha256; concurrent ⇒ 1 winner | will be proven in Stage 2 (Linux); **Windows not testable here** | P2-12 |
| G5 | Torn-frame, bad-CRC, zeroed-head, corrupt-meta ⇒ exact documented codes | will be proven in Stage 2 | P1-06, P1-11, P2-06 |
| G6 | Unmodified `openai` **and** `anthropic` script records with zero code changes | later: P3 (+ needs the SDKs installed — see Decision D2) | P3-05…P3-09 |
| G7 | Streaming partial chunks + MID_STREAM classification with byte counts | later: P3 | P3-06, P2-07 |
| G8 | Fail-open suite: broken recorder ⇒ identical outputs/exceptions | later: P3 | P3-14 |
| G9 | SDK matrix declared; unknown version ⇒ safe mode, no silent mislabel | later: P3 | P3-05, P11-14 |
| G10 | Formats frozen + conformance vectors + from-scratch reader passes | will be proven in Stage 2 **in Python only**; Go reader is later | P0-05, P4-12; Go P11-15 |
| G11 | `INTEGRITY.sha256` verifies; 1 byte tampered ⇒ named failure | will be proven in Stage 2 for capsule v0 | P2-10 |
| G12 | Index rebuild reproduces canonical dump (I8) on 500 runs | later: P5 | P5-06 |
| G13 | Retention + GC: referenced blob never deleted; dry run explains | later: P8 / P5 | P8-03, P9 |
| G14 | 60-secret corpus absent from 11 sinks | later: P9 (redaction itself is P3) | P3-11, P9-05 |
| G15 | Capsule attack corpus (12) rejected with codes < 50 ms | later: P4 | P4-04 |
| G16 | 20-payload XSS corpus inert in viewer + Markdown export | later: P4 | P9-06 |
| G17 | Netguard: zero outbound attempts, incl. child processes | later: P7 | P7-03 |
| G18 | Data dir 0700 / files 0600 verified at startup on all three OSes | later: P9 — **macOS/Windows not testable here** | P9-08 |
| G19 | Clean machine → install → crash demo → `explain` < 5 min, 3 people | later: P11 (needs humans — I cannot self-certify this) | P11-05 |
| G20 | Zero third-party imports in core (test-enforced); offline wheel install | will be proven in Stage 2 (test); wheel/offline later | P0-13, P11-01 |
| G21 | `blackbox --help` ≤ 20 commands, every command `--json` + exit code | later: P4 | P4-05 |
| G22 | Viewer opens from `file://`, no server, no network | later: P4 | P4-10 |
| G23 | Published overhead table with hardware/versions/raw JSON | will be proven in Stage 2 **for the writer path only** | P1-17, P1-19 |
| G24 | Published prior-art log, dated, "not found ≠ does not exist" | later: P11 | P11-11 |
| G25 | Diagnosis benchmark (if Q7 = yes) | later: P11 (needs humans) | P11-12 |

### 1.2 §24a — 1.1 additions, 11 gates

| # | Gate | Status | Landing |
|---|---|---|---|
| A1 | `blackbox run` over node/python/curl/claude records the in-flight call after `kill -9` | will be proven in Stage 2 **for python only** (`sh -c 'kill -9 $$'`) | P2-11; node/claude = Stage 3 |
| A2 | Proxy byte-identity over 10 000 streamed responses | later: P3B (Stage 3) | P3B-08 |
| A3 | `x-blackbox-run` never reaches a mock provider from an un-proxied app | later: P3B | P3B-xx |
| A4 | Hook adapter install/remove leaves foreign settings byte-identical | later: P3B | P3B-07 |
| A5 | `docs/attach.md` verified-dated or "unverified" per row; README has §11.12 verbatim | **README part done in Stage 0**; the per-row doc is later | P11-14 (decision D5) |
| A6 | S14–S18 green; S17 proves no `SIGBUS` reaches the app | will be proven in Stage 2 for **S17 only** | S14–S15 P1-09/P1-13; S18 Stage 3 |
| A7 | `verify` detects a 1-byte change and names segment+seq; retention `GAP` verifies as a hole | later: P1-18 (chain), P4 | P1-18, P4 |
| A8 | Go reader from `docs/format.md` alone decodes conformance vectors | later: P11 | P11-15 |
| A9 | Trigger `stop_delay`/`cooldown`/`max_per_24h` tested with mocked clock; raising rule disables only itself | later: P8 | P8-10 |
| A10 | `events.jsonl` + `otlp/spans.json` regenerate byte-identically; drift ⇒ `BBX-E412` | later: P4 | P4-13…15 |
| A11 | Serverless wrapper leaves no open run after 1 000 simulated invocations | later: P8 | P8-11 |

### 1.3 §15.1 — budgets, 13 rows

| # | Budget | Status | Landing |
|---|---|---|---|
| B1 | Disabled overhead ≤ 1 µs / ≤ 3 µs (hard fail > 5 µs) | later: P3 (needs decorators) | P3-02 |
| B2 | Enabled content=off enqueue ≤ 8 / 40 µs | will be proven in Stage 2 **as writer append only**; full queue path P3 | P1-08, P3-13 |
| B3 | content=hash, 1 KiB full event ≤ 25 / 250 µs | later: P3 (needs redactor + hasher) | P3-11 |
| B4 | content=full, 64 KiB incl. blob ≤ 400 µs / 3 ms | later: P3 (blob writer is P1-14, **out of Stage 2 scope**) | P1-14 |
| B5 | Provider-latency impact ≤ 3 % Δp95 | later: P3 | P3-06 |
| B6 | Throughput ≥ 40 000 events/s (hard fail < 10 000) | will be proven in Stage 2 | P1-17, P1-19 |
| B7 | RSS above baseline ≤ 24 MiB | will be proven in Stage 2 | P1-08, P1-17 |
| B8 | `install()` ≤ 25 ms | later: P3 | P3-01 |
| B9 | Recovery ≤ 120 ms per 10 k frames | will be proven in Stage 2 | P1-11, P1-17 |
| B10 | Seal 50 k events ≤ 2 s | will be proven in Stage 2 (capsule v0) | P2-10, P1-17 |
| B11 | Index ingest ≥ 20 000 events/s | later: P5 | P5-10 |
| B12 | Timeline query ≤ 40 / 300 ms @1 M events | later: P4/P5 | P5-10 |
| B13 | Disk amplification ≤ 1.25× | will be proven in Stage 2 | P1-17 |

### 1.4 §15.1a — proxy/hook/roll budgets, 7 rows

| # | Budget | Status | Landing |
|---|---|---|---|
| C1 | Proxy added latency p50 ≤ 1 ms / p99 ≤ 5 ms | later: P3B | Stage 3 |
| C2 | Proxy first-byte overhead ≤ 0.5 ms | later: P3B | Stage 3 |
| C3 | Proxy ≥ 200 concurrent streams | later: P3B | Stage 3 |
| C4 | Proxy RSS ≤ 60 MB @50 req/s | later: P3B | Stage 3 |
| C5 | `ingest` cold start ≤ 40 ms p95 | later: P3B | Stage 3 |
| C6 | Segment roll latency ≤ 5 ms p99 | **not reachable as scoped** — roll (P1-09) is not in the brief's Stage 2 list | see §3.1 |
| C7 | `CHAIN` frame cost ≤ 2 µs/batch | later: P1-18 (chain is not in Stage 2 scope) | P1-18 |

**Tally:** fulfilled now **0 / 56**. Will be proven in Stage 2 **21** (G1, G2*, G3, G4, G5, G10*, G11, G20, G23*, A1*, A5*, A6*, B2*, B6, B7, B9, B10, B13 = 18 full-or-partial + 3 partials marked with *). Not testable in this environment **4** (G4-Windows, G18, G19, G25).

---

## 2. Environment probe (measured, not assumed)

Raw JSON: `bench/results/2026-11-14-env-probe.json` · probe source `bench/probes/env_probe.py` (throwaway, not product code).
Command: `python3 bench/probes/env_probe.py`.

| Property | Measured value |
|---|---|
| OS / kernel | Linux 6.1.158+ (x86_64), 2 vCPU, 4,034,452 KiB RAM, 4 KiB pages |
| CPU | Intel(R) Xeon(R) @ 2.60 GHz |
| Filesystem (repo, `$HOME`, `/tmp`) | `ext4`, not a network FS → the §8.11 NFS/SMB refusal does not fire |
| Free space | 20.7 GB of 21.8 GB |
| Python | **3.11.2 only** (3.10/3.12/3.13/3.14 absent) |
| `mmap` / `MAP_SHARED` | available; **cross-process visibility verified** (parent wrote a nonce, a child process read it back) |
| `os.posix_fallocate` | available; 16 MiB in **0.10 ms** |
| `os.fdatasync` / `os.fsync` / `os.replace` | available |
| `fdatasync` latency, 1 MiB file, 4 KiB write | p50 **175 µs**, p95 **402 µs**, p99 **421 µs**, n=200 |
| `os.replace` atomicity, 500 iterations with a concurrent reader | **0 missing, 0 torn**, 15,709 consistent reads |
| `faulthandler.register(SIGUSR1)` round-trip | works (stack dump written) → CH-09 stall dump is implementable here |
| Preallocation vs sparse (16 MiB) | fallocate `du` = 16,384 KiB; `truncate()` `du` = **0 KiB** → block allocation differs as expected. **`SIGBUS` itself was not delivered by this probe**; S17 proves it (control case must SIGBUS, preallocated case must not) |
| `blake2b-256` 1 KiB | p50 **1.99 µs** |
| `blake2b-256` 1 MiB | p50 **1.65 ms** (~630 MB/s) |
| Tooling | `git` 2.39.5, `gh` 2.23.0, `pip` present; **pytest / hypothesis / ruff / mypy absent** |

### 2.1 What these numbers already tell the design

1. **`fdatasync` at 175 µs p50 / 421 µs p99 here** means `mode=kernel` group commit must be time-batched (250 ms), never per event; `mode=paranoid` would cost ~5.7 k events/s ceiling on this disk. The plan's ordering (page cache first, fsync only on a schedule) is confirmed by measurement, not intuition.
2. **`blake2b` 1 MiB = 1.65 ms** rules out hashing large payloads inline on the writer thread; the 1 KiB inline / blob-external split (§8.8) is validated (1 KiB ≈ 2 µs, affordable).
3. **`posix_fallocate` 16 MiB = 0.10 ms here** contradicts the plan's worry that roll preallocation is 5–50 ms — on ext4 it is free. That weakens my own review finding **F8** on *this* platform; on APFS/NTFS it may still be slow, so F8 should be re-scoped to "keeper pre-creation is default; synchronous path measured on each OS", not "preallocation is slow".
4. **`truncate()` leaves a 0-block file** — block allocation differs, which is the *precondition* for the CH-10 argument (sparse mapping ⇒ page fault ⇒ `SIGBUS`). This probe did **not** deliver a `SIGBUS`; per the owner's correction, no report claims that until S17 shows `SIGBUS` in the sparse control case and none in the preallocated case.

---

## 3. Constraint feasibility, and three blockers

### 3.1 Two scope defects in the Stage 2 ticket list (plan-level, need your call)

| # | Defect | Evidence | Fix |
|---|---|---|---|
| **S1** | The brief's Stage 2 list excludes **P1-09 (roll/segments, 6 h)** and **P1-13 (`.seal` footer, 5 h)**, but includes **P2-06** (recovery must write a `.seal` with `reason = RECOVERED`), **P2-08 S17** ("`ENOSPC` injected **at preallocation**", which only happens at roll) and **P2-10** (capsule from sealed segments) | 1.1 §8.6 step 5, §8.7 rules, §16.2a S14/S15/S17, §18 P1-09/P1-13 | add P1-09 + P1-13 to Stage 2 (**+11 h plan**, 158 h total) or accept a Stage 2 that ships a single-segment writer and a capsule with no `.seal` sidecars (which makes S17 untestable and G2/G4 weaker) |
| **S2** | Stage 2 measures the writer **before retention exists** (P1-09's budget logic, P8-01 keeper) | §8.6 retention bullets, P1-09 | acceptable: measure at a fixed segment size with `tape_max_bytes` unenforced, and label B6/B7/B13 "single-segment writer, no retention path" |

### 3.2 Three environment blockers

| # | Blocker | Impact | Options |
|---|---|---|---|
| **E1** | **`pytest` and `hypothesis` are absent**, and rule 1 says "no package installs" | the plan's test tickets name pytest fixtures (`tests/property/…`, `stage_probe.py`, `tests/kill/…`) | (a) stdlib `unittest` + my own ~80-line property helper (no installs, everything reproducible); (b) allow `pip install pytest hypothesis` in this sandbox only (dev deps, still zero in `src/`) |
| **E2** | Only **Python 3.11** exists | P0-01's Done-when says "clean **3.10** venv"; G18 says three OSes; the CI matrix cannot run here | (a) prove on 3.11 and mark 3.10/3.12–3.14 `not testable in this environment`; (b) allow installing other interpreters |
| **E3** | No macOS, no Windows, no CI runner reachable from this session | G2/G4/G18/A-gates on other platforms | write the CI workflow (P0-02) and report every other-platform gate as `not testable in this environment` — never as passed |

### 3.3 Feasible here, confirmed
- **P0-13 stdlib-only** and **P0-14 no-egress**: both are AST walks over `src/` with no imports → implementable and meaningful here. The egress test will allow exactly the proxy's configured-upstream set (1.1 CH-15), which is a data-driven allowlist, not a regex.
- **Frame/head/meta/segment/capsule** work is pure `struct` + `mmap` + `hashlib` → fully testable here.
- **Kill tests** are `subprocess` + `os.kill(SIGKILL)` + `os._exit(137)` → fully testable here on Linux/ext4.

---

## 4. Stage 2 plan (32 tickets, revised)

Scope per brief §4 **plus** P1-09 and P1-13 (defect S1). Each row: the plan's Done-when (quoted), the test that proves it, the budget row it feeds, plan hours vs my estimate.

| Ticket | Plan Done-when (abridged) | Test file | Budget row | Plan h | Mine h |
|---|---|---|---|---|---|
| P0-01 | `pip install -e .` in clean venv, zero deps | `tests/test_packaging.py` | — | 2 | 2 |
| P0-02 | CI green in all cells | `.github/workflows/ci.yml` (written; **not runnable here**) | — | 3 | 2 |
| P0-03 | nightly produces artifacts | `.github/workflows/nightly.yml` (written; not runnable) | — | 2 | 2 |
| P0-04 | every guarantees row links a test ID | `tests/test_docs_links.py` | — | 2 | 3 |
| P0-05 | a reader from the doc alone passes conformance vectors | `docs/format.md` + `tests/conformance/` | — | 2 | 4 |
| P0-10 | no magic number appears twice | `tests/test_constants_single_source.py` | — | 1 | 1 |
| P0-11 | every used code declared | `tests/test_errors_registry.py` | — | 2 | 2 |
| P0-12 | config round-trip property | `tests/test_config_roundtrip.py` | — | 3 | 3 |
| P0-13 | fails on third-party import in core | `tests/test_stdlib_only.py` | — | 1 | 1.5 |
| P0-14 | fails on outbound code / unlisted host | `tests/test_no_egress.py` | — | 1 | 2 |
| P0-15 | one command per job | `Makefile` | — | 1 | 1 |
| P1-01 | 10 000 CRC32C vectors; throughput measured | `tests/test_crc32c.py` | B6 (partial) | 3 | 3 |
| P1-02 | byte-identical output; RFC 8949 subset | `tests/test_cbor_encode.py` | — | 8 | 10 |
| P1-03 | 100 k random buffers ⇒ only `CBORError` | `tests/test_cbor_fuzz.py` | — | 6 | 7 |
| P1-04 | ids sort chronologically; stable node ids | `tests/test_ids.py` | — | 3 | 3 |
| P1-05 | round-trip + 12-byte offsets asserted | `tests/test_frame_layout.py` | B13 | 4 | 5 |
| P1-06 | corrupt meta ⇒ E222; version 99 ⇒ E221 | `tests/test_meta.py` | G5 | 3 | 3 |
| P1-07 | torn slot ⇒ older slot chosen (S4) | `tests/test_head.py` | G5 | 5 | 6 |
| P1-08 | 40 k events/s; no syscall in `mode=process` | `tests/test_writer.py` | B6, B7 | 12 | 16 |
| P1-09 | roll on full; no copy; active+unsealed never deleted | `tests/test_roll.py` | C6 | 6 | 8 |
| P1-11 | 2 000 randomized kills recover the exact prefix | `tests/test_scanner.py` + P2-08 | B9, G3 | 10 | 12 |
| P1-12 | frames in order; explicit gap marker | `tests/test_reader.py` | — | 3 | 3 |
| P1-13 | `.seal` idempotent; verified | `tests/test_seal_footer.py` | G5, A6 | 5 | 5 |
| P1-17 | benchmark JSON + hardware metadata | `bench/bench_frame.py` | B6, B7, B9, B10, B13, C6 | 4 | 5 |
| P1-19 | A/B mmap vs `write()`; ADR-001 reopened if within 20 % | `bench/bench_writer_ab.py` | B6 | 3 | 4 |
| P1-20 | S17: ENOSPC never delivers SIGBUS | `tests/test_prealloc_sigbus.py` | A6, G2 | 4 | 6 |
| P2-01 | stale/reboot/PID-reuse/live each tested | `tests/test_lease.py` | — | 8 | 8 |
| P2-02 | Linux+macOS pass, no psutil | `tests/test_platform_posix.py` (**macOS not testable here**) | — | 4 | 5 |
| P2-06 | kill at each of 8 steps × 3 modes ⇒ identical capsule | `tests/test_recovery_sm.py` | G4 | 14 | 16 |
| P2-08 | 18 stages × 3 modes × 3 sizes (Stage 2: S1–S5, S17) | `tests/kill/stage_probe.py` | G2, G3 | 10 | 12 |
| P2-10 | capsule opens, root verifies, re-seal identical | `tests/test_capsule_v0.py` | G11, B10 | 10 | 12 |
| P2-11 | `run -- sh -c 'kill -9 $$'` yields a recovered run | `tests/test_launcher.py` | A1 | 10 | 10 |
| P2-12 | both pass on Linux+Windows (**Windows not testable here**) | `tests/test_recovery_idempotence.py` | G4 | 3 | 3 |
| **Total** | | | | **158** | **188** |

My estimate is **+19 %** over the plan; the overrun sits almost entirely in the writer/scanner/roll blocks (P1-08, P1-09, P1-11, P2-06, P2-08) where the 1.1 design has the most byte-level surface, and in replacing pytest with a stdlib harness (~4 h spread across rows).

---

## 5. Benchmark protocol (used for every Stage 2 number)

1. **Repetitions:** 200 warm-up events, then N ≥ 100 000 events for throughput; latency rows use 30 repetitions of 5 000 events each and report the **median of the per-repetition p50/p99**, not the p99 of everything pooled.
2. **Pairing:** recorder-on vs recorder-off measured in the same process, interleaved in blocks, to cancel machine drift (1.1 §15.3).
3. **CLI commands** are recorded in the results JSON: `python3 bench/bench_frame.py --events 100000 --mode process --content off`.
4. **Metadata in every JSON:** CPU model, vCPU count, RAM, kernel, filesystem, Python build, segment size, durability mode, content mode, payload size, `git rev-parse HEAD`, and whether the machine was otherwise idle (**this container is shared: reported as "not isolated", and p99 is therefore an upper bound**).
5. **No pinned CPUs available** (2 vCPU, `taskset` would starve the probe) → recorded explicitly as a limitation rather than silently ignored.
6. **Hard-fail handling:** if any §15.1 hard-fail threshold is hit, Stage 2 stops and reports profile + options, per brief §4.

---

## 6. Decisions needed from you

| # | Decision | Plan ref | Plan default | My recommendation |
|---|---|---|---|---|
| D1 | Test harness: stdlib `unittest` + own property helper, **or** allow `pip install pytest hypothesis` (dev-only) | brief rule 1 | stdlib only | **install pytest+hypothesis as dev deps** if allowed — the plan's test tickets are written for pytest, and re-implementing fixtures costs ~15 h across P0–P9. If not allowed, I proceed with `unittest` and zero installs. |
| D2 | Python versions: prove on 3.11 only, or install 3.10/3.12+ | P0-01, §16.7 | 3.10–3.14 | prove on 3.11, label the rest **not testable here** |
| D3 | Stage 2 scope: add **P1-09 + P1-13** (+11 h) or ship a single-segment, seal-less Stage 2 | brief §4 vs §18 | — | **add them**; S17/S14/S15 are otherwise untestable and P2-06/P2-10 would be built against a missing layer |
| D4 | Resolve the four review blockers (F1–F4) **before** Stage 2? | `docs/REVIEW_v1.1_FINDINGS.md` §3 | none applied | Stage 2 touches none of F1/F2/F3; **F4 (`.seal` crc semantics) does touch P1-13** → apply F4 first, defer F1/F2/F3 to Stage 3/5 |
| D5 | `docs/attach.md` (A5) — build it in Stage 2 or leave to P11-14? | §24a | P11-14 | leave to P11-14; Stage 2 has no attach surface |
| D6 | Commit + push policy for stage work | brief §1 rule 7 | one commit per ticket, no push | commit locally per ticket; **push only when you say so** |
