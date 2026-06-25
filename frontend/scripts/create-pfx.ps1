# Transfera v2 — Local Code Signing Cert Generator
# Run this script as Administrator to create and trust a self-signed PFX for code signing.

$password = "transfera123"
$pfxPath = Join-Path (Get-Location) "test-cert.pfx"
$certSubject = "CN=Transfera Local Testing"

# 1. Create a self-signed code signing certificate in User Store
Write-Host "Creating self-signed code signing certificate..." -ForegroundColor Cyan
$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $certSubject -HashAlgorithm SHA256 -KeyLength 2048 -CertStoreLocation "Cert:\CurrentUser\My"

# 2. Export to PFX file
Write-Host "Exporting certificate to $pfxPath..." -ForegroundColor Cyan
$securePassword = ConvertTo-SecureString $password -AsPlainText -Force
$cert | Export-PfxCertificate -FilePath $pfxPath -Password $securePassword

# 3. Add root trust so Windows SmartScreen trusts it locally
Write-Host "Adding certificate to Trusted Root Certification Authorities (requires Admin)..." -ForegroundColor Cyan
try {
    Import-Certificate -FilePath $pfxPath -CertStoreLocation "Cert:\LocalMachine\Root" -ErrorAction Stop
    Write-Host "Successfully trusted the certificate locally!" -ForegroundColor Green
} catch {
    Write-Host "WARNING: Failed to import certificate to Trusted Root. You may need to run this PowerShell console as Administrator." -ForegroundColor Yellow
}

Write-Host "`nTo sign your app locally, set these environment variables before building:" -ForegroundColor Green
Write-Host "  `$env:CSC_LINK = `"$pfxPath`"" -ForegroundColor Yellow
Write-Host "  `$env:CSC_KEY_PASSWORD = `"$password`"" -ForegroundColor Yellow
