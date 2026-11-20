# Stage a self-contained Windows install tree for the Inno Setup definition beside this file.
#
#   powershell -ExecutionPolicy Bypass -File tools\package\windows\build_windows.ps1
#   iscc tools\package\windows\blackbox.iss /DAppVersion=0.1.0.dev0
#
# Run this ON A WINDOWS MACHINE. It downloads the Windows embeddable Python (3.11, from python.org) and the package's
# wheels into `win64\`, so the installer needs no Python on the user's machine and no network at install time.
#
# It has NOT been run in the build environment that wrote it: that environment is Linux, with no Windows, no wine and no
# Inno Setup. Nothing in this file is evidence that the Windows desktop app works; `docs/desktop.md` says so in the
# platform matrix and the release gate for Windows stays open until a person does the journey on Windows.
$ErrorActionPreference = 'Stop'

# Native commands do not throw in Windows PowerShell 5.1 when they fail: `$ErrorActionPreference` only covers cmdlets.
# The first CI run of this script staged a tree whose pip install had failed and said "5/5 done" anyway, and the only
# symptom was a `ModuleNotFoundError` three steps later. Every external command below goes through this.
function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What exited $LASTEXITCODE"
    }
}

$root = Resolve-Path (Join-Path $PSScriptRoot '..\..\..')
$stage = Join-Path $PSScriptRoot 'win64'
$py = Join-Path $stage 'python'
$pydir = Join-Path $py 'python-3.11.9-embed-amd64'

New-Item -ItemType Directory -Force -Path $stage | Out-Null
if (Test-Path $py) { Remove-Item -Recurse -Force $py }
New-Item -ItemType Directory -Force -Path $py | Out-Null

Write-Host '== 1/5 embeddable Python 3.11'
$zip = Join-Path $env:TEMP 'python-3.11.9-embed-amd64.zip'
Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile $zip
Expand-Archive -Path $zip -DestinationPath $py -Force

Write-Host '== 2/5 enable site-packages in the embedded interpreter'
# The embeddable distribution ships a python311._pth that disables `import site`; without this line `pip` cannot install.
$pth = Get-ChildItem -Path $py -Filter 'python311._pth' | Select-Object -First 1
(Get-Content $pth.FullName) -replace '^#\s*import site$', 'import site' | Set-Content $pth.FullName

Write-Host '== 3/5 wheels: this package + the desktop window'
$wheelDir = Join-Path $env:TEMP 'blackbox-wheels'
New-Item -ItemType Directory -Force -Path $wheelDir | Out-Null
Invoke-Checked 'pip wheel (this package)' { python -m pip wheel $root --no-deps -w $wheelDir }
Invoke-Checked 'pip download (pywebview, pythonnet)' { python -m pip download --only-binary=:all: pywebview pythonnet -d $wheelDir }
if (-not (Get-ChildItem $wheelDir -Filter '*.whl')) { throw "no wheels landed in $wheelDir" }

Write-Host '== 4/5 install into the embedded interpreter'
Invoke-Checked 'ensurepip' { & "$py\python.exe" -m ensurepip }
Invoke-Checked 'pip install into the embedded interpreter' {
    & "$py\python.exe" -m pip install --no-index --find-links $wheelDir --target "$py\Lib\site-packages" blackbox-recorder pywebview
}
# Say what the staged tree can actually import, in the script's own words, before anything claims "done".
& "$py\python.exe" -c "import sys; sys.path.insert(0, r'$py\Lib\site-packages'); import blackbox; print('staged import:', blackbox.__version__)"
if ($LASTEXITCODE -ne 0) { throw "the staged tree cannot import blackbox" }

Write-Host '== 5/5 launcher'
# A .cmd whose interpreter is `pythonw.exe`: pythonw is the GUI-subsystem build, so no console window appears beside the
# app. Its stderr goes to a log, because a silent window that fails to open is the worst support case there is; on a
# non-zero exit the launcher prints where the log is and waits, rather than vanishing.
@'
@echo off
setlocal
rem BlackBox launcher. The window needs WebView2, which ships with Windows 10 and 11.
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "BBX_HOME=%LOCALAPPDATA%\BlackBox"
if not exist "%BBX_HOME%" mkdir "%BBX_HOME%" >nul 2>&1
set "BBX_LOG=%BBX_HOME%\launcher.log"
"%~dp0python\pythonw.exe" -c "import sys; sys.path[:0] = [r'%~dp0python\Lib\site-packages']; from blackbox.__main__ import main; raise SystemExit(main(sys.argv[1:]))" %* 2>>"%BBX_LOG%"
if errorlevel 1 (
  echo BlackBox could not start, or exited with an error. 1>&2
  echo The log is: "%BBX_LOG%" 1>&2
  if exist "%BBX_LOG%" (
    echo ---- last lines ---- 1>&2
    powershell -NoProfile -Command "Get-Content -Tail 20 -LiteralPath '%BBX_LOG%'" 1>&2
  )
  pause
)
'@ | Set-Content -Encoding ASCII (Join-Path $stage 'blackbox.cmd')

Get-ChildItem $py | Select-Object -First 12 | ForEach-Object { Write-Host "  $($_.Name)" }
Get-ChildItem (Join-Path $py 'Lib\site-packages') -ErrorAction SilentlyContinue | Select-Object -First 12 |
    ForEach-Object { Write-Host "  site-packages/$($_.Name)" }
Write-Host "staged: $stage"
Write-Host "next:   iscc tools\package\windows\blackbox.iss /DAppVersion=$(python -c "import tomllib;print(tomllib.load(open(r'$(Join-Path $root 'pyproject.toml')','rb'))['project']['version'])")"
