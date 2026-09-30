$stdoutLog = "portable-stdout.log"
$stderrLog = "portable-stderr.log"

if (Test-Path $stdoutLog) { Remove-Item $stdoutLog }
if (Test-Path $stderrLog) { Remove-Item $stderrLog }

$appPath = Get-ChildItem -Path "frontend\src-tauri\target\release\bundle\nsis\*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
if (-not $appPath -or -not (Test-Path $appPath)) {
    Write-Host "Tauri installer not found in bundle directory. Run 'npm run tauri:build' first." -ForegroundColor Yellow
    exit 1
}

Write-Host "Inspecting installer at: $appPath"
Write-Host "Launch with /S for silent install, or run directly to test NSIS wizard."
