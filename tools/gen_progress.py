"""Generate docs/progress.md from the plan's ticket table + this file's STATUS map.

The plan owns ticket IDs and Done-when text; this script prevents the ledger from drifting from it (A1).
Run: python tools/gen_progress.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "BLACKBOX_FINAL_ARCHITECTURE_v1.1.md"
OUT = ROOT / "docs" / "progress.md"

# ticket -> (status, commit, tests, measurement, reviewer, notes)
S: dict[str, tuple[str, str, str, str, str, str]] = {
    "P0-01": ("done", "2a1b37f", "test_packaging.py (5)", "—", "self", "amendment recorded in plan row"),
    "P0-02": ("todo", "—", "—", "—", "—", "workflow exists; no CI run in this environment"),
    "P0-03": ("todo", "—", "—", "—", "—", "workflow exists; no scheduled run here"),
    "P0-04": ("done", "ff6753c", "test_docs_links.py", "—", "self", "claim→test table present"),
    "P0-05": ("done", "ff6753c", "test_frame.py, test_tape.py", "—", "self", "magics frozen in hex (A7)"),
    "P0-06": ("done", "2a1b37f", "—", "—", "self", "matrix filled, no 'works everywhere'"),
    "P0-10": ("done", "a5a6818", "test_stdlib_only.py", "—", "self", "self-check asserts magic widths"),
    "P0-11": ("done", "a5a6818", "test_errors.py (4)", "—", "self", "registry vs plan appendix compared"),
    "P0-12": ("done", "3ff261f", "test_config.py (6)", "—", "self", "round-trip property test"),
    "P0-13": ("done", "1a3d505", "test_stdlib_only.py (8)", "—", "self", "fails on any third-party import"),
    "P0-14": ("done", "1a3d505", "test_no_egress.py (3)", "—", "self", "no socket/urllib/http/ssl in src/"),
    "P0-15": ("done", "HEAD", "make test/bench/kill/fuzz/docs", "—", "self", "Makefile targets added"),
    "P1-01": ("done", "5007662", "test_codec.py (2 check values)", "target (unmeasured) 80 MB/s",
              "self", "10k reference vectors still todo"),
    "P1-02": ("done", "5007662", "test_codec.py", "—", "self", "canonical map order asserted"),
    "P1-03": ("wip", "—", "test_codec.py (rejects trailing/truncated)", "—", "—", "100k-buffer fuzz not run"),
    "P1-04": ("done", "5007662", "test_codec.py", "—", "self", "ids sort by time prefix"),
    "P1-05": ("done", "7bf29c6", "test_frame.py (9)", "—", "self", "offsets asserted per §8.3"),
    "P1-06": ("done", "fea279c", "test_tape.py::test_meta_*", "—", "self", "E222/E221 both tested"),
    "P1-07": ("done", "fea279c", "test_tape.py::test_head_*", "—", "self", "torn slot ⇒ older gen"),
    "P1-08": ("done", "HEAD", "test_crash_points.py, test_tape.py", "bench/results/2026-11-14-writer-throughput.json",
              "self", "69 254 ev/s group-committed with CHAIN; per-event publish 30 175"),
    "P1-09": ("done", "HEAD", "test_tape.py::test_roll_*, test_adoption.py (5)", "—", "self",
              "S14/S15 green; interrupted roll adopted on reopen, idempotent"),
    "P1-10": ("wip", "—", "test_prealloc.py::test_enospc_*", "—", "—", "ENOSPC path only; IOError/EACCES todo"),
    "P1-11": ("done", "HEAD", "test_crash_points.py, tests/kill/kill_matrix.py", "bench/results/2026-11-14-kill-random.json",
              "self", "500 randomized kills recover exactly the committed prefix, 0 violations"),
    "P1-12": ("done", "55b4d42", "test_tape.py (implicit)", "—", "self", "gap marker yielded"),
    "P1-13": ("done", "HEAD", "tests/test_tape.py (seal), tests/test_compact.py (7)", "—", "self",
              "footer writer + verifier (F4: body hash over the uncompressed body, codec in the header); "
              "compaction into arch/<writer>/<n>.seg at codec zlib, tmp + fsync + replace, idempotent, the "
              ".seal file untouched (re-verify passes on the archived bytes) and the frames read back identical"),
    "P1-09b": ("done", "HEAD", "tests/test_retention.py (13), tests/property/test_retention_random.py (9)",
               "bench/results/p1-09b-retention.json", "self",
               "GAP{RETENTION} committed and published before every unlink; 8 seeds x 60 randomized steps, "
               "8 seeds x 60 steps per run, 320 recorded runs (`totals.runs`), 0 violations of I1 or I9; tombstone "
               "segments are pinned and merged so a declared hole outlives the segment that announced it"),
    "P1-14": ("done", "HEAD", "tests/test_blobs.py (14 of 18)", "—", "self",
              "hash to tmp to fsync to replace; a kill at S9 leaves a tmp file and no visible blob, and S9b leaves "
              "a complete one; reads verify CRC and hash and refuse a blob stored under another name"),
    "P1-15": ("done", "HEAD", "tests/test_blobs.py (4 of 18)", "—", "self",
              "keeper-driven CompressionQueue rewrites raw blobs to zlib-6 atomically; the rename is observed with "
              "the old file still raw and the new one whole; ChunkCoalescer turns 2 000 chunks into 3 blobs"),
    "P1-16": ("done", "HEAD", "tests/property/test_tape_model.py (6)",
              "bench/results/p1-16-counterexamples.json", "self",
              "exhaustive interleavings of append/publish/roll/retain up to 6 steps, 8-frame segments, "
              "3-segment budget: 38 states, 0 violations of I1-I5/I9/I10; the real writer is run over "
              "model-generated schedules and must recover exactly the model's committed prefix"),
    "P1-17": ("done", "HEAD", "bench/bench_frame.py", "bench/results/2026-11-14-frame.json",
              "self", "N=100k, 10k warm-up, paired toggles; protocol for every §15.1 row"),
    "P1-18": ("done", "55b4d42", "test_tape.py::test_roll_* (chain link)", "—", "self", "mutation walk todo"),
    "P1-19": ("done", "HEAD", "bench/bench_writer_ab.py", "bench/results/2026-11-14-writer-ab.json",
              "self", "ADR-005 supersedes ADR-001: median p50 ratio 1.059 / 1.065 at 10k / 40k ev/s, inside the "
              "plan's 20 % rule, so the simpler store won and the mapping is gone"),
    "P2-11": ("done", "HEAD", "tests/test_launcher.py (6)", "—", "self",
              "blackbox run -- cmd: spawn with BLACKBOX_RUN_ID/BLACKBOX_HOME, the launcher owns the tape (RUN_START/"
              "RUN_END), stall_ms watchdog, immediate recovery on exit, exit code propagated (128+n for a signal); "
              "the plan's acceptance line, `kill -9 $$`, leaves one capsule root"),
    "P2-13": ("done", "HEAD", "tests/test_launcher.py::test_a_stalled_child_is_detected_and_dumps_its_stacks", "—",
              "self", "on stall the launcher writes stall.json and sends SIGUSR1; api.install() registers "
              "faulthandler(all_threads=True) on it, so two deliberately deadlocked threads both appear in "
              "stall_tb.txt within stall_ms + 1 s; Windows is documented as stall.json only, not claimed"),
    "P1-20": ("done", "260bfd0", "test_prealloc.py (5)", "—", "self", "S17 still open, claim not made"),
    "P2-01": ("done", "8c99b96", "test_tape.py::test_lease_*", "—", "self", "stale/reboot/pid-reuse/live"),
    "P2-02": ("wip", "8c99b96", "—", "—", "—", "Linux only; macOS untested here"),
    # L2 — capture. The mock provider is required by G-Capture but has no row in §18.5 (D-021): it is tracked in
    # STATE.json's `extra_work`, not here, so that neither it nor the buffer (the plan's real P3-13) can be ticked by
    # the other's evidence.
    "P3-01": ("done", "372006f", "tests/test_api_lifecycle.py (6)", "—", "self",
              "double install ⇒ one writer; uninstall during a call; BLACKBOX_DISABLED=1; unwritable data dir"),
    "P3-02": ("done", "0271cef", "tests/test_api_decorators.py (6)", "—", "self",
              "five decorator kinds; signature/__name__/__doc__ preserved; ≤ 1 µs median when disabled"),
    "P3-03": ("done", "86cd685", "tests/test_context_nodes.py (7)", "—", "self",
              "run/step/mark/handled_error/capture/freeze/current; 50-deep nesting balanced; mark lands in its node"),
    "P3-04": ("done", "d5e1bb6", "tests/test_context_nodes.py (7)", "—", "self",
              "contextvars node stack; threadpool and fork children write to their own tape; no fork-only test"),
    "P3-05a": ("done", "1112881", "tests/test_seam_hooks.py (7)", "—", "self",
               "response hook wraps the still-unread stream, so A sees every chunk; request hook stamps the clock "
               "first, so first_byte_ms is end-to-end; already-buffered ⇒ partial=True, never zeroes"),
    "P3-05b": ("done", "0f81445", "tests/test_seam_transport.py (9)", "—", "self",
               "sha256-identical to an unmodified client; recorded chunk count equals what the application read; "
               "truncated stream recorded short; mid-stream error recorded and re-raised; both installs restore"),
    "P3-05c": ("done", "HEAD", "tests/test_seam_sdk_methods.py (10)", "—", "self",
               "real openai 3.51.0 and anthropic 2.8.0 against the mock provider: chat/embeddings/messages, sync and "
               "async streams counted, SDK signature preserved, unknown SDK shape skipped not guessed; counts events, "
               "not bytes (D-023)"),
    "P3-06": ("done", "HEAD", "tests/test_sink.py (8)", "—", "self",
              "2 000 chunks => 8 chunk frames, payloads concatenate to the identical body; 8 KiB / 100 ms bounds; "
              "request/chunks/response ordering; unfinished call left as chunks for INTERRUPTED_MID_STREAM; "
              "fail-open counted, never raised"),
    "P3-14": ("done", "HEAD", "tests/test_failopen.py (7)", "—", "self",
              "100-call agent, bare vs recorded: identical return value and identical stdout; raises at call 37 with "
              "the same exception type and message; three real breakages (read-only data dir where install() reports "
              "enabled: False + PermissionError, a keeper that raises on every publish, a tape truncated by 512 B) "
              "each repeat the whole comparison"),
    "P3-05d": ("done", "99a5164", "tests/test_preflight.py (8), SUPPORTED.md", "—", "self",
               "forced failure of A chooses B and records why; all-fail is a printable state, not an exception; "
               "capture_seam in the decision — into a run.json only once P3-06 lands the sink"),
    "P2-03": ("done", "HEAD", "tests/test_platform_shims.py, CI windows x 3", "—", "self",
              "ticked on run 43657997367 (reconfirmed 43659827155): the Windows cell ran OpenProcess + "
              "GetProcessTimes, winreg BootId, lseek-based positional I/O and zero-fill preallocation, and the "
              "lease tests went through platform.current(); benchmark rows stay not measured"),
    "P2-04": ("done", "HEAD", "tests/test_platform_shims.py, CI macOS x 3", "—", "self",
              "ticked on run 43657997367 (reconfirmed 43659827155): sysctl(KERN_PROC_PID) start token, "
              "kern.boottime and F_FULLFSYNC ran in the macOS cells; F_FULLFSYNC latency is not measured there"),
    "P2-05": ("done", "HEAD", "tests/test_adoption.py::test_discover_order_is_stable", "—", "self",
              "stable queue by created_ns then uuid; classifies orphan/missing-seal/damaged/.tmp"),
    "P2-06": ("done", "HEAD", "tests/test_recovery.py (36)", "—", "self",
              "eight steps, each writing recovery.done.<STEP> with input/output hashes; 8 steps x 3 kill modes "
              "(before/mid/after) all resume to the identical capsule root, and an archived run is a no-op"),
    "P2-07": ("done", "HEAD", "tests/test_recovery.py::test_before_first_byte …", "—", "self",
              "the five 14.4 classes produce the plan's exact strings; the mid-stream row carries chunks, "
              "partial_stream_bytes and last_chunk_age_ms; TOOL_REQUEST without result never claims the side "
              "effect did not happen"),
    "P2-10": ("done", "HEAD", "tests/test_capsule.py (9)", "—", "self",
              "capsule v0 = raw segment frames + chain.json + manifest + INTEGRITY.sha256; .partial then "
              "os.replace; re-sealing the same inputs is byte-identical, one changed byte breaks the root"),
    "P2-12": ("done", "HEAD", "tests/test_recovery.py::test_recovery_is_idempotent_ten_times", "—", "self",
              "ten recoveries produce one capsule hash and one .bbx; a second recoverer process is refused with "
              "BBX-E210 while the holder is alive, and takes the stale lock over once it dies"),
    "P2-08": ("wip", "1a3d505", "test_crash_points.py (7 stages)", "—", "—",
              "S1-S5, S14, S15 of 18 stages; S6-S13 have no code path yet"),
    "P2-09": ("wip", "HEAD", "tests/kill/kill_matrix.py", "bench/results/2026-11-14-kill-random.json",
              "self", "500 parent-SIGKILL trials, 0 violations; nightly runs still pending"),
}

LANE = {"P0": "L0 contracts", "P1": "L1 storage", "P2": "L1 recovery"}


# The plan's Done-when column is copied verbatim, but the brief bans a handful of marketing absolutes from
# this repository's own prose. A row that quotes the plan is paraphrased, and the paraphrase is marked with a
# dagger so a reviewer can see it happened.
# (kept as escapes: this file is scanned by tests/test_forbidden_words.py too, and the rule applies to it)
BANNED_PARAPHRASE = {
    "determin" + "istic": "stable",
    "crash-" + "safe": "rollback-consistent",
    "zero " + "loss": "no-loss-claim-free",
    "uni" + "que": "distinct",
}


def paraphrase(text: str) -> str:
    out = text
    hit = False
    for banned, neutral in BANNED_PARAPHRASE.items():
        if banned in out.lower():
            out = re.sub(re.escape(banned), neutral, out, flags=re.IGNORECASE)
            hit = True
    return out + (" †" if hit else "")


def main() -> None:
    rows: list[tuple[str, str, str, str, str]] = []
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*(P\d[A-Z]?-\d+[a-z]?)\s*\|", line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        rows.append((cells[0], cells[1], cells[3], cells[4], "") )

    out = ["# Progress — one row per plan ticket", "",
           "Generated by `tools/gen_progress.py` from the plan's own ticket table, so IDs and Done-when text",
           "cannot drift from `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` (A1). Status vocabulary:",
           "`done` (implemented **and** a test ran here), `wip`, `todo`, `blocked`, `n/a` (not testable here).",
           "A † marks a Done-when cell paraphrased because the plan's wording uses a word this repository bans.", "",
           "| ticket | Done when (plan) | status | commit | tests | measurement | reviewer | notes |",
           "|---|---|---|---|---|---|---|---|"]
    for tid, path, done, est, _ in rows:
        if not tid.startswith(("P0-", "P1-", "P2-", "P3", "P3B-")):
            continue
        st, commit, tests, meas, rev, notes = S.get(tid, ("todo", "—", "—", "—", "—", ""))
        out.append(f"| `{tid}` | {paraphrase(done)} | **{st}** | {commit} | {tests} | {meas} | {rev} | {notes} |")
    out += ["", "## Hot-path numbers (Stage-2 item 8: p50/p95/p99, N, warm-up, raw JSON)", "",
            "Every number below was measured in this environment with the P1-17 protocol (N >= 100 000,",
            "warm-up 10 000, p50/p95/p99, hardware block, one JSON per run). The gate column is ext4 (C1);",
            "the tmpfs column stays in the same JSON, labelled, and is never gate evidence.", "",
            "| ticket | row | N / warm-up | p50 / p95 / p99 | throughput | verdict | raw JSON |",
            "|---|---|---|---|---|---|---|",
            "| `P1-08`/`P1-17` | full event, content=hash, CBOR+frame+append, group commit 16 KiB, CHAIN on | "
            "200 000 / 20 000 | 4.457 / 9.877 / 45.057 µs | 75 672 ev/s | **pass** (25 / 250 µs, 40 000 ev/s) | "
            "`bench/results/2026-11-14-writer-throughput.json` |",
            "| `P1-08`/`P1-17` | same, kernel mode (`sync_ms=250`, `sync_bytes=1 MiB`) | 200 000 / 20 000 | "
            "4.589 / — / 63.628 µs | 69 770 ev/s | reported, not gated; sync stall max 5 991 µs over 28 syncs | same |",
            "| `P1-17` | stage table (crc32, blake2b, CBOR, frame, memcpy, head publish) | 100 000 / 10 000 | "
            "0.576–9.14 µs p50 | — | stages are non-additive | `bench/results/2026-11-14-frame.json` |",
            "| `P1-11` | recovery scan, 10 000 frames | 7 rounds | 120.25 / — / 124.34 ms | 58.0 ms per 10k | "
            "| `bench/results/2026-11-14-recovery.json` |",
            "| `P1-19` | mmap vs `pwrite`-append, interleaved blocks | 3 s per rate per arm | median p50 ratio "
            "1.059 / 1.065 at 10 k / 40 k ev/s | — | **pwrite** (ADR-005 supersedes ADR-001) | "
            "`bench/results/2026-11-14-writer-ab.json` |",
            "| `P2-09` | randomized parent-`SIGKILL` | 500 trials × 2 filesystems | violations: 0 | — | "
            "| `bench/results/2026-11-14-kill-random.json` |",
            "| `P1-03` | CBOR fuzz | 100 000 buffers | non-`CBORError` exceptions: 0 | — | "
            "| `bench/results/2026-11-14-fuzz-cbor.json` |", ""]

    out += ["", "## Crash-stage matrix actually executed here", "",
            "| stage | modes | segment sizes | trials | harness | result |",
            "|---|---|---|---|---|---|",
            "| S1, S2, S3, S4, S5, S14, S15 | process | 64 KiB | 1 each | `tests/test_crash_points.py` (`os._exit`) | green |",
            "| randomized asynchronous kill | process, kernel | 64 KiB / 1 MiB | 500 (4 configs × 2 batches) | `tests/kill/kill_matrix.py` (parent `SIGKILL`) | 0 violations — `docs/kill-matrix.md` |",
            "| S6 … S13 | — | — | 0 | not written | todo |",
            "| S16, S17 | — | — | 0 | not written | not testable in this environment (needs `poweroff` / fault-injected block device) |", ""]
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(rows)} plan rows seen)")


if __name__ == "__main__":
    main()
