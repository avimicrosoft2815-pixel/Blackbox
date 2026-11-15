# BlackBox build status — 2026-11-16 — trunk `d901811` — gates: **G-Promise pass** (built and self-reviewed at `af908bc`, independently counter-signed at `fdb3875`; see `docs/gates/G-Promise.md`), G-Capture, G-Attach, G-Capsule, G-Sec, G-Release not yet opened

**Branch** `arena/a781c68f-blackbox` · **base** `31241d6` · CI at `d901811`: run `43698231222`, ten jobs, ten
`success`; the three runs before it — `43697929744` at `54c3aba`, `43697556490` at `23cdb7b`, `43697198262` at
`846e01a` — were also ten of ten, and `846e01a` is **the first all-OS green since `c7919f9`**. This note's own
commit is docs-only and its run is pending when it is written, which is what the `STOPPED` line reports. The three Windows cells had been red for four commits and the
cause was mine: the Windows teardown fixture in `tests/conftest.py` called `getattr(obj, "_head", None)` on every
object in `gc.get_objects()`, `anthropic` keeps its optional dependencies behind a `LazyProxy` that raises
`MissingDependencyError` when the extra is absent, and the teardown error landed on nine innocent property tests once
`capture-sdks` put the SDK in the environment (defect 25). Found through the check-run **annotations**, not the job
log: GitHub redirects logs to a host this sandbox cannot reach, so `tools/ci_summary.py`'s `::error::` lines are the
only readable failure trail — they now carry 600 characters instead of 200. Before that, `d075a32` (`43695573285`)
and `4bb722d` (`43696068136`) were 7/10 on that same Windows defect and `67b5ae5` (`43696841639`) was 7/10 on a
fail-open test of mine whose breakage did not break on Windows (defect 26, still open). The run before this gate work
is `43695177370` at `27b82a3`, ten of ten. Older red runs keep their numbers and their verdicts — `af908bc`
(`43678767989`, 9/10, C40), `15cb55c` and `6d52088` (7/10, a fork-only multiprocessing test) — because a later green
tree is not a green commit.

**product: pre-alpha — the recorder, the tape, recovery, the launcher, the SDK lifecycle/decorator/node layer, the
loopback mock provider and capture seams A (client hooks over `httpx` **and** `httpx2`) and B (transport byte tee)
with the ordered preflight are real and tested, as is seam C (SDK method patches, against the real `openai` 3.51.0 and
`anthropic` 2.8.0), the tape sink that turns seam events into `MODEL_CHUNK` frames, and the day-14 rule: an unmodified
`openai` script killed with `SIGKILL` mid-stream is recovered with chunk and byte counts that never exceed what the
provider pushed. The proxy, the launcher integration, the viewer and the diagnosis workflow are not built yet.**
Suite: **Total: 482 tests** — 481 pass here and 1 is skipped on POSIX by name
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
| Group-commit window, per OS (C40) | §8.6, C3 | `tests/test_keeper.py::test_low_rate_event_is_published_within_the_window` starts its own window and `test_a_window_older_than_the_bound_publishes_inline` states the expired-window rule; the ≤ 5 ms row is measured on ext4/Linux only and the Windows bound (one 15.6 ms timer tick for a keeper-only drain, inline publish for arriving events) is stated in `docs/compatibility.md` and marked `not measured` |
| **P3-02/P3-03/P3-04** decorators, node stack, context managers | §10.2, P3-02–04 | `src/blackbox/api.py`, `src/blackbox/rec/context.py`, `tests/test_api_decorators.py` (6), `tests/test_context_nodes.py` (7): one `NODE_OPEN`/`NODE_CLOSE` pair per call for all five decorator kinds; signature, `__name__` and `__doc__` survive; an exception closes the node with its type and is re-raised; a **disabled** decorated call costs under 1 µs of median overhead; async keeps its node across awaits; 50 nested steps stay balanced with each level's parent the level above; `mark` lands in the node it was called inside; `handled_error` records a handled exception where it was handled; a plain thread gets a `detached:` root (marked `detached: True`) instead of an orphan; a forked child writes to its own tape |
| **P3-01** SDK lifecycle: `install`/`uninstall`/`shutdown` | §10.2, P3-01 | `src/blackbox/api.py`, `tests/test_api_lifecycle.py` (6): double install ⇒ one writer UUID and one `RUN_START`; 16 threads install at once ⇒ one writer and `refs` 16; `uninstall()` during an in-flight append leaves the tape open and the frame lands; `BLACKBOX_DISABLED=1` creates no tape and `emit()` returns 0; an unwritable data dir leaves the app running with the reason recorded; `shutdown()` is idempotent and install works after it |
| **P3-13MP** loopback mock provider | §18.5 (G-Capture needs it), D-021 | `src/blackbox/cap_sdk/mock_provider.py`, `tests/test_mock_provider.py` (9): OpenAI and Anthropic SSE dialects over a real socket, driven with stdlib `http.client` so the test cannot be coupled to the library under test; knobs for chunk count, inter-chunk delay, first-byte delay, truncation, mid-stream error, non-200 status and payload size; the request log proves what arrived; the source is asserted to contain no wildcard bind and to bind an ephemeral loopback port |
| **P3-05a** seam A, httpx response hooks | §18.5, D-022 | `src/blackbox/cap_sdk/seam_httpx_hooks.py` + `tee.py`, `tests/test_seam_hooks.py` (7): the hook wraps the still-unread stream, so every chunk is recorded — the recorder's count equals the number of chunks the application observed in the same run, and the body is sha256-identical with and without the seam; a request hook stamps the clock before the request goes out so `first_byte_ms`/`total_ms` are end-to-end; a response whose body is already gone is recorded `partial=True` with no byte count; a recorder that raises never reaches the application (fail-open); `uninstall` restores both client classes and is idempotent; the caller's own event hooks still run |
| **P3-05a/05b async** — the path that was implemented and unexercised | §18.5, defect 20 | `tests/test_seam_async.py` (4), `src/blackbox/cap_sdk/tee.py`: `wrap_stream` picks the tee from the stream (`__aiter__` ⇒ `AsyncTeeStream`), so an async response is recorded chunk-by-chunk instead of being handed a sync wrapper that fails the moment it is iterated; both seams are tested through `AsyncClient` for chunk count, byte total and sha256 equality against an unseamed client |
| **P3-05b** seam B, transport byte tee | §18.5 | `src/blackbox/cap_sdk/seam_httpx_transport.py`, `tests/test_seam_transport.py` (9): the tee is compared against an unmodified client by sha256, the recorded chunk count against what the application actually read, a truncated stream is recorded short instead of completed, an error mid-stream is recorded and re-raised, an abandoned stream is closed with the bytes that arrived, and both `client=` (instance) and class-patch installs restore exactly what they patched |
| **P3-05d** ordered preflight A→B, `capture_seam` | §18.5 | `src/blackbox/cap_sdk/preflight.py`, `tests/test_preflight.py` (8), `SUPPORTED.md`: forcing A picks B and records why, forcing both leaves the recorder installed-but-not-capturing with two reasons and no exception, an absent library is `unavailable` rather than `forced-failed`, an `install` that raises does not escape, a client handed to the preflight is wrapped rather than having its class patched. `capture_seam` is written into the decision (`as_dict()`) and tested; writing it into a `run.json` waits for the sink (P3-06) and is labelled pending in `SUPPORTED.md` |
| **P3-05c** seam C, SDK method patches | §18.5, D-023 | `src/blackbox/cap_sdk/seam_sdk_methods.py`, `tests/test_seam_sdk_methods.py` (10) against **real** `openai` 3.51.0 and `anthropic` 2.8.0 pointed at the mock provider: `chat.completions.create` (streaming and not), `embeddings.create`, `messages.create`, sync and async; the SDK signature survives the patch (`inspect.signature` compared); an unrecognised SDK shape is skipped and listed, never guessed; a 429 is recorded as an error and re-raised. Every record is `partial=True` with `bytes_received = 0` because a parsed event is not a wire byte (D-023) |
| **P3-06** the sink: seam events become frames | §18.5, D-024 | `src/blackbox/cap_sdk/sink.py`, `tests/test_sink.py` (8): 2 000 chunks become **8 chunk frames** (bound 40) with the payloads concatenating back to the identical body; the time bound flushes at 100 ms without waiting for 8 KiB; `MODEL_REQUEST` before every chunk and `MODEL_RESPONSE`/`MODEL_ERROR` after the last; frames land on a real tape and the same `scanner` a recovery uses reads them back with a sha256 match against the body; an unfinished call is left as chunks with no response, which is the shape `recovery` classifies as `INTERRUPTED_MID_STREAM`; a sink that raises is counted and never reaches the application |
| **Day-14 rule** (the gate's core mechanism; **G-Capture itself is still open**) | §4, G-Capture, D-025 | `tests/_day14_child.py` + `tests/test_day14_kill.py` (3): an **unmodified** `openai` script (client built after install, `chat.completions.create(stream=True)` as documented) is killed with `SIGKILL` mid-stream; `recovery.recover()` then reports the node as `INTERRUPTED_MID_STREAM` with `chunks ≥ 1`, `partial_stream_bytes > 0`, and **both no larger than the mock provider's own tally of what it pushed down the socket**; the control run (no kill) records the whole stream and ends with `MODEL_RESPONSE` whose byte count equals the sum of its `MODEL_CHUNK` payloads; `capture_seam` and the coalescing parameters are readable in the request frame |
| **Fail-open, measured as identical application behaviour** | P3-14, §4 | `tests/test_failopen.py` (8): a 100-call agent run twice — once bare, once with seam A and the sink installed — with **identical return value, identical stdout**, and when it raises at call 37, the **same exception type and message**. The recorder is then broken three real ways and the comparison repeated: an impossible configuration (`segment_bytes=1`, which `TapeWriter` refuses with `BBX-E130` on **every** OS: `install()` returns `enabled: False` and that error as its reason, asserted, so the test cannot pass against a recorder that was never broken), a data directory that is a file (POSIX, because Windows let the writer open anyway — defect 26), a keeper that raises on every publish, and a tape truncated by 512 bytes the way a previous `kill -9` leaves one |
| **The Windows teardown fixture touches nothing it did not identify** | defect 25 | `tests/_hygiene.py` + `tests/test_gc_hygiene.py` (3): the fixture finds a leaked writer by **exact type**, proved against an object that raises on any attribute access and a subclass that must not be mistaken for its parent; the SDK's real `LazyProxy` is instantiated to show the shape still raises. This is why three Windows cells were red from `c7919f9`: the old fixture called `getattr(obj, "_head")` on every live object, `anthropic` keeps optional dependencies behind a proxy that raises on load, and the resulting `ERROR` landed on nine innocent property tests |
| **The nightly workflow actually runs** | defect 28, P2-09 | `.github/workflows/nightly.yml`: both steps named files that do not exist, so the only scheduled run this repository has had failed with exit 2 (`kill-matrix`) and exit 4 (`cbor-fuzz`). Now `tests/kill/kill_matrix.py --trials 2000 --json …` and `bench/fuzz_cbor.py --iterations 100000 --out …`, each smoke-run here before the workflow was touched |
| **Both client modules, both directions, and cancellation** | P0 §"verify, do not infer" | `tests/test_capture_modules.py` (11): the same five claims run against **`httpx` 0.28.1 and `httpx2` 2.16.0** directly (not through an SDK): a stream is byte-identical to an unseamed client with `call.chunks` equal to the reads the application actually made, a plain POST is one call and one chunk with `stream is False`, a truncated stream is recorded short and never completed, an async stream is identical with every read counted, and a **cancelled** task keeps `CancelledError` while the call is left `partial=True` with the chunks that arrived. The chunk expectations are the *observed* read count, never a fixed number — a six-chunk stream can arrive as four reads on a slow runner, and asserting otherwise is a flake (C40) |
| **A stream closed early is marked `partial`** | D-023 applied end to end | `tests/test_capture_modules.py::test_cancellation_mid_stream_stays_cancelled_and_leaves_the_call_open` (both modules), `src/blackbox/cap_sdk/tee.py`: `break`, timeout or cancellation closes the tee, which sets `call.partial = True` and records the status that actually arrived; a completed iteration records the same call without the marker. The mock provider now labels a non-streaming answer `application/json` and a stream `text/event-stream`, so `call.stream` is decided from the header the provider really sent rather than from a fixture that said SSE to everything |
| **Capture scope: an explicit allowlist, with skips counted** | D-026, P0 §"immediate" | `src/blackbox/cap_sdk/scope.py`, `tests/test_capture_scope.py` (12): the default scope is `api.openai.com`, `api.anthropic.com` and loopback — nothing else; `capture_hosts=` / `BLACKBOX_CAPTURE_HOSTS` extend it, `"*"` is the only way to capture everything and it says so in `source`; ports and schemes are ignored, `*.example.com` matches subdomains and **not** the bare domain. Seam A and seam B both consult it and return before opening a call, so nothing half-written is left on the tape; the skip is counted in `detail["scope"]["stats"]["out_of_scope"]`, because an absence is not evidence. Proved in both directions: an out-of-scope loopback request yields **zero** calls and byte-identical output, and an unreachable unrelated host fails with the *same* exception type as it does unseamed |
| **A data directory that is a file is refused by name, on every OS** | defect 26, closed | `src/blackbox/api.py`, `tests/test_failopen.py::test_a_data_directory_that_is_a_file_is_refused_on_every_os`: `install()` now rejects an existing non-directory before any directory is touched (`NotADirectoryError`, "exists and is not a directory") instead of relying on `os.makedirs` to refuse — which on Windows it did not, and the writer opened against a tape directory that cannot exist. The test asserts the reason string, so a platform that starts tolerating the case cannot pass silently. **Windows evidence is the CI cell, not an observation made here** |
| **Fail-open is per callback, not per seam** (the reviewer's P0 at `ee210ae`) | P0 §"immediate", D-027, defect 27 | `src/blackbox/cap_sdk/failopen.py`, `tests/test_capture_failopen.py` (18): `open_call`, `receive` and `close_call` each raising, on seam A and seam B, sync **and** async — the application gets every byte, the same body and no exception, and the failure is counted. Reproduced first in a clean worktree at `ee210ae` (the reviewer's script printed `ESCAPED RuntimeError: recorder blew up on a chunk`); after the fix the same script prints `SURVIVED bytes=849`. Also asserted: `CancelledError` is not swallowed, a provider 429 arrives as 429 with the library's own error, the failure sample is bounded at 64, and no exception *message* is ever recorded |
| Capture layer's network exception | P0-08, P0-13, D-020 | `tests/_capture_policy.py` + `tests/test_stdlib_only.py`, `tests/test_no_egress.py`, `tests/test_codec.py`: four named files are exempt and nothing outside `cap_sdk/` can be; `httpx` is allowed only **inside a function body**, so `import blackbox` works with httpx absent; the mock provider may bind loopback only and the wildcard-bind source check runs; `httpx==0.28.1` lives in its own `capture` extra, `[project].dependencies` is still empty |
| Lint, types, forbidden words, egress, stdlib-only | C6, C12, P0-14 | `make fmt` 0 findings, `mypy` strict 0 findings, `tests/test_forbidden_words.py`, `tests/test_no_egress.py`, `tests/test_stdlib_only.py` |
| CI on three OSes × three Pythons + docs | C7 | run `43697198262` at `846e01a` — ten jobs, ten `success`, Windows included; `docs/compatibility.md` holds the per-cell verdicts and every historical red run |

## Labelled

| Item | Label (requires humans / requires time / not testable here / blocked) | Reason / fix attempts |
|---|---|---|
| **Independent gate review** | G-Promise: **independent review pass (owner-relayed)**; every other ticket row: requires humans | G-Promise was counter-signed by a different model family on a different machine (fresh clone of `fdb3875`, CPython 3.13.18, ext4): 337 passed / 1 skipped, 100/100 kill trials with 0 violations, smoke 9 keys, launcher recovery, and it was that reviewer who found C40. The signature and a copy-paste reproduce block are in `docs/gates/G-Promise.md`. **Ticket rows in `docs/reviews.md` stay `self`** until a second model is wired into the loop; none of them counts as independent. |
| P2-09 week of nightlies | requires time | **One scheduled run has ever happened**: `43675454143` on `main` at `31241d6`, 2026-11-15 04:08Z, and both jobs failed on defect 28 — the steps named `tests/kill/randomized_kills.py` and `tests/test_cbor_fuzz.py`, which do not exist (exit 2 and exit 4). Both now call the real drivers, smoke-run locally (3 trials, 500 buffers). The clock cannot start until this work is merged: `origin/main` is still at `31241d6`, the branch's base — verified with `git merge-base --is-ancestor`, not assumed from a report. |
| 3-person stranger test | requires humans | Prepared to one command: `docs/onboarding.md` holds the setup line and three tasks; nobody outside this session has run it. |
| Blinded diagnosis benchmark | requires humans | Prepared: `docs/benchmarks/diagnosis.md` holds the checklist and the capsule set is reproducible with `blackbox recover`. Needs a second and third person. |
| S17 sparse-control run under `sudo` | requires humans | `tools/s17_control.sh` builds the 2 MiB tmpfs control and prints the signal; no superuser here. |
| Windows / macOS benchmark rows | not measured | The **test** gates are green there (run `43698231222`); no §15.1 number in this repository comes from a runner, and none will. |
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
- group-commit window · ext4 (Linux only; the Windows bound is stated, not measured — `docs/compatibility.md`) · 1 ev/s 4.603 ms · 1 000 ev/s 4.999 ms · unbounded 3.572 ms (120.99 ev/commit) · `bench/results/2026-11-14-writer-throughput.json`
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

The defect list (seed → invariant → cause → fix → test) lives in `docs/defects.md` — twenty-six entries. Six are
from this stretch of work: defects 23 and 24 (a seam that installed, reported `installed`, and captured nothing because both real SDKs speak
`httpx2`; and a request frame that could say `model: null`), defect 25 (the Windows teardown fixture that probed
every object in the collector and turned `anthropic`'s `LazyProxy` into nine teardown errors), defect 26 (open: on
Windows `install()` accepted a data directory that is a file), and the two older capture-layer gaps (defect 20, the
untested async path, still open; defect 21, a wall-clock assertion that reddened `43693106857` at `714554c`, fixed at
`771f5ea`).
Twelve deviations, all dated and argued in `docs/DEVIATIONS.md`: DV-005 (one checksum, CRC-32), DV-006/007 (S17
degrades instead of raising, including the first segment), DV-008 (P2-03/P2-04 written with an honest
`capabilities()`), DV-009/010 (ADR-005 store change; the head is no longer mapped), DV-011 (every OS call through
`platform.current()`), DV-012 (`O_BINARY` on every descriptor). Findings F1–F13 are decided, not pending: F4 became
DV-001 (seal hashes over the uncompressed body), F13 lives in P2-11 (launcher stall dump), and the remainder are
recorded in `docs/errors.md` with the defect that motivated each. No finding is "awaiting a word".

## Decisions taken with plan defaults (DECISIONS.md)

Twenty-five decisions, D-001 … D-025. The ones taken since the last checkpoint, each with the plan default and the
reason written beside it: **D-020…D-025** (the capture layer's network exception, the mock provider's
numbering, the `partial` marker, and seam C's unit being an event rather than a byte), **D-016/D-017/D-018** (retention prunes whole sealed segments, a tombstone pins and merges
`GAP`s, deletion is `GAP`-then-unlink), **D-019** (the calling thread never syncs a blob; the keeper syncs blob and
directory before the tape sync that covers the frame — the invariant is a test). The two knobs of `P1-02b` were the
last thing switched off in `mypy`; nothing is switched off now. Format-freeze decisions carry their `DEVIATIONS.md`
row in the same commit.

## What is next

0. **`d901811` is green on all ten cells** (`43698231222`), as are `54c3aba` (`43697929744`), `23cdb7b`
   (`43697556490`) and `846e01a` (`43697198262`) — the first all-OS green since `c7919f9`, after four commits of
   Windows red on defect 25 and one on defect 26. `STATE.last_green_commit` is `d901811`.
1. **G-Promise is signed** (`docs/gates/G-Promise.md`, verdict pass, self-signed — see §Labelled) and `STATE.json`
   carries `gates["G-Promise"] = "pass"`.
2. **G-Capture is open, and its two mechanism rows are now green**: the day-14 rule (an unmodified `openai` script
   killed mid-stream, recovered with counts that never exceed what the provider pushed) and P3-14 (a 100-call agent
   identical with and without the recorder, under three real breakages). What remains, in order: **P3-11** (the
   redaction pipeline and the 60-secret corpus, asserted absent from every sink), an **`anthropic` child** for the
   zero-code-change row, the four unmeasured §15.1 rows (Disabled, content=off, content=hash, provider-call latency
   Δ p95, startup install cost), then a reviewer's signature on `docs/gates/G-Capture.md`. **Next ticket: P3-11.**
3. **Defect 26 is open and cannot be closed here**: on Windows, `install(data_dir=<a file>)` reported `enabled: True`
   and let 100 calls run against a tape directory that cannot exist. Three questions are written down in
   `docs/defects.md`; answering them needs a Windows host, which this sandbox does not have.
4. **The capture seams each cover both client modules** (`httpx` and `httpx2`) — the published `openai` and
   `anthropic` packages are built on `httpx2`, which is why seam A reported `installed` and captured nothing until
   the day-14 test found it (defect 23, D-025). No seam, and no unit test of a seam, could have found it from the
   inside.
5. **L3 is untouched**: the attach lane (proxy, launcher integration, hooks, demo) has no commits yet and starts from
   the current trunk, not from an older SHA.
6. The independent reviewer's loop is the gate loop: a counter-signature at every gate, with the reproduce block in
   `docs/gates/` and `docs/reviews.md` as the ledger. It found C40 on its first pass and defect 25 on this one, which
   is the argument for it.
7. The 2 000-trial kill run starts in the background during L5–L8; it is not needed for L2/L3.
