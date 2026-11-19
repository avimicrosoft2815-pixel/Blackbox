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
& python -m pip wheel $root --no-deps -w $wheelDir
& python -m pip download --only-binary=:all: pywebview pythonnet -d $wheelDir

Write-Host '== 4/5 install into the embedded interpreter'
& "$py\python.exe" -m ensurepip
& "$py\python.exe" -m pip install --no-index --find-links $wheelDir --target "$py\Lib\site-packages" blackbox-recorder pywebview

Write-Host '== 5/5 launcher'
@'
@echo off
rem BlackBox launcher. The window needs WebView2, which ships with Windows 10/11.
"%~dp0python\python.exe" -c "import sys; sys.path[:0] = [r'%~dp0python\Lib\site-packages']; from blackbox.__main__ import main; raise SystemExit(main(sys.argv[1:]))" %*
'@ | Set-Content -Encoding ASCII (Join-Path $stage 'blackbox.cmd')

Write-Host "staged: $stage"
Write-Host "next:   iscc tools\package\windows\blackbox.iss /DAppVersion=$(python -c "import tomllib;print(tomllib.load(open(r'$(Join-Path $root 'pyproject.toml')','rb'))['project']['version'])")"
