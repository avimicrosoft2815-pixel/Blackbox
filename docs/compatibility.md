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

Last update: run [`43647132716`](https://github.com/avimicrosoft2815-pixel/Blackbox/actions/runs/43647132716)
(commit `cd85a7f`), read with `gh run view` because the job logs are outside this sandbox's egress.

| OS | 3.11 | 3.12 | 3.13 | Evidence |
|---|---|---|---|---|
| ubuntu-latest (Linux 6.x, x86-64) | **green** | **green** | **green** | `gh run view 43647132716` — three cells completed successfully |
| macOS (arm64 runner) | **red** | **red** | **red** | same run; the job exits 1 in ~20 s, reason not readable from here. C22's known cause (`tested_here` hard-coded) is fixed; a second layer remains |
| windows-latest | **red** | **red** | **red** | same run; exit 1 in ~33 s. `os.pwrite`/`os.pread` were absent and are now behind the platform layer; a second layer remains (`os.replace` over an open handle is the usual next one) |
| docs job | green | — | — | `ci.yml` docs job, `tests/test_docs_links.py` |

Consequences for the gates, stated plainly:

* **P2-03 / P2-04 are NOT ticked.** Their shims exist and their contract tests pass on Linux, but a cell has to
  be green before an OS gate moves off `not testable in this environment`.
* Every Linux test gate is `passed (CI, run 43647132716)` **and** passed here.
* Benchmark rows: the §15.1 numbers in `docs/STATUS.md` are from this machine only (`requires hardware`), never
  from a runner.

