# Blinded diagnosis benchmark — preparation for the human-only item

The plan asks whether a person given only a capsule (no logs, no source of the recorded errors) can say what went
wrong. Nobody outside this session has run it; `docs/STATUS.md` labels it `requires humans`.

## The capsule set

Five capsules are produced by the repository's own tests and demo, so the set is reproducible and none of them is
hand-written:

| Case | How it is produced | What a correct reader should conclude |
|---|---|---|
| A | `blackbox run -- sh -c 'kill -9 $$'` | the run was interrupted mid-flight; the last committed node is named |
| B | `tests/test_launcher.py` deadlock case (two locks, `stall_ms`) | a hang, not a crash; the stack of both threads is the evidence |
| C | `tests/test_recovery.py` mid-stream kill | a `MODEL_CHUNK` stream was cut; `chunks_received` and `partial_stream_bytes` are the count |
| D | `tests/test_enospc.py` (small tmpfs, S17) | the recorder degraded; the user's program kept running and nothing was lost silently |
| E | `tests/test_retention.py` after a prune | the missing range is declared in a `GAP`, not lost |

## Procedure

1. Hand the reviewer case A–E capsules, the `blackbox verify` command, and nothing else. No file in this
   repository, no STATUS, no source.
2. Ask, per case, in writing: *what happened, which step, and what would you do next?*
3. Score each answer against the third column: **specific** (names the interrupted step or the declared hole),
   **generic** ("it crashed"), or **wrong** (claims durability the capsule does not show).
4. A case passes when four of five reviewers are **specific** and none is **wrong**.

## Why it is not run here

It needs four people who have not read this repository. The capsules are cheap to regenerate; the reviewers are
not, and pretending the session can supply them is the kind of claim this project's rule 24 exists to prevent.
