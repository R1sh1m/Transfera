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
function Invoke-Native([ScriptBlock]$Command, [string]$ShowPattern) {
  # Run a native command, printing only output lines matching $ShowPattern.
  # Returns the exit code — callers MUST check it (`$code = ...; if ($code
  # -ne 0) { Fail ... }`) because $LASTEXITCODE does not propagate out of
  # this function scope. Use "(?!)" (never matches) to silence a command.
  # The scoped Continue is load-bearing: with $ErrorActionPreference =
  # "Stop", Windows PowerShell 5.1 turns 2>&1-merged native stderr into
  # terminating errors, so the first vite/rustc/npm warning line would
  # abort the whole installer (Step 7 died exactly this way on a vite
  # chunking warning). Every caller still gates on the returned code.
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $Command 2>&1 | Where-Object { "$_" -match $ShowPattern } | ForEach-Object { Write-Host "  $_" }
  } finally {
    $ErrorActionPreference = $prevEAP
  }
  return $LASTEXITCODE
}
function Invoke-Probe([ScriptBlock]$Command) {
  # Run a probe command that is ALLOWED to fail (version checks, feature
  # detection). Returns @{ Output = <stdout lines>; Code = <exit code> }.
  # Scoped Continue is load-bearing here too: even 2>$null-redirected
  # native stderr is terminating under Stop/5.1, so an unguarded probe
  # (e.g. an import check traceback) aborts the whole installer instead
  # of taking the fallback path. Use Invoke-Native + Fail for required
  # steps; Invoke-Probe for detection.
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & $Command 2>$null
    return @{ Output = $out; Code = $LASTEXITCODE }
  } finally {
    $ErrorActionPreference = $prevEAP
  }
}
function Confirm-Step([string]$msg) {
  if ($Yes) { return $true }
  # Headless runs (detached, stdin at EOF) make Read-Host throw or return
  # $null — which is NOT the same as pressing Enter (""). Passing $null
  # through would silently take the "no" path and misreport "skipped by
  # user choice", so detect it and say so explicitly.
  try {
    $ans = Read-Host "$msg [Y/n]"
  } catch {
    Warn "Non-interactive session (no console input) - treating prompt as skipped (pass -Yes to auto-accept)."
    return $false
  }
  if ($null -eq $ans) {
    Warn "Non-interactive session (no console input) - treating prompt as skipped (pass -Yes to auto-accept)."
    return $false
  }
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

# FIGlet Relief2 TRANSFERA banner (pure ASCII, PS 5.1-safe)
cls 2>$null
Write-Host ""
Write-Host "\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\" -ForegroundColor Cyan
Write-Host "///////// \//////// \\\/////// \\/// \\/// \\/////// \\///////// \///////// \//////// \\\/////// \\" -ForegroundColor Cyan
Write-Host "\\\/// \\\\/// \\/// \/// \\/// \///// /// \/// \\\\\\\/// \\\\\\\/// \\\\\\\/// \\/// \/// \\/// \" -ForegroundColor Cyan
Write-Host "\\\/// \\\\//////// \\///////// \///////// \\/////// \\/////// \\\/////// \\\//////// \\///////// \" -ForegroundColor Cyan
Write-Host "\\\/// \\\\/// \\/// \/// \\/// \/// ///// \\\\\\\/// \/// \\\\\\\/// \\\\\\\/// \\/// \/// \\/// \" -ForegroundColor Cyan
Write-Host "\\\/// \\\\/// \\/// \/// \\/// \/// \\/// \\/////// \\/// \\\\\\\///////// \/// \\/// \/// \\/// \" -ForegroundColor Cyan
Write-Host "\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Your photos & videos. Your machine. Your rules." -ForegroundColor White
Write-Host "  Windows installer — building everything locally from source" -ForegroundColor DarkCyan
Write-Host ""
# Prefer UTF-8 console output so box-drawing, checkmarks and emoji render
# instead of mojibake. Best-effort: legacy raster-font consoles still show
# boxes for emoji (cosmetic only — Windows Terminal renders everything).
try {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  chcp 65001 | Out-Null
} catch { }


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
$hasMsvc = $false
if (Test-Path $vswhere) {
  $vsProbe = Invoke-Probe { & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath }
  if ($vsProbe.Code -eq 0 -and "$($vsProbe.Output)".Trim() -ne "") { $hasMsvc = $true }
}
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
  # Staleness guard: installing from an outdated checkout silently ships old
  # code. Offline-safe checks only (local refs, no fetch).
  try {
    Push-Location $Root
    try {
      $headDate = git log -1 --format=%ci 2>$null
      if ($headDate) { Write-Host "  Checkout commit date: $($headDate.Trim())" }
      $upstream = git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null
      if ($upstream) {
        $behind = git rev-list --count "HEAD..$($upstream.Trim())" 2>$null
        if ($behind -match '^\d+$' -and [int]$behind.Trim() -gt 0) {
          Warn "Checkout is $($behind.Trim()) commit(s) behind $upstream - consider 'git pull' before installing."
        }
      }
      $dirtyCount = @(git status --porcelain 2>$null).Count
      if ($dirtyCount -gt 0) {
        Warn "Working tree has $dirtyCount uncommitted change(s) - the installer will ship them as-is."
      }
    } finally { Pop-Location }
  } catch { }
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
  # Reuse a healthy 3.12 venv (fast re-runs, and avoids clobbering an
  # interpreter another program has open — e.g. an IDE language server
  # locking .venv\Scripts\python.exe, which fails recreation with
  # Errno 13). Recreate only when missing, wrong version, or broken deps.
  $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
  $venvOk = $false
  if (Test-Path $venvPy) {
    $ver = Invoke-Probe { & $venvPy -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" }
    if ($ver.Code -eq 0 -and "$($ver.Output)".Trim() -eq "3.12") {
      $imp = Invoke-Probe { & $venvPy -c "import fastapi, uvicorn, sqlalchemy, aiosqlite, blake3, PIL" }
      if ($imp.Code -eq 0) { $venvOk = $true }
    }
  }
  if ($venvOk) {
    Ok "Backend venv already healthy (Python 3.12, deps present) - skipping recreate"
  } else {
    if (Test-Path (Join-Path $Root ".venv")) {
      Step "Removing stale venv (missing, wrong Python, or broken deps)..."
      try {
        Remove-Item -Recurse -Force (Join-Path $Root ".venv") -ErrorAction Stop
      } catch {
        Fail "Cannot remove stale .venv (locked by another process?). Close programs using it - e.g. an IDE language server on .venv\Scripts\python.exe - and re-run. Details: $($_.Exception.Message)"
      }
    }
    $code = Invoke-Native { & py -3.12 -m venv .venv } "(?!)"
    if ($code -ne 0) { Fail "Python venv creation failed. If another program (e.g. an IDE language server) is using .venv\Scripts\python.exe, close it and re-run." }
  }
  Step "Upgrading pip..."
  $null = Invoke-Native { .\.venv\Scripts\python -m pip install --upgrade pip -q } "(?!)"
  Step "Installing Python dependencies (fastapi, sqlalchemy, pillow, blake3 + device stack)..."
  .\.venv\Scripts\python -m pip install -r backend\requirements.txt -q
  if ($LASTEXITCODE -ne 0) { Fail "Backend pip install failed." }

  if ($SkipAI) {
    Warn "Skipped (-SkipAI). Semantic search runtime and models not downloaded."
  } else {
    Step "Installing AI runtime (onnxruntime + tokenizers)..."
    .\.venv\Scripts\python -m pip install -r backend\requirements-ai.txt -q
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
  Step "Installing npm packages..."
  $code = Invoke-Native { npm ci --silent } "(error|ERR!|warn)"
  if ($code -ne 0) { Fail "npm ci failed." }
  Step "Building React frontend (tsc + vite)..."
  $code = Invoke-Native { npm run build } "(error|ERR!|FAIL|built in)"
  if ($code -ne 0) { Fail "Frontend build failed." }
  if (-not (Test-Path "dist\index.html")) { Fail "dist\index.html missing after build." }
} finally { Pop-Location }
Ok "Frontend built"

# ---------------------------------------------------------------------------
StepBox "🔍" "8/9" "Native helper + ExifTool + frozen sidecar"
# ---------------------------------------------------------------------------
Push-Location (Join-Path $Root "frontend")
try {
  Step "Building C++ WPD helper (MSVC)..."
  $null = Invoke-Native { npm run build:native } "(BUILD SUCCESSFUL|BUILD FAILED|error|up to date)"
  if (-not (Test-Path "..\backend\bin\wpd_helper.exe")) {
    Warn "wpd_helper.exe not produced (MSVC missing?) — continuing without it."
  }
} finally { Pop-Location }

# Generate NSIS branding images (idempotent — skipped if already present)
$nsisHeader  = Join-Path $Root "frontend\src-tauri\icons\nsis-header.bmp"
$nsisSidebar = Join-Path $Root "frontend\src-tauri\icons\nsis-sidebar.bmp"
if (-not (Test-Path $nsisHeader) -or -not (Test-Path $nsisSidebar)) {
  Step "Generating NSIS installer branding images..."
  $null = Invoke-Native { .\.venv\Scripts\python -m scripts.generate-nsis-images } "(?!)"
  # Fallback: call the script file directly
  if (-not (Test-Path $nsisHeader)) {
    $null = Invoke-Native { .\.venv\Scripts\python (Join-Path $Root "scripts\generate-nsis-images.py") } "(?!)"
  }
  if (Test-Path $nsisHeader) { Ok "NSIS branding images generated" }
  else { Warn "Could not generate NSIS branding images — installer will use default logo." }
} else {
  Ok "NSIS branding images already present"
}
Push-Location $Root
try {
  # Idempotent pre-seed: re-downloading an 11 MB zip from SourceForge on
  # every run wastes time and fails the whole install when mirrors stall.
  # If the staged tree already runs, keep it.
  $exifOk = $false
  if ((Test-Path "backend\bin\exiftool\exiftool.exe") -and (Test-Path "backend\bin\exiftool\exiftool_files")) {
    $exifVer = Invoke-Probe { & backend\bin\exiftool\exiftool.exe -ver }
    if ($exifVer.Code -eq 0 -and $exifVer.Output) { $exifOk = $true }
  }
  if ($exifOk) {
    Ok "ExifTool already staged ($("$($exifVer.Output)".Trim())) — skipping download"
  } else {
    .\.venv\Scripts\python -c "from backend.engines.metadata_extractor import _download_exiftool; import sys; p=_download_exiftool('backend/bin/exiftool'); sys.exit(0 if p and p.is_file() else 1)"
  }
  if (-not (Test-Path "backend\bin\exiftool\exiftool.exe")) { Fail "ExifTool pre-seed failed (SourceForge mirrors unreachable?). Manual fix: download exiftool-13.59_64.zip from https://exiftool.org, extract exiftool.exe + exiftool_files/ into backend\bin\exiftool\, then re-run this script." }
  # Stage helpers into Tauri resources BEFORE build-sidecar: the frozen
  # smoke probe resolves ExifTool via Tier-0 (resources/), exactly like the
  # installed app — staging afterwards left the probe downloading (or
  # degraded) on a path production never takes.
  Copy-Item backend\bin\wpd_helper.exe frontend\src-tauri\resources\wpd_helper.exe -Force -ErrorAction SilentlyContinue
  Copy-Item backend\bin\exiftool\exiftool.exe frontend\src-tauri\resources\exiftool.exe -Force
  # ExifTool v13.59+ ships as a stub exe + exiftool_files/ Perl runtime tree.
  # Copy the whole tree so the launcher can resolve its runtime.
  # NOTE: the trailing \* copies the tree *contents* into the destination.
  # Without it, Copy-Item nests the container when the dest dir already
  # exists (fresh clones carry it with .gitkeep), producing
  # resources\exiftool_files\exiftool_files\... and the stub fails with
  # "Could not find ...\exiftool_files\perl5*.dll".
  if (Test-Path "backend\bin\exiftool\exiftool_files") {
    New-Item -ItemType Directory -Path frontend\src-tauri\resources\exiftool_files -Force | Out-Null
    Copy-Item backend\bin\exiftool\exiftool_files\* frontend\src-tauri\resources\exiftool_files -Recurse -Force
  }
  & frontend\src-tauri\resources\exiftool.exe -ver | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Warn "Staged ExifTool tree (frontend\src-tauri\resources\exiftool_files):"
    Get-ChildItem frontend\src-tauri\resources\exiftool_files -ErrorAction SilentlyContinue | ForEach-Object { Warn "  $($_.Name)" }
    Fail "ExifTool smoke test failed."
  }
  Step "Installing PyInstaller..."
  $null = Invoke-Native { .\.venv\Scripts\python -m pip install --upgrade pyinstaller -q } "(?!)"
  Step "Freezing Python backend into sidecar (~200 MB, one-time)..."
  $code = Invoke-Native { powershell -ExecutionPolicy Bypass -File scripts\build-sidecar.ps1 } "(INFO: Build complete|WARNING|ERROR|Sidecar staged|exe:|Smoke-testing|smoke test)"
  # build-sidecar.ps1 relays its own failures via exit code — but only the
  # filtered stream is shown above, so check explicitly: the staging files
  # below EXIST even when the smoke gate fails (staging precedes probing).
  if ($LASTEXITCODE -ne 0) { Fail "Sidecar build or frozen-engine smoke test failed (see output above)." }
  # The one-dir bundle must be staged whole (exe + _internal/ runtime) —
  # the exe alone cannot start the installed engine.
  $engineExe = "frontend\src-tauri\resources\transfera-engine\transfera-engine.exe"
  $engineInternal = "frontend\src-tauri\resources\transfera-engine\_internal"
  $engineFiles = @(Get-ChildItem -Path $engineInternal -Recurse -File -ErrorAction SilentlyContinue).Count
  if (-not (Test-Path $engineExe) -or $engineFiles -lt 10) { Fail "Sidecar staging failed (engine exe or _internal runtime missing)." }
} finally { Pop-Location }
Ok "Sidecar frozen, helpers staged"

# ---------------------------------------------------------------------------
StepBox "🦀" "9/9" "Building + installing the Tauri app" "Grab a coffee ☕ — Rust compile is the slow part"
# ---------------------------------------------------------------------------
Push-Location (Join-Path $Root "frontend")
try {
  Step "Compiling Rust shell + bundling NSIS installer (this takes ~5 min)..."
  $code = Invoke-Native { npm run tauri:build } "(Compiling transfera |Finished |Built application|Running makensis|Finished \d|error\[|^error:)"
  if ($code -ne 0) { Fail "Tauri build failed. See the Rust/NSIS output above." }
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
  Write-Host "  Complete the installer window to continue (check the taskbar if it opened behind this window)..." -ForegroundColor DarkCyan
  $installProc = Start-Process $installer.FullName -PassThru -Wait
  if ($installProc.ExitCode -ne 0) {
    Fail "Installer exited with code $($installProc.ExitCode) (cancelled?). Re-run this script to retry."
  }
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
Write-Host "  ║   App data lives in:  %APPDATA%\com.transfera.app          ║" -ForegroundColor Green
Write-Host "  ║   Launch it from the Start Menu  🚀                        ║" -ForegroundColor Green
Write-Host "  ║                                                            ║" -ForegroundColor Green
Write-Host "  ══════════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
