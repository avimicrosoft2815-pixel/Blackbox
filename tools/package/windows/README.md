# Windows packaging — staged here, built and tested on Windows

This directory carries everything a Windows build needs, and it is honest about what has not happened: **no Windows
installer has been produced or run in the build environment that wrote this** (a Linux container with no Windows, no
`wine` and no Inno Setup). The Windows desktop journey is therefore *unverified*, and the release gate for Windows stays
open until somebody does it.

## Build it, on Windows

```powershell
powershell -ExecutionPolicy Bypass -File tools\package\windows\build_windows.ps1
iscc tools\package\windows\blackbox.iss /DAppVersion=0.1.0.dev0
```

`build_windows.ps1` downloads the Windows embeddable Python 3.11, the package wheel and `pywebview`/`pythonnet`, installs
them into `win64\python\`, and writes the launcher. `blackbox.iss` then produces `dist\BlackBox-Setup-<version>.exe`.

Requirements on the build machine: Windows 10/11 x64, PowerShell 5+, [Inno Setup 6](https://jrsoftware.org/isdl.php)
(`iscc` on `PATH`), network access for the downloads, and a `.ico` at `tools\package\windows\blackbox.ico` (convert
`debian/blackbox.svg`, or drop in the owner's finished icon).

## What the installer does

* installs to `%LOCALAPPDATA%\Programs\BlackBox` by default (`PrivilegesRequired=lowest`, no UAC prompt);
* Start Menu entry, optional desktop shortcut, and a `.bbx` file association under `HKCU`;
* an uninstaller that removes the program **and keeps the recordings**, saying where they are;
* a first page that states the privacy position in one sentence.

## What is still needed before this can be called released

| Item | Who | Why it cannot be done here |
|---|---|---|
| Build the installer | a person on Windows (or a Windows CI runner with Inno Setup) | no Windows and no Inno Setup in this environment |
| Install → launch → record → open → uninstall on Windows 10 and 11 | a person or a real Windows VM | there is no Windows machine here; this is the gate row, not a formality |
| Authenticode signature | the owner's certificate | signing needs a key that is not in this environment; unsigned builds show SmartScreen warnings |
| Windows desktop screenshots/notes for the docs | whoever runs the journey | would otherwise be invented, which this project does not do |

When those exist, update `docs/desktop.md`'s platform matrix from *not run* to the observed result, with the Windows
version and the installer hash.
