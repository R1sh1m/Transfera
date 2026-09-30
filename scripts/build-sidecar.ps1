# Transfera v2 — build the frozen Python sidecar for the Tauri shell.
# Usage (from repo root):  powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1
#
# Steps: ensure .venv (Python 3.12) → pip install requirements.txt +
# pyinstaller → pyinstaller transfera-engine.spec → mirror the whole one-dir
# folder (transfera-engine.exe + _internal/ runtime) into
# frontend/src-tauri/resources/transfera-engine/ (Tauri *resources* layout).
#
# The one-dir folder must ship whole: Tauri externalBin only supports single
# files, so staging just the exe leaves its _internal/ runtime behind and
# the installed engine dies on launch with "Failed to load Python DLL".

$ErrorActionPreference = "Stop"

# Windows-only: venv layout (.venv\Scripts), .exe suffixes, and the
# msvc/gnu alias pair below are all Windows-specific.
# macOS/Linux users: bash scripts/build-sidecar.sh (same contract).
# $IsWindows only exists on PowerShell 6+; Windows PowerShell 5.1 (which is
# Windows-only) has no such variable, so absence means Windows.
if ((Test-Path variable:IsWindows) -and (-not $IsWindows)) {
    Write-Error "This script is for Windows. On macOS/Linux use: bash scripts/build-sidecar.sh"
    exit 1
}

$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$ResDir = Join-Path $Root "frontend\src-tauri\resources\transfera-engine"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Missing .venv Python at $VenvPython — run 'python run.py' once first (it creates the 3.12 venv)."
    exit 1
}

# One-time VC++ redistributable for the NSIS post-install hook
# (python312.dll links VCRUNTIME140.dll, which is not inbox on Windows —
# without it the installed engine dies with "Failed to load Python DLL").
$VcRedist = Join-Path $Root "frontend\src-tauri\resources\VC_redist.x64.exe"
$VcUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
if (-not (Test-Path $VcRedist) -or (Get-Item $VcRedist).Length -eq 0) {
    Write-Host "  Downloading VC++ redistributable (~25 MB)..." -ForegroundColor DarkCyan
    $vcOk = $false
    for ($i = 1; $i -le 3 -and -not $vcOk; $i++) {
        try {
            Invoke-WebRequest -Uri $VcUrl -OutFile $VcRedist -UseBasicParsing -TimeoutSec 120
            if ((Get-Item $VcRedist).Length -gt 0) { $vcOk = $true }
        } catch {
            Write-Host "    attempt $i failed: $($_.Exception.Message)" -ForegroundColor Yellow
            Start-Sleep -Seconds ($i * 2)
        }
    }
    if (-not $vcOk) {
        Write-Error "VC++ redistributable download failed ($VcUrl). Download VC_redist.x64.exe manually into frontend\src-tauri\resources\ and re-run."
        exit 1
    }
    Write-Host "  VC++ redistributable ready"
} else {
    Write-Host "  VC++ redistributable already staged"
}

Write-Host "  Ensuring PyInstaller is up to date..." -ForegroundColor DarkCyan
# Native commands log progress to stderr. Under `$ErrorActionPreference =
# "Stop", Windows PowerShell 5.1 turns those 2>&1-merged stderr lines into
# terminating errors and aborts on the first INFO line — so scope Continue
# around native calls only. Every call is still validated via $LASTEXITCODE.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $VenvPython -m pip install --upgrade pyinstaller -q 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error "pyinstaller install failed"; exit 1 }

    Push-Location $Root
    try {
        Write-Host "  Freezing Python backend (PyInstaller one-dir)..." -ForegroundColor DarkCyan
        & $VenvPython -m PyInstaller --noconfirm transfera-engine.spec 2>&1 |
            Where-Object { $_ -match "(INFO: Build complete|WARNING|ERROR)" } |
            ForEach-Object { Write-Host "    $_" }
        if ($LASTEXITCODE -ne 0) { Write-Error "pyinstaller build failed"; exit 1 }
    } finally {
        Pop-Location
    }
} finally {
    $ErrorActionPreference = $prevEAP
}

$BuiltDir = Join-Path $Root "dist\transfera-engine"
$Built = Join-Path $BuiltDir "transfera-engine.exe"
if (-not (Test-Path $Built)) { Write-Error "Expected output missing: $Built"; exit 1 }

# Mirror the whole one-dir folder into Tauri resources (exe + _internal/
# runtime). Tauri externalBin only supports single files, so staging just
# the exe leaves its runtime behind and the installed engine dies on launch
# with "Failed to load Python DLL". Remove-then-copy keeps staging clean
# across rebuilds; .gitkeep is preserved so the dir stays tracked.
if (Test-Path $ResDir) {
    Get-ChildItem $ResDir -Exclude ".gitkeep" -Force | Remove-Item -Recurse -Force
} else {
    New-Item -ItemType Directory -Path $ResDir -Force | Out-Null
}
Copy-Item (Join-Path $BuiltDir "*") $ResDir -Recurse -Force
$StagedExe = Join-Path $ResDir "transfera-engine.exe"
if (-not (Test-Path $StagedExe)) { Write-Error "Staging failed: $StagedExe missing"; exit 1 }
# Version-agnostic: release venvs use 3.12 (python312.dll), dev machines may
# run newer interpreters — any python3*.dll proves the runtime shipped.
$pyDll = Get-ChildItem (Join-Path $ResDir "_internal\python3*.dll") -File -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pyDll) { Write-Error "Staging failed: _internal Python runtime missing"; exit 1 }
Write-Host "Sidecar staged: $ResDir"
$SizeMB = [math]::Round((Get-Item $StagedExe).Length / 1MB, 1)
$DirMB = [math]::Round(((Get-ChildItem $BuiltDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "exe: ${SizeMB} MB, one-dir total: ${DirMB} MB"

# Smoke-test the frozen engine: boot it against a throwaway data dir and
# probe /api/health. The frozen entry point has never been executed by any
# earlier gate, and import-string vs app-object mistakes (or missing hidden
# imports) only surface here. Skipped when :47821 is already occupied — a
# running dev backend would make the probe a false pass.
$smokeTcp = New-Object Net.Sockets.TcpClient
try {
    $smokeTcp.Connect("127.0.0.1", 47821)
    $smokePortInUse = $true
} catch {
    $smokePortInUse = $false
} finally {
    $smokeTcp.Close()
}
if ($smokePortInUse) {
    Write-Host "Port 47821 occupied (dev backend?) — skipping frozen-engine smoke test" -ForegroundColor Yellow
} else {
    Write-Host "  Smoke-testing frozen engine (/api/health)..." -ForegroundColor DarkCyan
    $smokeData = Join-Path ([IO.Path]::GetTempPath()) "transfera-sidecar-smoke"
    # stderr to a file: inherited stdout reproduces the installed launch most
    # faithfully, while a captured stderr names the cause on failure
    # (tracebacks never reach transfera.log when boot dies during imports).
    $smokeErr = Join-Path ([IO.Path]::GetTempPath()) "transfera-sidecar-smoke.err.log"
    Remove-Item $smokeErr -Force -ErrorAction SilentlyContinue
    $prevDataDir = $env:TRANSFERA_DATA_DIR
    $prevResDir = $env:TRANSFERA_RESOURCE_DIR
    $env:TRANSFERA_DATA_DIR = $smokeData
    $env:TRANSFERA_RESOURCE_DIR = Join-Path $Root "frontend\src-tauri\resources"
    $smokeProc = Start-Process -FilePath $StagedExe -RedirectStandardError $smokeErr -PassThru
    try {
        $smokeHealthy = $false
        for ($i = 0; $i -lt 90; $i++) {
            Start-Sleep -Seconds 1
            if ($smokeProc.HasExited) { break }
            try {
                $smokeRes = Invoke-WebRequest -Uri "http://127.0.0.1:47821/api/health" -TimeoutSec 2 -UseBasicParsing
                if ($smokeRes.StatusCode -eq 200) { $smokeHealthy = $true; break }
            } catch { }
        }
        if (-not $smokeHealthy) {
            Write-Host "--- frozen engine stderr tail ---" -ForegroundColor Yellow
            Get-Content $smokeErr -Tail 15 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
            Write-Error "Frozen engine smoke test failed (no /api/health from staged bundle)."; exit 1
        }
        Write-Host "Frozen engine smoke test passed (/api/health OK)"
    } finally {
        if (-not $smokeProc.HasExited) { Stop-Process -Id $smokeProc.Id -Force }
        Remove-Item -Recurse -Force $smokeData -ErrorAction SilentlyContinue
        $env:TRANSFERA_DATA_DIR = $prevDataDir
        $env:TRANSFERA_RESOURCE_DIR = $prevResDir
    }
}
