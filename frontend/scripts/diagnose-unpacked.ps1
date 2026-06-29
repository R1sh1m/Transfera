$env:ELECTRON_ENABLE_LOGGING = "1"
$stdoutLog = "unpacked-stdout.log"
$stderrLog = "unpacked-stderr.log"

if (Test-Path $stdoutLog) { Remove-Item $stdoutLog }
if (Test-Path $stderrLog) { Remove-Item $stderrLog }

Write-Host "Launching Unpacked Transfera..."
$p = Start-Process -FilePath "frontend\release\win-unpacked\Transfera.exe" -NoNewWindow -PassThru -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
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
