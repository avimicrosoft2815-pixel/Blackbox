# G-Promise — 2026-11-14 — trunk fdb3875 — verdict: pass

**Two signatures.** Built and self-reviewed at `af908bc` (below, unchanged except where this commit corrects it);
**independently re-run and counter-signed at `fdb3875`** by a different model family on a different machine. The
counter-signature is at the end of this file, under *Counter-signature*. The earlier header named `af908bc`, whose
own CI run (`43678767989`) was 9/10 — C40, in `docs/defects.md` — so the gate is re-signed at the newest commit whose
run is 10/10.

**Who signed the first pass.** The plan requires a reviewer from a different model family or a fresh context,
working from the plan, the diff and the *pushed* tree. When this gate was first signed, no second model was
available, so that pass was run by the builder in a **fresh clone of the pushed branch** (`/tmp/gp`, `git clone --branch
arena/a781c68f-blackbox` at `af908bc`), from the gate rows in `AGENT_FINAL.md` §5.1 and §3 rather than from memory.
That is a weaker check than the plan asks for, and it is labelled `requires humans` in `docs/STATUS.md` §Labelled
rather than being counted as independent. Everything below is reproducible from the clone: the commands are named
with their output.

## Must-be-true rows (Part B §5, verbatim) → evidence → ✓/✗

| Row (verbatim) | Evidence | Verdict |
|---|---|---|
| S1–S5, S14, S15, S17 green | `tests/test_crash_points.py`, `tests/test_adoption.py`, `tests/test_enospc.py`; fresh clone: `make test` → 328 passed, 1 skipped | ✓ |
| P2-09 ≥ 500 randomized parent-`SIGKILL` trials, 0 violations over effective N | fresh clone `tests/kill/kill_matrix.py --trials 500` → `500/500 trials, 0 with violations`, 10 cells × 50, `not_started` false on every trial (the timer starts at the child's `READY`), exit 0 | ✓ |
| content=hash 1 KiB p50 ≤ 25 µs / p99 ≤ 250 µs with CHAIN | `bench/results/2026-11-14-writer-throughput.json` `results.ext4.rows[gate=true]`: p50 11.057 · p95 20.53 · p99 51.662 µs | ✓ |
| ≥ 40 000 ev/s | same row: 73 338 ev/s (N 200 000, warm-up 20 000) | ✓ |
| RSS ≤ 24 MiB above baseline | `bench/results/2026-11-15-recovery.json` `rss_write_only_process.ext4.above_baseline_mib` = 1.56 MiB at `head_off` 4 723 072 (28.13 % of the segment) | ✓ |
| recovery ≤ 120 ms / 10 k frames | same file, `results.ext4.recovery`: p50 236.83 ms for a 22 312-frame tape written by 10 000 events = **106.1 ms/10 k**, n 30 rounds | ✓ |
| P1-19 verdict written (ADR-005) | `docs/adr/ADR-005-*.md` + `docs/DEVIATIONS.md` DV-010; median p50 ratio 1.059 @ 10 k ev/s and 1.065 @ 40 k ev/s | ✓ |
| `blackbox run -- sh -c 'kill -9 $$'` recovers | fresh clone: `run r_40ead954098fb5df · exit 137 · signal 9 · recovered → capsules/c_40b2ebb29e649534.bbx (SEAL_INTERRUPTED, root 14f1737e50b6)` | ✓ |
| capsule v0 root verifies | `tests/test_capsule.py` (9) — re-seal byte-identical, one changed byte breaks the root, traversal/duplicate/newer-manifest rejected | ✓ |
| P2-12 idempotence | `tests/test_recovery.py` — 10 recoveries ⇒ 1 capsule hash, 1 `.bbx`; second recoverer `BBX-E210`; dead holder's lock taken over | ✓ |
| C32's row (64 KiB `content=full`, blob on the keeper) | `bench/results/2026-11-15-payload-blob.json` `results.ext4.rows[gate=true]`: p50 226.864 · p99 461.281 µs · 4 356.4 ev/s · 0 tape syncs · 0 blob syncs on the calling thread | ✓ |
| amplification ≤ 1.25× at the default config, 2× hard fail | `bench/results/2026-11-15-recovery.json` `results.ext4.amplification_by_batch[2]`: 1.231× (3 150 352 / 2 560 000); publish-per-event 1.843× and 4 KiB 1.263× are above the row and below the hard fail, and they are reported as such | ✓ |
| CI green for this HEAD | run `43678447134` at `4619b90` — ten jobs, ten `success`; later commits are docs/signature-only (`af908bc` is P1-02b, pushed, run pending at review time and recorded in STATUS) | ✓ |

## Reviewer checklist items run (§3), and findings

- **1–6 (measurement honesty).** Re-read every number against its JSON, not against STATUS: recovery, RSS,
  amplification and the 64 KiB row all match their files. Finding fixed this turn: `1.231× (4,718,976 /
  2,560,000)` did not divide out — 1.231 is `amplification_by_batch[2]` (3 150 352) and 4 718 976 is the per-event
  numerator (1.843). C38 fixed the file (each object now states its configuration and what it counts) and added a
  test that recomputes every quoted ratio. Item 5 (physical plausibility) checked: 1.56 MiB above baseline for a
  CPython process writing 4.7 MB through a 16 KiB batch buffer is plausible; the earlier `5.15 MiB total RSS`
  figure is not used anywhere.
- **20–23 (reporting honesty).** 20: `git fetch` shows `origin/arena/a781c68f-blackbox == HEAD` after every
  commit in this turn; the clones used for this review were made from GitHub, not from the working tree. 21: STATUS
  was rewritten into the §10 shape; one HEAD, one verdict per row, no stale narrative; the test count is read from
  `--collect-only` by a test, not typed by hand. 22: `tests/test_state_consistency.py` is green and enforces
  STATE↔progress agreement, a `last_green_commit` that is an ancestor of HEAD, and every baseline key present with
  a JSON path. 23: labels in §Labelled are exactly the four allowed strings.
- **7–10 (kill honesty).** The fresh-clone 500-trial run reports effective N by starting its timer at the child's
  `READY`, so `not_started` is reported per trial and never counted as a pass; `harness_abort` is counted
  separately. S4 tears bytes at the level the stage defines (split `pwrite` into body and CRC) because a process
  kill cannot tear a 64 B `pwrite` — the test proves the tear exists first.
- **11–13 (format honesty).** No byte-format change in C35–C39 or P1-02b. `p1-09b-retention.json` changed shape
  (JSON Lines → one document) but it is a results file, not a format; `format.md` is untouched this turn.
- **14–19 (platform honesty).** P1-02b touched signatures only. The AST guard still covers `os.fsync`/`os.fdatasync`
  (C32) and `platform.pwrite`/`pread`; the macOS shim's `fcntl(F_FULLFSYNC)` is now what the *tests* count (C36),
  which was the actual defect — three macOS cells had been failing on a counter that watched the wrong function.
- **24 (forbidden words).** `tests/test_forbidden_words.py` green over the tree and `git log`. It caught one this
  turn: the word the plan bans appeared in a `bench/_stats.py` docstring and was rewritten.

## Numbers (ext4 gate column, one line per §15.1 row touched by this gate)

- 1 KiB `content=hash`, group commit 16 KiB, CHAIN on — N 200 000 · warm-up 20 000 · p50 11.057 · p95 20.53 ·
  p99 51.662 µs · 73 338 ev/s — `bench/results/2026-11-14-writer-throughput.json`
- Recovery — 22 312-frame tape from 10 000 events · n 30 · p50 236.83 ms = 106.1 ms/10 k · budget 120 ms —
  `bench/results/2026-11-15-recovery.json`
- Disk amplification — 16 KiB group commit 1.231× · publish-per-event 1.843× · 4 KiB 1.263× · 64 KiB 1.222× —
  `bench/results/2026-11-15-recovery.json`
- RSS above baseline — 1.56 MiB at 28.13 % of a segment — `bench/results/2026-11-15-recovery.json`
- 64 KiB `content=full` with the blob on the keeper — N 20 000 · warm-up 2 000 · p50 226.864 · p99 461.281 µs ·
  4 356.4 ev/s · 0 syncs on the calling thread — `bench/results/2026-11-15-payload-blob.json`
- Randomized parent-`SIGKILL` — 500 effective trials, 10 cells × 50, 0 violations, 0 `not_started` — fresh clone
  of `af908bc`

## Labelled items created by this gate and why

- **Independent review — `requires humans`.** The gate above is a self-review from a fresh clone. The plan asks for
  a reviewer from a different model family; none was available in this session. Recorded in `docs/reviews.md` and in
  STATUS §Labelled so that no later reader counts it as independent.
- **Windows/macOS benchmark rows — `not measured`.** The test gates are green on those cells (run `43678447134`);
  no §15.1 number in this repository comes from a runner.
- **P2-09 week of nightlies — `requires time`.** `schedule` runs only on the default branch; the clock starts at
  merge.

## Commands to reproduce (for a counter-signing reviewer)

```
git clone --branch arena/a781c68f-blackbox https://github.com/avimicrosoft2815-pixel/Blackbox
cd Blackbox && python3 -m venv .venv && .venv/bin/pip install -e . pytest hypothesis
make test PY=.venv/bin/python                     # expect: 337 passed, 1 skipped (the named POSIX skip)
PYTHONPATH=src .venv/bin/python tests/kill/kill_matrix.py --trials 100 --seed 20261115 --root /tmp/kb --filesystem ext4
.venv/bin/python tools/bench_smoke.py              # expect: 9 keys within 10 % of ext4 baseline
PYTHONPATH=src .venv/bin/python -m blackbox run -- sh -c 'kill -9 $$'
.venv/bin/python tools/ci_status.py arena/a781c68f-blackbox
```

Nothing above writes outside the clone and `/tmp`. The §15.1 latency rows are **not** reproducible on different
hardware; a counter-signature accepts them on the builder's JSON (which `tools/bench_smoke.py` ties to
`docs/STATE.json`) and says so.

## Counter-signature — independent reviewer, different model family, different machine

Reviewer: an Arena agent of a different model family, relayed by the owner. Fresh clone of
`arena/a781c68f-blackbox` at **`fdb3875`**; Linux x86_64, CPython **3.13.18**, ext4 (`/dev/root`); not the builder's
machine.

| Command | Result |
|---|---|
| `python -m pytest tests -q` | **337 passed, 1 skipped** (the named POSIX skip) |
| `tests/kill/kill_matrix.py --trials 100 --seed 20261115 --root <ext4 dir> --filesystem ext4` | **100/100 effective trials, 0 violations, 0 `not_started`**, 10 cells × 10 (`process`/`kernel`, segment 64 KiB / 1 MiB, event 32–1 024 B, batch 0/4096), 32.1 s |
| `tools/bench_smoke.py` | `9 keys within 10 % of ext4 baseline` |
| `python -m blackbox run -- sh -c 'kill -9 $$'` | `exit 137 · signal 9 · recovered → capsules/c_….bbx (SEAL_INTERRUPTED)`, with the `recovery.done.*` markers in the run directory |
| `tools/ci_status.py arena/a781c68f-blackbox` | `fdb3875 · success`, `c76f917 · success`, `372006f · success`, `7ba55b0 · success`, `a6f4faa · success`, **`af908bc · failure`**, `2c8e28f · success`, `a4455e6 · success` |
| §15.1 latency/throughput rows | **not re-measured here** (different hardware); accepted on the builder's JSON, which the smoke check ties to STATE |

**Verdict: pass — independently confirmed for the suite, the kill invariant, the launcher recovery and the smoke
keys.** The reviewer also found C40 (the `af908bc` run was 9/10 while three documents called it green), which is
fixed in the same commit as this signature: the flaky Windows cell, the per-OS statement of the group-commit window,
the re-signature at `fdb3875`, and `STATE.last_green_commit`.

Signed: *independent (Arena agent, different model family, owner-relayed)* — `fdb3875`, 2026-11-15.
