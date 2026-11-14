# Progress — ticket by ticket

One row per ticket in `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` §13. Status vocabulary:

| status | meaning |
|---|---|
| `todo` | not started |
| `wip` | partly built in the working tree, not yet committed |
| `done` | implemented **and** covered by a test that runs here |
| `n/a` | not testable in this environment (recorded, never counted as passed) |

> Rule 9: any deviation from the plan is recorded in the plan in the same commit as the code.

## Stage 0 — documentation baseline (`02fd609`)

| ticket | status | evidence |
|---|---|---|
| P0-00 (`docs/format.md` v2 freeze) | done | `docs/format.md` |
| P0-02 (`docs/guarantees.md` F1–F4 + claim→test map) | done | `docs/guarantees.md` |
| P0-04 (`docs/compatibility.md`) | done | `docs/compatibility.md` |
| P0-12 (docs link check) | done | `tests/test_docs_links.py` |

## Stage 2a — P0 remainder (this report)

| ticket | status | evidence |
|---|---|---|
| P0-01 (packaging, `>=3.11`, setuptools) | done | `pyproject.toml`, `tests/test_packaging.py` |
| P0-03 (`constants.py` single source of truth) | done | `src/blackbox/constants.py` + magic-width self-check |
| P0-05 (error-code registry) | done | `src/blackbox/errors.py`, `tests/test_errors.py` |
| P0-06 (config loader, env + file precedence) | done | `src/blackbox/config.py`, `tests/test_config.py` |
| P0-07 (CRC32C) | done | `tests/test_codec.py` (check value `0xE3069283`) |
| P0-08 (no-egress guard) | done | `tests/test_no_egress.py` |
| P0-09 (canonical CBOR) | done | `tests/test_codec.py` |
| P0-10 (ids / hashing) | done | `tests/test_codec.py` |
| P0-11 (CI matrix) | wip | `.github/workflows/ci.yml` — cells for 3.12/3.13/macOS/Windows are **not run in this environment** |
| P0-13 (stdlib-only guard) | done | `tests/test_stdlib_only.py` |

## Stage 2a — P1 core

| ticket | status | evidence |
|---|---|---|
| P1-01 (frame codec + CRC + trailer + backward walk) | done | `tape/frame.py`, `tests/test_frame.py` |
| P1-02 (preallocation; sparse refused) | done | `tape/prealloc.py`, `tests/test_prealloc.py` |
| P1-06 (immutable `meta` page) | done | `tape/meta.py`, `tests/test_tape.py::test_meta_*` |
| P1-07 (two-slot commit head) | done | `tape/head.py`, `tests/test_tape.py::test_head_*` |
| P1-08 (append + publish, named crash points) | done | `tape/writer.py`, `tests/test_crash_points.py` |
| P1-09 (segment create/roll/list) | done | `tape/segments.py`, `tests/test_tape.py::test_roll_*` |
| P1-11 (backward scan + salvage) | done | `tape/scanner.py`, `tests/test_crash_points.py` |
| P1-12 (forward reader) | done | `tape/reader.py` |
| P1-13 (`.seal` footer, verify) | done | `tape/seal.py`, `tests/test_tape.py::test_seal_*` |
| P1-18 (batch + segment chain) | done | `tape/chain.py`, `tests/test_tape.py::test_roll_*` |
| P1-20 (preallocation failure degrades, never raises into the app) | done | `tests/test_prealloc.py::test_enospc_*` |
| P1-03, P1-04, P1-05, P1-10, P1-14 … P1-17, P1-19, P1-21 … P1-27 | todo | see plan §13 |
| P2-01 (writer/recovery lease) | done | `tape/lease.py`, `tests/test_tape.py::test_lease_*` |
| P2-02 (POSIX process/boot/fs detection) | done | `platform/posix.py` |
| P2-03 (recovery: adopt an orphan segment after S15) | todo | needed: `open()` currently refuses to roll onto an existing segment (BBX-E140) |

Crash matrix proven here: S1, S2, S3, S4, S5, S14, S15 (`tests/test_crash_points.py`, 7 cases).
Not yet attempted: S6 … S13, S16 (`poweroff`), S17 (`SIGBUS`).

## Not testable in this environment

* Linux 6.x `SIGBUS` behaviour on sparse vs preallocated mappings (stage S17) — needs a dedicated probe.
* macOS / Windows filesystem cells, Python 3.12 / 3.13.
* Real `poweroff -f` / `dm-flakey` fault injection (stage S16).
