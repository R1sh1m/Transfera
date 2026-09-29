# Transfera v2 — build the frozen Python sidecar for the Tauri shell.
# Usage (from repo root):  powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1
#
# Steps: ensure .venv (Python 3.12) → pip install requirements.txt +
# pyinstaller → pyinstaller transfera-engine.spec → stage the one-dir exe as
# frontend/src-tauri/binaries/transfera-engine-<triple>.exe (Tauri
# externalBin layout; triple matches the Rust target so `cargo check` and
# `tauri build` resolve the binary).

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$OutDir = Join-Path $Root "frontend\src-tauri\binaries"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Missing .venv Python at $VenvPython — run 'python run.py' once first (it creates the 3.12 venv)."
    exit 1
}

& $VenvPython -m pip install --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { Write-Error "pyinstaller install failed"; exit 1 }

Push-Location $Root
try {
    & $VenvPython -m PyInstaller --noconfirm transfera-engine.spec
    if ($LASTEXITCODE -ne 0) { Write-Error "pyinstaller build failed"; exit 1 }
} finally {
    Pop-Location
}

$Built = Join-Path $Root "dist\transfera-engine\transfera-engine.exe"
if (-not (Test-Path $Built)) { Write-Error "Expected output missing: $Built"; exit 1 }

$Triple = "x86_64-pc-windows-msvc"
try {
    $Host_ = (rustc -vV | Where-Object { $_ -match "^host:" }) -replace "^host:\s*", ""
    if ($Host_) { $Triple = $Host_.Trim() }
} catch { }

New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$Dest = Join-Path $OutDir "transfera-engine-$Triple.exe"
Copy-Item $Built $Dest -Force
Write-Host "Sidecar staged: $Dest"
# Tauri's NSIS bundler on Windows resolves externalBin to the msvc triple
# even when the active Rust toolchain is gnu (frozen PyInstaller exe is
# toolchain-agnostic), so always stage the sibling triple too. Otherwise
# `tauri build` compiles fine for 9+ min then fails at bundle time with
# "resource path binaries\transfera-engine-x86_64-pc-windows-msvc.exe doesn't exist".
foreach ($AltTriple in @("x86_64-pc-windows-msvc", "x86_64-pc-windows-gnu")) {
    if ($AltTriple -ne $Triple) {
        $AltDest = Join-Path $OutDir "transfera-engine-$AltTriple.exe"
        Copy-Item $Built $AltDest -Force
        Write-Host "Sidecar staged (alias): $AltDest"
    }
}
$SizeMB = [math]::Round((Get-Item $Dest).Length / 1MB, 1)
$DirMB = [math]::Round(((Get-ChildItem (Join-Path $Root "dist\transfera-engine") -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "exe: ${SizeMB} MB, one-dir total: ${DirMB} MB"
