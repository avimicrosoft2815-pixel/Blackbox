# Compatibility (declared, tested, best-effort)

Rule: a version is listed here only if it is tested in CI. Everything else is **best-effort** and must
report `not testable in this environment`, never "supported".

## Python

| Version | Status | Where it is proven |
|---|---|---|
| 3.10 | **excluded** — end-of-life October 2026; `tomllib` needed for zero-dependency config parsing is 3.11+ | n/a (`requires-python = ">=3.11"`) |
| 3.11 | supported (floor) | this environment (3.11.2) + CI `ubuntu` cell |
| 3.12 | supported (declared) | CI cells only — **not run in this environment** |
| 3.13 | supported (declared) | CI cells only — **not run in this environment** |
| 3.14 | best-effort | not tested anywhere yet |

## Operating systems

| OS | Status | Where it is proven |
|---|---|---|
| Linux (x86_64, ext4) | supported | this environment + CI `ubuntu-latest` |
| Linux (aarch64) | best-effort | not tested |
| macOS (arm64) | declared | CI `macos-latest` only — **not run in this environment** |
| Windows (x64) | declared | CI `windows-latest` only — **not run in this environment** |
| Docker / overlayfs | declared | not tested |

## Filesystems

| Filesystem | Status | Note |
|---|---|---|
| ext4 | supported | primary target; probed here |
| xfs / btrfs / zfs | best-effort | rename atomicity and fsync semantics untested |
| APFS | declared | `F_FULLFSYNC` path required; **not testable in this environment** |
| NTFS | declared | `FlushFileBuffers` path; **not testable in this environment** |
| tmpfs | best-effort | no machine-failure guarantee; F1 only |
| NFS / SMB / 9p / sshfs | **refused** | `BBX-E120`; mmap coherence and rename atomicity are not guaranteed |

## Provider SDKs

Not yet applicable — no capture layer exists (phase P3). The declared matrix lands with P3-05 and `SUPPORTED.md`.
| macOS (APFS) | **written, never executed** | `blackbox.platform.macos`: preallocation is `zero-fill` (no `F_PREALLOCATE` in stdlib, so a sparse file is never mapped), `fsync(dir)` is a documented no-op, and `os.fsync` does not flush the drive cache — F2 is weaker than on Linux until measured | `tests/test_platform_shims.py` (contract only) | not testable here |
| Windows (NTFS) | **written, never executed** | `blackbox.platform.windows`: preallocation is `zero-fill`, no directory fsync, no process-start token (the lease falls back to a mtime staleness check instead of trusting a PID) | `tests/test_platform_shims.py` (contract only) | not testable here |

## CI evidence — OS × Python, rewritten on every STATUS (C23)

Cell values are the **test** verdict only. No benchmark number in this repository comes from a CI runner: those
rows stay `smoke only` on a runner and `not measured` for an OS that has not run the benchmark.

Last update: run [`43656191825`](https://github.com/avimicrosoft2815-pixel/Blackbox/actions/runs/43656191825)
(commit `9960fc5`-era). Since C27's follow-up every failing test is emitted as a `::error::` annotation, so
`gh run view <id>` prints **which tests failed** — no job log, no login, no guessing.

| OS | 3.11 | 3.12 | 3.13 | Evidence |
|---|---|---|---|---|
| ubuntu-latest (Linux 6.x, x86-64) | **green** | **green** | **green** | `gh run view 43647132716` — three cells completed successfully |
| macOS (arm64 runner) | **green** | **green** | **green** | `gh run view 43656191825` — all three cells completed successfully (C25/C26/C28 fixed this) |
| windows-latest | **red** | **red** | **red** | exit 1 in ~30 s; **the failing tests are named by the annotations**, see below |
| docs job | green | — | — | `ci.yml` docs job, `tests/test_docs_links.py` |

Consequences for the gates, stated plainly:

* **P2-03 / P2-04 are NOT ticked.** Their shims exist and their contract tests pass on Linux, but a cell has to
  be green before an OS gate moves off `not testable in this environment`.
* Every Linux test gate is `passed (CI, run 43647132716)` **and** passed here.
* Benchmark rows: the §15.1 numbers in `docs/STATUS.md` are from this machine only (`requires hardware`), never
  from a runner.


### Windows: the failing tests, named (run `43656191825`)

Eight of them, all in two files, and all with the same signature — the tape the child wrote is reported empty:

```
FAILED tests/test_crash_points.py::test_s5_after_head_publish_the_commit_survives        {'completeness': 'empty', 'frames': 0, ...}
FAILED tests/test_crash_points.py::test_s4_torn_head_slot_falls_back_to_previous_commit   {'completeness': 'empty', 'frames': 0, ...}
FAILED tests/test_crash_points.py::test_s14_after_seg_end_before_next_segment             {'completeness': 'empty', 'frames': 0, ...}
FAILED tests/test_crash_points.py::test_s15_next_segment_durable_but_head_still_names_the_old_one
FAILED tests/test_crash_points.py::test_torn_frame_is_never_committed[S1|S2|S3]
FAILED tests/test_adoption.py::test_adoption_does_not_require_a_seg_end_frame             AttributeError: 'NoneType' has no attribute 'active_segment'
FAILED tests/test_adoption.py::test_append_after_adoption_keeps_seq_dense                IndexError: list index out of range
```

`frames: 0` while the directory exists means the child's frames are not visible to the parent's scanner: the
frame went into the writer's batch buffer and **the child died before the batch was written**, or the head is
unreadable on the parent side. The stage child is spawned by `subprocess.run` with `BLACKBOX_DIE_AT`, so on
Windows the two candidates are (a) the batch buffer not being flushed on the `os._exit` path — the `S1`/`S2`/`S3`
stages die *inside* `emit` before `publish`, which is correct behaviour and must be asserted differently on a
platform where the child cannot be relied on to reach the same instruction, and (b) `_child.py` dying during
import on Windows. This is the next ticket, with the named tests as its acceptance: the same eight ids green in
an annotation-free run.

### Windows: the cause, named (C29)

Every `os.open()` in `src/` produced a **text-mode CRT descriptor** on Windows, because `O_BINARY` was in the
flags nowhere in the tree. `open()` and `tempfile` add the flag themselves; a raw `os.open()` does not. On the
`lseek`+`read`/`write` path that meant every `0x0A` the writer emitted became `0x0D 0x0A`, every `0x0D 0x0A`
read back collapsed to `0x0A`, and the first `0x1A` byte read was treated as end of file — which is exactly the
run-`43656494686` failure set: `BBX-E224 segment header checksum mismatch`, `body does not match the footer`,
`completeness: empty`, `assert 0 == 22`, adoption finding no header. Not flush ordering, not short reads (both
earlier guesses were wrong).

Fixed in one commit: `platform.open_fd` ORs `O_BINARY` into every tape descriptor, the dispatch test forbids a
bare `os.open` outside the platform layer, and `test_positional_io_is_byte_exact_for_all_256_values` writes all
256 byte values through `pwrite`/`pread` and demands byte identity — it fails on any OS whose `open_fd` forgets
the flag.

`test_sparse_detection` is skipped on Windows for a different reason and it is not a measurement gap: on NTFS,
`SetEndOfFile` (what `os.ftruncate` becomes) **allocates** the clusters, and a file is sparse only after
`FSCTL_SET_SPARSE`. The test's premise is false there, so `prealloc.is_sparse` documents that Windows returns
`False` because the file really is dense — which is what S17 wants.
