# Stage 2a report — P0 complete, P1 core standing up

**Date:** 2026-11-14 · **Branch:** `arena/e10fc603-blackbox` · **Base:** `02fd609` · **Pushed:** no (waiting for `PUSH`)

## Commits

| sha | scope |
|---|---|
| `2a1b37f` | packaging skeleton, 3.11 floor, compatibility matrix (P0-01, P0-02, P0-04) |
| `a5a6818` | constants + closed error registry (P0-03, P0-05) |
| `3ff261f` | config loading, env over file (P0-06) |
| `5007662` | CRC32C / canonical CBOR / ids (P0-07, P0-09, P0-10) |
| `7bf29c6` | frame codec + backward walk (P1-01) |
| `fea279c` | meta page + two-slot head (P1-06, P1-07) |
| `260bfd0` | preallocation, segments, seal footer (P1-02, P1-09, P1-13, P1-20) |
| `55b4d42` | writer, scanner, reader, chain (P1-08, P1-11, P1-12, P1-18) |
| `8c99b96` | POSIX shims + lease (P2-01, P2-02) |
| `1a3d505` | guard rails, crash matrix, CI (P0-08, P0-11…P0-13) |
| `ff6753c` | format/guarantees/progress docs + plan amendment (P0-00) |

## Test state

`105 passed in 1.39s` (Python 3.11.2, pytest 8.3.4, hypothesis 6.122.3). Runtime dependencies: none.

Crash matrix actually exercised — a real child process dies via `os._exit` (no atexit, no flush) and the tape is
then scanned:

| stage | what it kills | result |
|---|---|---|
| S1 | mid-payload | earlier commits intact, in-flight frame invisible |
| S2 | after payload, before trailer | same |
| S3 | after header, before head publish | same |
| S4 | between the two halves of a head slot | previous commit readable, in-flight commit lost |
| S5 | after head publish | commit survives |
| S14 | after `SEG_END`, before the next segment | all frames survive |
| S15 | next segment durable, head still names the old one | all frames survive; orphan segment detected |

Not attempted yet: S6 … S13, S16 (`poweroff -f`), S17 (`SIGBUS` — the claim stays open until S17 delivers it).

## Measured here (`bench/results/2026-11-14-writer-append.json`)

2,000 events, 200 warm-up, 200 B payload, tmpfs, one process, append + publish per event.

| mode | chain | p50 | p95 | p99 | events/s |
|---|---|---|---|---|---|
| process | on | 148.1 µs | 226.0 µs | 281.5 µs | 6,218 |
| process | off | 97.6 µs | 175.5 µs | 229.1 µs | 9,127 |
| kernel | on | 321.3 µs | 426.1 µs | 508.8 µs | 2,977 |

**These are far off the §15.1 budget** (content=off ≤8 µs p50, ≥40,000 ev/s). The cause is known and is not the
frame codec: every `publish()` re-opens and rewrites the 128-byte head file, which costs ~100 µs in Python. The
budget requires the batched publish path (P1-14) and a kept-open head descriptor (P1-15), neither of which is in
this batch. Until those land, the numbers above are a description of the current code, not of the design.

## Deviations and decisions recorded (rule 9)

1. **Segment magic.** `docs/format.md` said `BBXSEG2` in an 8-byte field; slicing a 7-byte literal into an 8-byte
   field shortened the bytearray and shifted the whole record. Code and doc now both say `BBXSEG2` + NUL pad, and
   `constants.py` asserts every magic's width.
2. **`host_fingerprint`.** Dropped `socket.gethostname()` — it can reach NSS/DNS, which the no-egress rule forbids.
   Now `os.uname().nodename` (POSIX) / `COMPUTERNAME` (Windows). `tests/test_no_egress.py` enforces this.
3. **F4 applied** (owner-authorized, confined to P1-13): `body_len`, `body_crc32c`, `body_hash` are defined over
   the uncompressed body; the codec lives in the segment header; the `.seal` byte layout is unchanged. Noted at the
   top of `tape/seal.py`.
4. **Chain link.** The next segment's header now chains to the previous **seal's** chain value, not to the writer's
   batch chain — otherwise segment 2 starts a parallel chain. Found by the multi-segment test.
5. **Bug found and fixed by that test:** the CHAIN frame was encoded before a possible roll, so a roll inside
   `publish()` duplicated a seq number and dropped the next one. The roll decision now happens first.

## Open, and next in order

1. P2-03 recovery: `open()` refuses to roll onto an existing segment (S15 leaves an orphan). Recovery must adopt or
   reuse it; today it raises `BBX-E140`.
2. P1-14 / P1-15: batched publish + persistent head descriptor — this is what the §15.1 budget depends on.
3. P1-03/04/05/10, P1-16/17/19, P1-21…P1-27 still `todo` (see `docs/progress.md`).
4. Not testable here: 3.12/3.13, macOS, Windows, `poweroff`, `SIGBUS`.

## Review findings status

Only F4 was applied, and only because it was authorised as confined to P1-13 with the `.seal` layout unchanged.
F1, F2, F3, F5 … F13 remain untouched and wait for an explicit word. F8 stays re-scoped to "keeper pre-creation is
the default; measure the synchronous path per OS" — the measurement is not done yet.
