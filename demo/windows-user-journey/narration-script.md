# Narration Script — BlackBox Windows User Journey

> **Status:** Complete timed script. Spoken audio has not been rendered yet because real Windows desktop footage is not available in this Linux sandbox.

## 00:00 – 00:25 | 1. Finding and downloading the Windows installer
"In this walkthrough, we'll install BlackBox on Windows from scratch, record a safe local mock application, and inspect the recording in the BlackBox desktop window. First, open the `package-windows` GitHub Actions run for commit `72398c8` and download the `blackbox-windows-installer` artifact at the bottom of the page."

## 00:25 – 00:55 | 2–3. Extracting, Windows SmartScreen warning, and wizard
"Extract the ZIP file and run `BlackBox-Setup-0.1.0.dev0.exe`. Because this preview installer is unsigned, Windows SmartScreen may show an unknown-publisher warning. Only proceed if you trust this build and are comfortable running an unsigned internal preview. Follow the installation wizard to finish setup."

## 00:55 – 01:25 | 4–5. Launching BlackBox and first-run UI
"Launch BlackBox from the Start menu or the installer's launch option. On first run, BlackBox opens the setup view when no recordings exist in the current directory, showing what it records, what it never records, and how to point an app at the local proxy."

## 01:25 – 01:55 | 6. Opening PowerShell and checking the install
"Open PowerShell from the Start menu. Set `$bb` to the installed Python executable under `$env:LOCALAPPDATA\Programs\BlackBox\python\python.exe` and run `& $bb -m blackbox --version` to confirm the installed build responds."

## 01:55 – 02:35 | 7–9. Creating and running the local mock test
"Create a dedicated `BlackBox-Test` folder so we don't touch any personal projects. Our `mock_test.py` starts a local HTTP server bound only to `127.0.0.1`, writes `mock_client.py` to send an OpenAI-shaped request, and runs it under `blackbox run`. No real OpenAI request is made and no API key is needed — the reply comes from our local mock."

## 02:35 – 03:05 | 10. Listing and verifying the recordings
"Run `& $bb -m blackbox ls` from the same folder — BlackBox's default data directory is the current working directory. You'll see two capsules from the run: one for the proxy tape and one for the SDK tape. Running `blackbox verify` recomputes the integrity hashes, while noting that `capture_vantage` is declared rather than recomputable from the capsule alone."

## 03:05 – 03:50 | 11–12. Opening the desktop window, Check, Diagnose, and Brief
"Now run `& $bb -m blackbox desktop` from `BlackBox-Test`. The library lists the new recording. Open it to inspect the run, click **Check this capsule** to verify it, and open **Diagnose** to view the read-only incident report. You can also save a repair brief (`repair-brief.md`) — sharing it with a coding assistant is always your decision."

## 03:50 – 04:15 | 13. Summary
"We downloaded the unsigned Windows preview, verified the CLI, recorded a loopback-only mock OpenAI-shaped request, and inspected the resulting capsule and diagnosis in the BlackBox UI. No external provider was called, and no automated repair was applied."
