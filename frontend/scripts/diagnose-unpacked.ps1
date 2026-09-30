$stdoutLog = "unpacked-stdout.log"
$stderrLog = "unpacked-stderr.log"

if (Test-Path $stdoutLog) { Remove-Item $stdoutLog }
if (Test-Path $stderrLog) { Remove-Item $stderrLog }

$appPath = "frontend\src-tauri\target\release\Transfera.exe"
if (-not (Test-Path $appPath)) {
    Write-Host "Tauri release binary not found at $appPath. Run 'npm run tauri:build' first." -ForegroundColor Yellow
    exit 1
}

Write-Host "Launching Unpacked Transfera (Tauri)..."
$p = Start-Process -FilePath $appPath -NoNewWindow -PassThru -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
Start-Sleep -Seconds 10

Write-Host "Stopping processes..."
Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
Stop-Process -Name "*Transfera*" -Force -ErrorAction SilentlyContinue

Write-Host "`n=== STDOUT ==="
if (Test-Path $stdoutLog) {
    Get-Content $stdoutLog
    Remove-Item $stdoutLog
} else {
    Write-Host "(no stdout output)"
}

Write-Host "`n=== STDERR ==="
if (Test-Path $stderrLog) {
    Get-Content $stderrLog
    Remove-Item $stderrLog
} else {
    Write-Host "(no stderr output)"
}
