<#Requires -Version 5.1>
<#
.SYNOPSIS
  Transfera one-shot installer — builds the app from source on your PC.

.DESCRIPTION
  Downloads nothing executable: every binary is compiled on this machine
  (Python venv, React build, C++ helper, Rust shell, frozen sidecar), so
  Windows SmartScreen stays quiet — locally-built files carry no
  Mark-of-the-Web. Apple drivers, the AI runtime, and all helpers ship as
  part of the same run: everything is installed from day one.

  Usage (PowerShell):
    # inside an existing checkout:
    powershell -ExecutionPolicy Bypass -File scripts\Install-Transfera.ps1
    # anywhere (clones the latest release tag for you):
    powershell -ExecutionPolicy Bypass -File Install-Transfera.ps1

.PARAMETER RepoDir
  Use an existing Transfera checkout instead of cloning.
.PARAMETER InstallDir
  Where to clone / build. Default: $env:LOCALAPPDATA\Transfera\src.
.PARAMETER Silent
  Run the produced NSIS installer silently (/S) instead of interactively.
.PARAMETER SkipNative
  Skip the MSVC Build Tools install (iPhone/WPD helper won't build;
  folder backup + Tier-1 iPhone access still work).
.PARAMETER SkipDriver
  Skip the Apple Mobile Device Support install.
.PARAMETER SkipAI
  Skip downloading the on-board AI stack (onnxruntime + tokenizers) and MobileCLIP models (~320 MB total).
.PARAMETER Yes
  Non-interactive: assume yes for the (small) confirmation prompts.
#>
[CmdletBinding()]
param(
  [string]$RepoDir = "",
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Transfera\src"),
  [switch]$Silent,
  [switch]$SkipNative,
  [switch]$SkipDriver,
  [switch]$SkipAI,
  [switch]$Yes
)

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/R1sh1m/Transfera.git"

function Step([string]$msg) {
  Write-Host ""
  Write-Host "==> $msg" -ForegroundColor Cyan
}
function StepBox([string]$icon, [string]$num, [string]$title, [string]$note = "") {
  $line = "  " + ("═" * 58)
  Write-Host ""
  Write-Host $line -ForegroundColor Cyan
  Write-Host "  ║  $icon  Step $num — $title" -ForegroundColor Cyan
  if ($note -ne "") { Write-Host "  ║      $note" -ForegroundColor DarkCyan }
  Write-Host $line -ForegroundColor Cyan
}
function Ok([string]$msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Warn([string]$msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Fail([string]$msg) {
  Write-Host "  [FAIL] $msg" -ForegroundColor Red
  exit 1
}
function Confirm-Step([string]$msg) {
  if ($Yes) { return $true }
  $ans = Read-Host "$msg [Y/n]"
  return ($ans -eq "" -or $ans -match "^[Yy]")
}
function Refresh-Path {
  # Pick up machine + user PATH changes from winget installs in this session.
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user    = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = "$machine;$user"
}
function Winget-Ensure([string]$id, [string]$name, [string]$extraArgs = "") {
  $found = winget list --id $id -e 2>$null | Select-String $id
  if ($found) { Ok "$name already installed"; return }
  Step "Installing $name (winget, may prompt for admin)..."
  $args = @("install", "-e", "--id", $id, "--accept-package-agreements", "--accept-source-agreements", "--silent")
  if ($extraArgs -ne "") { $args += $extraArgs.Split(" ") }
  & winget @args
  if ($LASTEXITCODE -ne 0) { Fail "$name install failed (exit $LASTEXITCODE). Install it manually, then re-run this script." }
  Refresh-Path
  Ok "$name installed"
}

# ── ASCII banner ──────────────────────────────────────────────────────────────
cls 2>$null
Write-Host ""
Write-Host "█████████████████████████████████████████████████████████████████████████████████████████████" -ForegroundColor Cyan
Write-Host "█        ██       ██████  █████  ███████  ███      ███        ██        ██       ██████  ████" -ForegroundColor Cyan
Write-Host "████  █████  ████  ████    ████   ██████  ██  ████  ██  ████████  ████████  ████  ████    ███" -ForegroundColor Cyan
Write-Host "████  █████  ████  ███  ██  ███    █████  ██  ████  ██  ████████  ████████  ████  ███  ██  ██" -ForegroundColor Cyan
Write-Host "████  █████  ███   ██  ████  ██  ██  ███  ███  ███████  ████████  ████████  ███   ██  ████  █" -ForegroundColor Cyan
Write-Host "████  █████      ████  ████  ██  ███  ██  █████  █████      ████      ████      ████  ████  █" -ForegroundColor Cyan
Write-Host "████  █████  ████  ██        ██  ████  █  ███████  ███  ████████  ████████  ████  ██        █" -ForegroundColor Cyan
Write-Host "████  █████  ████  ██  ████  ██  █████    ██  ████  ██  ████████  ████████  ████  ██  ████  █" -ForegroundColor Cyan
Write-Host "████  █████  ████  ██  ████  ██  ██████   ██  ████  ██  ████████  ████████  ████  ██  ████  █" -ForegroundColor Cyan
Write-Host "████  █████  ████  ██  ████  ██  ███████  ███      ███  ████████        ██  ████  ██  ████  █" -ForegroundColor Cyan
Write-Host "█████████████████████████████████████████████████████████████████████████████████████████████" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Your photos & videos. Your machine. Your rules." -ForegroundColor White
Write-Host "  Windows installer — building everything locally from source" -ForegroundColor DarkCyan
Write-Host ""


# ---------------------------------------------------------------------------
StepBox "🪟" "1/9" "Checking Windows + winget"
# ---------------------------------------------------------------------------
$os = (Get-CimInstance Win32_OperatingSystem).Version
if ([version]$os -lt [version]"10.0.0") { Fail "Windows 10 or 11 is required (found $os)." }
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
  Fail "winget not found. Install 'App Installer' from the Microsoft Store, then re-run."
}
Ok "Windows $os, winget present"

# ---------------------------------------------------------------------------
StepBox "📦" "2/9" "Toolchain: Python 3.12, Node LTS, Rust, Git"
# ---------------------------------------------------------------------------
Winget-Ensure "Python.Python.3.12" "Python 3.12"
Winget-Ensure "OpenJS.NodeJS.LTS" "Node.js LTS"
Winget-Ensure "Rustlang.Rustup" "Rust (via rustup)"
Winget-Ensure "Git.Git" "Git"
foreach ($cmd in @("py", "node", "npm", "cargo", "git")) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    Fail "'$cmd' not on PATH after install. Restart PowerShell and re-run."
  }
}
$nodeMajor = (node --version).TrimStart("v").Split(".")[0]
if ([int]$nodeMajor -lt 20) { Fail "Node.js v20+ required (found $(node --version))." }
Ok "Toolchain ready (node $(node --version), $(cargo --version))"

# ---------------------------------------------------------------------------
StepBox "📱" "3/9" "Apple Mobile Device Support (iPhone driver)"
# ---------------------------------------------------------------------------
if ($SkipDriver) {
  Warn "Skipped (-SkipDriver). iPhone access falls back to Tier-1/folder backup."
} else {
  Winget-Ensure "Apple.AppleMobileDeviceSupport" "Apple Mobile Device Support"
}

# ---------------------------------------------------------------------------
StepBox "🔧" "4/9" "MSVC Build Tools (C++ iPhone helper)" "~2-5 GB download, only needed once"
# ---------------------------------------------------------------------------
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$hasMsvc = (Test-Path $vswhere) -and (& $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null)
if ($SkipNative) {
  Warn "Skipped (-SkipNative). The WPD helper won't build; folder backup still works fully."
} elseif (-not $hasMsvc) {
  if (Confirm-Step "MSVC Build Tools (~2-5 GB) are needed for the iPhone helper. Install now?") {
    Step "Installing Visual Studio Build Tools (this takes a while)..."
    & winget install -e --id Microsoft.VisualStudio.2022.BuildTools --accept-package-agreements --accept-source-agreements --silent --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive --wait"
    if ($LASTEXITCODE -ne 0) { Warn "Build Tools install reported issues — the helper build later will confirm." }
    Refresh-Path
  } else {
    Warn "Skipped by user choice. The WPD helper won't build; folder backup still works fully."
  }
} else {
  Ok "MSVC Build Tools present"
}

# ---------------------------------------------------------------------------
StepBox "📥" "5/9" "Getting Transfera source"
if ($RepoDir -eq "" -and $PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "..\run.py"))) {
  $RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
} elseif ($RepoDir -eq "" -and (Test-Path "run.py")) {
  $RepoDir = (Get-Location).Path
}

if ($RepoDir -ne "" -and (Test-Path (Join-Path $RepoDir "run.py"))) {
  $Root = (Resolve-Path $RepoDir).Path
  Ok "Using existing checkout: $Root"
} else {
  New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
  if (Test-Path (Join-Path $InstallDir "run.py")) {
    $Root = $InstallDir
    Push-Location $Root
    git pull --ff-only 2>$null
    Pop-Location
    Ok "Using existing checkout (updated): $Root"
  } else {
    Step "Cloning latest release tag..."
    git clone --depth 1 $RepoUrl $InstallDir
    Push-Location $InstallDir
    $tag = (git tag --list "v*" --sort=-v:refname | Select-Object -First 1)
    if ($tag) { git fetch --depth 1 origin tag $tag; git checkout $tag }
    Pop-Location
    $Root = $InstallDir
    Ok "Cloned $(if ($tag) { $tag } else { 'main' }) to $Root"
  }
}

# ---------------------------------------------------------------------------
StepBox "🐍" "6/9" "Python backend (venv + all features, AI runtime & models included)"
# ---------------------------------------------------------------------------
Push-Location $Root
try {
  & py -3.12 -m venv .venv
  .\.venv\Scripts\python -m pip install --upgrade pip
  .\.venv\Scripts\python -m pip install -r backend\requirements.txt
  if ($LASTEXITCODE -ne 0) { Fail "Backend pip install failed." }

  if ($SkipAI) {
    Warn "Skipped (-SkipAI). Semantic search runtime and models not downloaded."
  } else {
    Step "Installing on-board AI stack (onnxruntime + tokenizers)..."
    .\.venv\Scripts\python -m pip install -r backend\requirements-ai.txt
    if ($LASTEXITCODE -ne 0) { Warn "AI dependencies install reported issues — continuing." }

    Step "Downloading MobileCLIP AI models (~207 MB) for Day-1 semantic search..."
    .\.venv\Scripts\python -c "from backend.engines.clip import ensure_models; ok = ensure_models(); print('AI models ready' if ok else 'Model download skipped')"
  }
} finally { Pop-Location }
Ok "Backend venv ready (FastAPI + AI runtime + models + device stack)"

# ---------------------------------------------------------------------------
StepBox "⚛️ " "7/9" "Frontend (npm ci + production build)"
# ---------------------------------------------------------------------------
Push-Location (Join-Path $Root "frontend")
try {
  npm ci
  if ($LASTEXITCODE -ne 0) { Fail "npm ci failed." }
  npm run build
  if ($LASTEXITCODE -ne 0) { Fail "Frontend build failed." }
  if (-not (Test-Path "dist\index.html")) { Fail "dist\index.html missing after build." }
} finally { Pop-Location }
Ok "Frontend built"

# ---------------------------------------------------------------------------
StepBox "🔍" "8/9" "Native helper + ExifTool + frozen sidecar"
# ---------------------------------------------------------------------------
Push-Location (Join-Path $Root "frontend")
try {
  npm run build:native
  if (-not (Test-Path "..\backend\bin\wpd_helper.exe")) {
    Warn "wpd_helper.exe not produced (MSVC missing?) — continuing without it."
  }
} finally { Pop-Location }
Push-Location $Root
try {
  .\.venv\Scripts\python -c "from backend.engines.metadata_extractor import _download_exiftool; import sys; p=_download_exiftool('backend/bin/exiftool'); sys.exit(0 if p and p.is_file() else 1)"
  if (-not (Test-Path "backend\bin\exiftool\exiftool.exe")) { Fail "ExifTool pre-seed failed." }
  .\.venv\Scripts\python -m pip install --upgrade pyinstaller
  powershell -ExecutionPolicy Bypass -File scripts\build-sidecar.ps1
  $staged = Get-ChildItem -Path frontend\src-tauri\binaries -Filter "transfera-engine-*.exe" -File -ErrorAction SilentlyContinue
  if (-not $staged) { Fail "Sidecar staging failed." }
  Copy-Item backend\bin\wpd_helper.exe frontend\src-tauri\resources\wpd_helper.exe -Force -ErrorAction SilentlyContinue
  Copy-Item backend\bin\exiftool\exiftool.exe frontend\src-tauri\resources\exiftool.exe -Force
  & frontend\src-tauri\resources\exiftool.exe -ver | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail "Single-file ExifTool smoke test failed." }
} finally { Pop-Location }
Ok "Sidecar frozen, helpers staged"

# ---------------------------------------------------------------------------
StepBox "🦀" "9/9" "Building + installing the Tauri app" "Grab a coffee ☕ — Rust compile is the slow part"
# ---------------------------------------------------------------------------
Push-Location (Join-Path $Root "frontend")
try {
  npm run tauri:build
  if ($LASTEXITCODE -ne 0) { Fail "Tauri build failed. See the Rust/NSIS output above." }
} finally { Pop-Location }
$installer = Get-ChildItem -Path (Join-Path $Root "frontend\src-tauri\target\release\bundle\nsis") -Filter "*.exe" -File -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -like "Transfera*" } | Select-Object -First 1
if (-not $installer) { Fail "No installer produced in src-tauri\target\release\bundle\nsis." }
$mb = [math]::Round($installer.Length / 1MB, 1)
Ok "Installer built: $($installer.Name) ($mb MB) — compiled locally, so no SmartScreen warning."
if ($Silent) {
  Step "Silent-installing..."
  Start-Process $installer.FullName -ArgumentList "/S" -Wait
} else {
  Step "Launching the installer (one click-through, no SmartScreen)..."
  Start-Process $installer.FullName -Wait
}
Write-Host ""
Write-Host "  ══════════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  ║                                                            ║" -ForegroundColor Green
Write-Host "  ║   ✅  ALL DONE!  Transfera is installed & ready to roll.  ║" -ForegroundColor Green
Write-Host "  ║                                                            ║" -ForegroundColor Green
Write-Host "  ║   Everything ships on day one:                             ║" -ForegroundColor Green
Write-Host "  ║     📁  Backup engine + two-hop verification               ║" -ForegroundColor Green
Write-Host "  ║     🤖  AI search  (ONNX / CPU, 100%% local)               ║" -ForegroundColor Green
Write-Host "  ║     🔍  ExifTool metadata extraction                       ║" -ForegroundColor Green
Write-Host "  ║     📱  Apple driver: $(if ($SkipDriver) { 'skipped (fallback active)' } else { 'installed          ' })          ║" -ForegroundColor Green
Write-Host "  ║     🔧  iPhone WPD helper: $(if ((Test-Path (Join-Path $Root 'backend\bin\wpd_helper.exe'))) { 'built              ' } else { 'skipped (folder OK)' })       ║" -ForegroundColor Green
Write-Host "  ║                                                            ║" -ForegroundColor Green
Write-Host "  ║   App data lives in:  %APPDATA%\Transfera                  ║" -ForegroundColor Green
Write-Host "  ║   Launch it from the Start Menu  🚀                        ║" -ForegroundColor Green
Write-Host "  ║                                                            ║" -ForegroundColor Green
Write-Host "  ══════════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
