# Kill evidence — what was actually run here

_Python 3.11.2 · Linux 6.1.158 x86_64 · tmpfs · single process per trial · 2026-11-14_

## 1. Named crash points (P2-08, `os._exit`)

Deterministic stages, one trial each, in `tests/test_crash_points.py` (part of `make test`):

| stage | where it dies | modes | segment sizes | trials | result |
|---|---|---|---|---|---|
| S1 | after the payload copy, before the trailer | process | 64 KiB | 1 | green |
| S2 | after the trailer, before the header | process | 64 KiB | 1 | green |
| S3 | after the header, before the head publish | process | 64 KiB | 1 | green |
| S4 | between the two stores of a head slot | process | 64 KiB | 1 | green |
| S5 | after the head publish | process | 64 KiB | 1 | green |
| S14 | after `SEG_END`, before the next segment | process | 64 KiB | 1 | green |
| S15 | next segment durable, head still names `n` | process | 64 KiB | 1 | green |
| S6 … S13 | not implemented | — | — | 0 | **todo** — the writer has no code path at those points yet |
| S16 (`poweroff -f`), S17 (`SIGBUS`) | needs a VM / fault-injected block device | — | — | 0 | **not testable in this environment** |

## 2. Randomized asynchronous kills (P2-09, parent `SIGKILL`)

`tests/kill/kill_matrix.py`, 500 trials, kill delay uniform in [50 ms, 250 ms], 4 configurations × 2 batch
settings, ~62 trials each. Raw evidence: `bench/results/2026-11-14-kill-random.json`.

| configuration | trials | invariant violations |
|---|---|---|
| process · 64 KiB segments · 32 B events · publish per event | 63 | 0 |
| process · 64 KiB segments · 32 B events · 4 KiB group commit | 62 | 0 |
| process · 64 KiB segments · 1 KiB events · publish per event | 63 | 0 |
| process · 64 KiB segments · 1 KiB events · 4 KiB group commit | 62 | 0 |
| process · 1 MiB segments · 256 B events · publish per event | 63 | 0 |
| process · 1 MiB segments · 256 B events · 4 KiB group commit | 62 | 0 |
| kernel · 1 MiB segments · 256 B events · publish per event | 63 | 0 |
| kernel · 1 MiB segments · 256 B events · 4 KiB group commit | 62 | 0 |

Invariants per trial: recovered seq numbers form a dense prefix from 1 · no gaps inside the committed region ·
no torn tail inside the committed region · head present and not behind the recovered frames · the tape reopens
(adopting an interrupted roll) and a further append loses nothing.

P2-09's Done-when is "0 invariant violations over a week of runs". 500 trials in one run is **not** a week;
nightly runs in CI are the remaining evidence.

### What the matrix found (each is now a fix and a test)

| trial | symptom | cause | fix |
|---|---|---|---|
| seed 352 | reopen refused the tape (`BBX-E133`) | adoption required the last frame of `n` to be `SEG_END`, but `roll()` skips that frame when it cannot fit | adopt by sealing the committed prefix `[4096, head_off)` instead; `test_adoption_does_not_require_a_seg_end_frame` |
| seed 388 | reopen refused again, `segment 4 already exists` | `segments.create` wrote the header in place, so a kill left a half-headered `<n>.seg` that could never be rolled onto | create `<n>.seg.tmp`, `fdatasync`, `os.replace` (as §8.6 step 3 already said), `fsync(dir)` |
| seeds 209, 307, 386 | scan raised, or reported a torn tail | a reader arriving while the writer initialises the directory (dir → `meta` → `head`) saw "corruption" | an uninitialised tape is empty; it is only an error when segments already hold frames (`BBX-E220`/`BBX-E222`) |
| seed 396 | `BBX-E220` on a fresh tape | the "does this segment hold frames" probe tested the file size, and every segment is preallocated to full size | probe decodes one frame from the body instead |
