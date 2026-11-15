# Guarantees (BBX-ARCH-1.1 §10) — every claim is tied to a test ID

**Status:** the tests below are *planned* until `docs/progress.md` records a passing result for them.
Nothing in this file may be quoted in marketing copy; §7.4 of the plan governs wording.
Forbidden words in this repository: zero loss, crash-safe, power-loss safe, secure, forensic-grade,
deterministic, first ever, unique, never done before.

## 1. Failure classes are never conflated

| Class | What survives | What is at risk |
|---|---|---|
| **F1** process death (`kill -9`, `SIGSEGV`, OOM kill) | everything in the OS page cache: each batch `pwrite` completes before the head store that commits it (ADR-005) | only bytes still in the writer's batch buffer, i.e. frames the head has not committed |
| **F2** kernel crash / VM reset | only data that reached the device (`fdatasync`) | everything in page cache |
| **F3** power loss | `fdatasync`ed data **if** the device honours flush and has no volatile write cache | page cache + device cache without barriers |
| **F4** storage corruption | every frame whose CRC validates; the rest is reported as a declared hole | frames inside the damaged region |

## 2. Durability modes

| Mode | Sync policy | Claim | Status |
|---|---|---|---|
| `volatile` | none | not recommended; F1 only if the OS keeps the mapping | planned (P1-08) |
| **`process` (default)** | fsync only at roll / seal / journal | **F1** | in progress (P1-08) |
| `kernel` | group commit every 250 ms or 1 MiB | F1 + F2 | planned (P1-08, P2-06) |
| `paranoid` | `fdatasync` after every P0 event | F1 + F2 per event | planned (P1-08) |

Measured here: `fdatasync` on a 1 MiB file, 4 KiB write, n=200 → p50 **175 µs**, p99 **421 µs**
(`bench/results/2026-11-14-env-probe.json`). That is why `paranoid` is documented as a low-QPS mode.

## 3. Commit point

> An event is *committed* when a head record with `head_seq ≥ event.seq` and a valid CRC is visible in the
> `head` mapping and the event lies within `head_off` of the active segment, or inside a segment with a
> valid `.seal`. Committed ⇒ visible to every later reader, including one in another process.
> Committed does **not** imply survival of a machine-level failure unless the durability mode says so.

## 4. Claims → tests

| Claim | Test ID | Status |
|---|---|---|
| kill after payload copy, before trailer ⇒ frame invisible, head unchanged | S1 · `tests/kill/test_kill_stages.py::test_s1` | planned |
| kill after trailer, before header ⇒ same as S1 | S2 · `…::test_s2` | planned |
| kill after header, before head publish ⇒ frame not visible, prefix intact | S3 · `…::test_s3` | planned |
| torn head slot ⇒ older slot wins, nothing lost | S4 · `tests/test_head.py::test_torn_slot_uses_previous` | planned |
| kill after head publish ⇒ frame is recovered exactly | S5 · `…::test_s5` | planned |
| 2 000 randomized kills recover exactly the committed prefix | G3 · `tests/kill/randomized_kills.py` | planned (200 in-tree, 2 000 nightly) |
| `ENOSPC` at preallocation never delivers `SIGBUS` to the app; sparse control case does | S17 · `tests/test_prealloc_sigbus.py` | planned |
| `.seal` is written once; compaction never rewrites it | A6 · `tests/test_seal_footer.py` | planned |
| recovery is idempotent (10× ⇒ identical capsule) | G4 · `tests/test_recovery_idempotence.py` | planned (P2-12) |

## 5. What is never claimed

- zero loss, in any mode;
- machine-failure safety without naming the device-cache behaviour and the measurement behind it;
- "kill -9 tested ⇒ crash safe" (F1 ≠ F2 ≠ F3);
- that all in-flight data is preserved — only what was received, with a byte count.
