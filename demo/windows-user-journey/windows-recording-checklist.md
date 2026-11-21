# Windows 10 / 11 Recording Checklist (Missing Shots)

This sandbox runs headless Linux, so the real Windows graphical shots below could not be captured here. Record these on a real Windows 10 or Windows 11 desktop at 1920×1080 (with large, readable font in PowerShell):

1. **GitHub Actions download**: Browser showing `package-windows` run `44885626025` (`72398c8`) and downloading `blackbox-windows-installer`.
2. **Extract & SmartScreen**: Extracting `BlackBox-Setup-0.1.0.dev0.exe` and showing the unsigned-publisher / SmartScreen prompt (if shown), explaining the trust decision.
3. **Installer wizard**: Stepping through the wizard and finishing installation.
4. **First-run UI**: Launching BlackBox from the Start menu / installer and showing the setup screen.
5. **PowerShell version check**: Typing `$bb = "$env:LOCALAPPDATA\Programs\BlackBox\python\python.exe"` and `& $bb -m blackbox --version`.
6. **Mock test run**: Creating `$env:USERPROFILE\BlackBox-Test`, running `& $bb .\mock_test.py`, showing `Mock replied: Hello! This is a local mock response.` and the BlackBox run summary.
7. **List & verify**: Running `& $bb -m blackbox ls` and `& $bb -m blackbox verify capsules\*.bbx`.
8. **Desktop UI**: Running `& $bb -m blackbox desktop` from `BlackBox-Test`, opening the recording, clicking **Check this capsule**, **Diagnose**, and saving/reading the repair brief.
9. **Record metadata**: Note the exact Windows version (`winver`) and the SHA-256 of `BlackBox-Setup-0.1.0.dev0.exe` (`Get-FileHash BlackBox-Setup-0.1.0.dev0.exe -Algorithm SHA256`).
