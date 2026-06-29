# Transfera v2 — Release Code Signing Certificate Generator
# Run this script in PowerShell as Administrator to generate a self-signed release certificate.
# This produces both a .pfx (for signing the build) and a .cer (to distribute to users so they can trust it).

$password = "transfera-release-pwd"
$outDir = Join-Path (Get-Location) "release-certs"
if (-not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir | Out-Null
}

$pfxPath = Join-Path $outDir "transfera-release.pfx"
$cerPath = Join-Path $outDir "transfera-release.cer"
$certSubject = "CN=Transfera Open Source Media Backup"

Write-Host "Creating self-signed code signing certificate..." -ForegroundColor Cyan
$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $certSubject -HashAlgorithm SHA256 -KeyLength 2048 -CertStoreLocation "Cert:\CurrentUser\My"

Write-Host "Exporting certificate to PFX: $pfxPath..." -ForegroundColor Cyan
$securePassword = ConvertTo-SecureString $password -AsPlainText -Force
$cert | Export-PfxCertificate -FilePath $pfxPath -Password $securePassword

Write-Host "Exporting public key to CER: $cerPath..." -ForegroundColor Cyan
Export-Certificate -Cert $cert -FilePath $cerPath -Type CERT | Out-Null

Write-Host "`nSuccessfully generated release signing credentials!" -ForegroundColor Green
Write-Host "File Location: $outDir" -ForegroundColor White
Write-Host "PFX Password: $password" -ForegroundColor White
Write-Host "`nTo build a signed installer locally using this key, copy the PFX path and run:" -ForegroundColor Green
Write-Host "  `$env:CSC_LINK = `"$pfxPath`"" -ForegroundColor Yellow
Write-Host "  `$env:CSC_KEY_PASSWORD = `"$password`"" -ForegroundColor Yellow
Write-Host "  npm run electron:build" -ForegroundColor Yellow
