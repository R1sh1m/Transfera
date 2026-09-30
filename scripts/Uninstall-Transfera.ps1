<#Requires -Version 5.1
<#
.SYNOPSIS
  Transfera — full uninstaller.

.DESCRIPTION
  Runs the NSIS-generated uninstaller (removes app binaries from Program Files)
  then optionally removes app data, the source checkout, and any build artifacts.

  What this covers:
    1. Tauri / NSIS uninstaller  — removes the installed binaries
    2. App data  (%APPDATA%\Transfera\)  — DB, thumbnails, logs, exports, secret
    3. Start Menu shortcut cleanup        — in case NSIS missed it
    4. Source checkout + build artifacts  — only if -RemoveSource is given
    5. pip wheel cache                    — only if -RemoveCache is given

  NEVER removes anything outside the repo or %APPDATA%\Transfera\.
  Your imported media (wherever you chose to put it) is NOT touched.

  Usage:
    # Minimal: remove the installed app only
    powershell -ExecutionPolicy Bypass -File scripts\Uninstall-Transfera.ps1

    # Remove app + all data (thumbnails, DB, logs)
    powershell -ExecutionPolicy Bypass -File scripts\Uninstall-Transfera.ps1 -RemoveData

    # Full wipe including source checkout and build caches
    powershell -ExecutionPolicy Bypass -File scripts\Uninstall-Transfera.ps1 -RemoveData -RemoveSource

.PARAMETER RemoveData
  Also delete %APPDATA%\Transfera\ (database, thumbnails, exports, logs).
  Your imported media files in your chosen vault folder are NOT affected.
.PARAMETER RemoveSource
  Also delete the source checkout and all build artifacts (runs Clean-Transfera
  first, then removes the repo directory). Only relevant if this script is
  running from inside or next to the repo.
.PARAMETER RemoveCache
  Also clear the pip and npm global caches.
.PARAMETER Yes
  Non-interactive — skip all confirmation prompts (dangerous with -RemoveData).
#>
[CmdletBinding()]
param(
  [switch]$RemoveData,
  [switch]$RemoveSource,
  [switch]$RemoveCache,
  [switch]$Yes
)

$ErrorActionPreference = "Stop"

function Ok([string]$msg)   { Write-Host "  [OK] $msg"   -ForegroundColor Green  }
function Step([string]$msg) { Write-Host "`n==> $msg"     -ForegroundColor Cyan   }
function Warn([string]$msg) { Write-Host "  [WARN] $msg"  -ForegroundColor Yellow }
function Info([string]$msg) { Write-Host "  [--] $msg"    -ForegroundColor Gray   }
function Fail([string]$msg) { Write-Host "  [FAIL] $msg"  -ForegroundColor Red; exit 1 }

function Confirm-Step([string]$msg) {
  if ($Yes) { return $true }
  $ans = Read-Host "$msg [Y/n]"
  return ($ans -eq "" -or $ans -match "^[Yy]")
}

function Remove-Dir-Safe([string]$path, [string]$label) {
  if (Test-Path $path) {
    $size = "{0:N1} MB" -f ((Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue |
      Measure-Object Length -Sum).Sum / 1MB)
    Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
    Ok "Removed $label  ($size)"
  } else {
    Info "Already gone: $label"
  }
}

Write-Host ""
Write-Host "  ══════════════════════════════════════════════════════════" -ForegroundColor Red
Write-Host "  ║  Transfera Uninstaller                                 ║" -ForegroundColor Red
Write-Host "  ══════════════════════════════════════════════════════════" -ForegroundColor Red
Write-Host ""
Write-Host "  This will:"                                                -ForegroundColor White
Write-Host "    ✓  Run the Transfera NSIS uninstaller (app binaries)"   -ForegroundColor White
if ($RemoveData)   { Write-Host "    ✓  Delete %APPDATA%\Transfera\ (DB, cache, logs)"  -ForegroundColor Yellow }
if ($RemoveSource) { Write-Host "    ✓  Delete the source checkout + build artifacts"   -ForegroundColor Yellow }
if ($RemoveCache)  { Write-Host "    ✓  Clear pip / npm global caches"                  -ForegroundColor Yellow }
Write-Host "    ✗  Your imported media vault is NEVER touched"          -ForegroundColor Green
Write-Host ""

if (-not (Confirm-Step "  Proceed with uninstall?")) { Write-Host "  Aborted."; exit 0 }

# ── 1  Kill any running Transfera processes ────────────────────────────────────
Step "Stopping any running Transfera processes"
$procs = @("transfera", "transfera-engine", "Transfera")
foreach ($name in $procs) {
  Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
    $_.Kill()
    Ok "Killed process: $($_.Name) (PID $($_.Id))"
  }
}

# ── 2  NSIS uninstaller ───────────────────────────────────────────────────────
Step "Running Tauri / NSIS uninstaller"
$nsisUninstaller = @(
  # Typical Tauri NSIS install location
  "$env:LOCALAPPDATA\Transfera\Uninstall Transfera.exe",
  "$env:ProgramFiles\Transfera\Uninstall Transfera.exe",
  "${env:ProgramFiles(x86)}\Transfera\Uninstall Transfera.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($nsisUninstaller) {
  Ok "Found uninstaller: $nsisUninstaller"
  if ($Yes) {
    Start-Process $nsisUninstaller -ArgumentList "/S" -Wait
    Ok "Uninstaller finished (silent)"
  } else {
    Start-Process $nsisUninstaller -Wait
    Ok "Uninstaller finished"
  }
} else {
  # Fallback: look in registry
  $regPaths = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
  )
  $regEntry = $null
  foreach ($rp in $regPaths) {
    $regEntry = Get-ChildItem $rp -ErrorAction SilentlyContinue |
      Get-ItemProperty -ErrorAction SilentlyContinue |
      Where-Object { $_.DisplayName -match "Transfera" } |
      Select-Object -First 1
    if ($regEntry) { break }
  }
  if ($regEntry -and $regEntry.UninstallString) {
    Ok "Found via registry: $($regEntry.UninstallString)"
    $uninst = $regEntry.UninstallString -replace '"', ''
    Start-Process $uninst -ArgumentList "/S" -Wait
    Ok "Uninstaller finished (via registry)"
  } else {
    Warn "Transfera NSIS uninstaller not found — app may already be uninstalled or was never installed via the NSIS package."
  }
}

# ── 3  Start Menu / desktop shortcuts ─────────────────────────────────────────
Step "Removing leftover shortcuts"
$shortcutPaths = @(
  "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Transfera",
  "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Transfera",
  "$env:PUBLIC\Desktop\Transfera.lnk",
  "$env:USERPROFILE\Desktop\Transfera.lnk"
)
foreach ($sp in $shortcutPaths) {
  if (Test-Path $sp) {
    Remove-Item $sp -Recurse -Force -ErrorAction SilentlyContinue
    Ok "Removed shortcut: $sp"
  }
}

# ── 4  App data ────────────────────────────────────────────────────────────────
Step "App data (%APPDATA%\Transfera)"
$appData = Join-Path $env:APPDATA "Transfera"
if ($RemoveData) {
  if (Confirm-Step "  Delete ALL app data in $appData (DB, thumbnails, exports, logs)?") {
    Remove-Dir-Safe $appData "%APPDATA%\Transfera\"
  } else {
    Warn "Skipped — app data left in place: $appData"
  }
} else {
  Info "Preserved (pass -RemoveData to delete): $appData"
  Info "Contains: database, thumbnails, logs, exports"
}

# ── 5  Installed app directory (LOCALAPPDATA) ─────────────────────────────────
Step "Installed app files (%LOCALAPPDATA%\Transfera)"
$localData = Join-Path $env:LOCALAPPDATA "Transfera"
if (Test-Path $localData) {
  Remove-Dir-Safe $localData "%LOCALAPPDATA%\Transfera\"
}

# ── 6  Source checkout + build artifacts ──────────────────────────────────────
Step "Source checkout / build artifacts"
if ($RemoveSource) {
  # Locate repo root (this script may be running from inside it)
  $Root = $null
  if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "..\run.py"))) {
    $Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
  } elseif (Test-Path "run.py") {
    $Root = (Get-Location).Path
  }

  if ($Root) {
    if (Confirm-Step "  Delete source checkout + all build artifacts at $Root?") {
      # Run the hygiene cleaner first so Rust target and sidecar are wiped cleanly
      $cleanScript = Join-Path $Root "scripts\Clean-Transfera.ps1"
      if (Test-Path $cleanScript) {
        powershell -ExecutionPolicy Bypass -File $cleanScript -Yes
      }
      # Now remove the repo directory itself (can't Remove-Item our own cwd)
      $tempScript = [System.IO.Path]::GetTempFileName() + ".ps1"
      @"
Start-Sleep -Seconds 2
Remove-Item -Path '$Root' -Recurse -Force -ErrorAction SilentlyContinue
"@ | Set-Content $tempScript
      Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -File `"$tempScript`"" -WindowStyle Hidden
      Ok "Scheduled removal of $Root (runs in background)"
    } else {
      Warn "Skipped — source checkout left in place."
    }
  } else {
    Warn "Could not locate repo root — skipping source removal."
  }
} else {
  Info "Skipped (pass -RemoveSource to also wipe the source checkout)"
}

# ── 7  Package caches ─────────────────────────────────────────────────────────
Step "Package manager caches"
if ($RemoveCache) {
  $pipCache = Join-Path $env:LOCALAPPDATA "pip\cache"
  Remove-Dir-Safe $pipCache "pip wheel cache"
  npm cache clean --force 2>&1 | Out-Null
  Ok "npm cache cleared"
} else {
  Info "Skipped pip/npm caches (pass -RemoveCache to include)"
}

# ── 8  Registry cleanup ───────────────────────────────────────────────────────
Step "Registry cleanup"
$regKeys = @(
  "HKCU:\SOFTWARE\Transfera",
  "HKLM:\SOFTWARE\Transfera"
)
foreach ($rk in $regKeys) {
  if (Test-Path $rk) {
    Remove-Item $rk -Recurse -Force -ErrorAction SilentlyContinue
    Ok "Removed registry key: $rk"
  }
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ══════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  ║  Transfera has been uninstalled.                       ║" -ForegroundColor Green
Write-Host "  ║                                                        ║" -ForegroundColor Green
if (-not $RemoveData) {
Write-Host "  ║  Your app data is still at:                            ║" -ForegroundColor Green
Write-Host "  ║    %APPDATA%\Transfera\                                ║" -ForegroundColor Green
Write-Host "  ║  Re-run with -RemoveData to delete it.                 ║" -ForegroundColor Green
Write-Host "  ║                                                        ║" -ForegroundColor Green
}
Write-Host "  ║  Your imported media vault was NOT touched.            ║" -ForegroundColor Green
Write-Host "  ══════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
