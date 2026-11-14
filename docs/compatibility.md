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
