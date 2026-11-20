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

The four modes are in the code (`constants.MODES`, `constants.DURABILITY_CLASS`) and each one's sync policy is asserted
where it lives: the writer's roll and seal syncs (`tests/test_blob_durability.py`), the head store's sync choice
(`src/blackbox/tape/head.py`), and the serverless wrapper's per-invocation close.

| Mode | Sync policy | Claim it may make | Where it is asserted |
|---|---|---|---|
| `volatile` | none | **F1-weak** — named that way in `DURABILITY_CLASS`, because the OS may not keep the mapping | `constants.DURABILITY_CLASS`; no test claims more |
| **`process` (default)** | fsync at roll, seal and journal | **F1** | `tests/test_crash_points.py` (8), `tests/test_adoption.py`, `tests/test_recovery_identity.py` (3) |
| `kernel` | group commit every 250 ms or 1 MiB | **F1 + F2** | `tests/test_blob_durability.py`, `tests/test_recovery_identity.py` |
| `paranoid` | `fdatasync` after every P0 event | **F1 + F2** per event, at the cost measured below | `tests/test_blob_durability.py::test_paranoid_mode_syncs_before_the_frame_is_appended` |

Measured here: `fdatasync` on a 1 MiB file, 4 KiB write, n=200 → p50 **175 µs**, p99 **421 µs**
(`bench/results/2026-11-14-env-probe.json`). That is why `paranoid` is documented as a low-QPS mode.

## 3. Commit point

> An event is *committed* when a head record with `head_seq ≥ event.seq` and a valid CRC is visible in the
> `head` mapping and the event lies within `head_off` of the active segment, or inside a segment with a
> valid `.seal`. Committed ⇒ visible to every later reader, including one in another process.
> Committed does **not** imply survival of a machine-level failure unless the durability mode says so.

## 4. Claims → tests

Every row names a test that exists in this tree. `tests/test_guarantees_links.py` fails if a row here names a file or a
node that cannot be collected, which is what this table needed: until 2026-11-20 it pointed at
`tests/kill/test_kill_stages.py::test_s1`, `tests/test_head.py`, `tests/test_prealloc_sigbus.py` and
`tests/test_recovery_idempotence.py` — four paths that have never existed in this repository (defect 112).

| Claim | Test that holds it | Result here |
|---|---|---|
| a kill between payload copy and trailer leaves the frame invisible and the head unchanged | `tests/test_crash_points.py::test_torn_frame_is_never_committed` | pass |
| a torn head slot falls back to the previous commit, losing nothing | `tests/test_crash_points.py::test_s4_torn_head_slot_falls_back_to_previous_commit` | pass |
| after the head publish the commit survives | `tests/test_crash_points.py::test_s5_after_head_publish_the_commit_survives` | pass |
| a segment end before the next segment starts is recoverable | `tests/test_crash_points.py::test_s14_after_seg_end_before_next_segment` | pass |
| a durable next segment whose head still names the old one is recovered | `tests/test_crash_points.py::test_s15_next_segment_durable_but_head_still_names_the_old_one` | pass |
| randomized parent-`SIGKILL` trials recover exactly the committed prefix | `tests/kill/kill_matrix.py` (500 trials in-tree; 2 000 on the nightly) | 500/500, 0 violations (recorded in `docs/STATUS.md`); the 2 000-trial run was a `workflow_dispatch`, not a schedule |
| recovery is idempotent: ten recoveries of one tape ⇒ one capsule, one sha256 | `tests/test_recovery_identity.py` | pass (3 tests) |
| a second recoverer loses with a code rather than a traceback | `tests/test_recovery_identity.py` | pass |
| `.seal` is written once and compaction never rewrites it | `tests/test_capsule.py` | pass |
| a body the provider cut short is recorded as cut short, with byte counts no larger than the provider pushed | `tests/test_stream_termination.py`, `tests/test_day14_kill.py` | pass (POSIX) |
| the recorder deliberately broken leaves the app's output and exceptions identical | `tests/test_failopen.py` (8) | pass |
| a torn frame, a bad CRC, a zeroed head and corrupt meta produce the documented codes | `tests/test_fault_codes.py` (7) | pass |
| a capsule's derived parts recompute to the same bytes | `tests/test_capsule.py`, `blackbox verify`'s `derived_parts` check | pass |
| a hostile capsule is refused with its own code in under 50 ms | `tests/test_capsule_attacks.py` (25) | pass; worst rejection 15.7 ms |
| recorded text cannot become markup in any sink | `tests/test_xss_corpus.py` (5) | pass |
| the sixty-secret corpus is absent from all eleven sinks | `tests/test_redaction_every_sink.py` | pass, with a redaction-off control |
| replay and every read path make no outbound connection | `tests/test_no_egress.py` (5), `tests/test_netguard_reads.py` (9) | pass |

## 5. What is never claimed

- zero loss, in any mode;
- machine-failure safety without naming the device-cache behaviour and the measurement behind it;
- "kill -9 tested ⇒ crash safe" (F1 ≠ F2 ≠ F3);
- that all in-flight data is preserved — only what was received, with a byte count.
