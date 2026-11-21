# BlackBox Windows User-Journey Demo

A beginner-friendly walkthrough of installing BlackBox on Windows, recording a local loopback-only mock application, and inspecting the resulting capsule and diagnosis in the BlackBox UI.

## Pinned Build & Installer

- **Repository commit:** `72398c8` (`main`)
- **Working branch:** `arena/9ec6858b-blackbox`
- **Installer source:** GitHub Actions `package-windows` run `44885626025` (`completed`, `success`), artifact `blackbox-windows-installer` (12,416,765 bytes)
- **Installer SHA-256:** `pending-windows-capture` (compute on Windows with `Get-FileHash BlackBox-Setup-0.1.0.dev0.exe -Algorithm SHA256`)
- **Windows version:** `pending-windows-capture` (target: Windows 10 or Windows 11)

## What Was Actually Tested Here

Executed in a clean temporary directory (`/tmp/bbx-demo-test`) against commit `72398c8` on Linux:
- `mock_test.py` ran cleanly (`exit 0`), printing `Mock replied: Hello! This is a local mock response.`
- `blackbox run` bound its proxy to `127.0.0.1` and recovered 2 capsules (`c_55b1ef98d9d353e5.bbx` proxy tape with 1 model call / 14 events, and `c_019371a741cc25cc.bbx` SDK tape with 0 model calls / 5 events).
- `blackbox ls`, `blackbox verify capsules/*.bbx` (`2 capsule(s) verified; 1 check(s) declared, not checkable from a capsule`), and `blackbox diagnose capsules/ --brief repair-brief.md` (wrote 3,621 chars) all succeeded.
- **Loopback confirmation:** `MockModel` bound to `("127.0.0.1", 0)`, `mock_client.py` asserted `urlparse(base).hostname in ("127.0.0.1", "localhost")`, and `OPENAI_API_KEY` was unset. No external provider was called.

## What Is Missing / Not Captured Here

- **Real Windows desktop footage & final MP4:** This environment is a headless Linux sandbox (`ffmpeg` and a Windows 10/11 desktop are not available). No synthetic Windows UI was fabricated.
- **Spoken narration audio:** `narration-script.md` and `captions.vtt` are complete and timed, awaiting voice rendering alongside the real Windows capture.
- See `windows-recording-checklist.md` for the exact shots to capture on Windows 10/11.

## Files in This Directory

- `mock_test.py` — self-contained loopback mock server + `mock_client.py` + `blackbox run` harness
- `commands.md` — exact copyable PowerShell commands
- `narration-script.md` — timed spoken narration script
- `captions.vtt` — WebVTT captions
- `windows-recording-checklist.md` — Windows 10/11 capture checklist
- `README.md` — this file
