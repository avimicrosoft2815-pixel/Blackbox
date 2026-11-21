# Copyable Commands — BlackBox Windows User Journey

All commands below were verified against commit `72398c8` (with the Python/CLI commands executed in a clean temporary directory on Linux; the Windows executable path `$env:LOCALAPPDATA\Programs\BlackBox\python\python.exe` is the path installed by `BlackBox-Setup-0.1.0.dev0.exe`).

> **Note on data directory:** BlackBox's default data directory is the **current working directory**, not `~/.blackbox`. Run `ls`, `verify`, `diagnose`, and `desktop` from the same folder where you ran `blackbox run`, or pass `--data` (for `ls`/`verify`) or the capsule path (for `diagnose`/`view`).

## 1. Check the installed Windows build

```powershell
$bb = "$env:LOCALAPPDATA\Programs\BlackBox\python\python.exe"
& $bb -m blackbox --version
```

## 2. Create the dedicated test folder and mock test

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\BlackBox-Test" | Out-Null
Set-Location "$env:USERPROFILE\BlackBox-Test"
```

Copy `mock_test.py` from this directory into `$env:USERPROFILE\BlackBox-Test\mock_test.py` and run:

```powershell
& $bb .\mock_test.py
```

## 3. List and verify the recordings

```powershell
& $bb -m blackbox ls
& $bb -m blackbox verify capsules\*.bbx
```

## 4. Open the BlackBox desktop window

```powershell
& $bb -m blackbox desktop
```

## 5. View diagnosis and save a repair brief (CLI)

```powershell
& $bb -m blackbox diagnose capsules\ --brief repair-brief.md
```
