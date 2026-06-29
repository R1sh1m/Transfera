@echo off
:: Transfera v2 — Release Certificate Trust & Installer Helper
:: Run this batch file as Administrator to trust the Transfera publisher key before installing.

:: 1. Check for Admin Privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ==========================================================
    echo ERROR: This script must be run as Administrator.
    echo ==========================================================
    echo Please right-click this file and select "Run as administrator".
    echo ==========================================================
    pause
    exit /b 1
)

:: 2. Resolve Paths
set SCRIPT_DIR=%~dp0
set CER_FILE=%SCRIPT_DIR%transfera-release.cer

:: 3. Check if CER exists
if not exist "%CER_FILE%" (
    echo ==========================================================
    echo ERROR: transfera-release.cer was not found in the same folder.
    echo Please ensure "transfera-release.cer" is in:
    echo %SCRIPT_DIR%
    echo ==========================================================
    pause
    exit /b 1
)

:: 4. Import Certificate to Trusted Stores
echo [INFO] Importing certificate to Trusted Root Certification Authorities...
certutil -addstore -f Root "%CER_FILE%" >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Failed to add certificate to Trusted Root store.
    pause
    exit /b 1
)

echo [INFO] Importing certificate to Trusted Publishers...
certutil -addstore -f TrustedPublisher "%CER_FILE%" >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Failed to add certificate to Trusted Publishers store.
    pause
    exit /b 1
)

echo [SUCCESS] Certificate imported successfully! Windows SmartScreen now trusts the publisher.
echo.

:: 5. Launch Installer
:: Find any Transfera-Setup-*.exe in the current folder
set INSTALLER=
for %%f in ("%SCRIPT_DIR%Transfera-Setup-*.exe") do (
    set INSTALLER=%%f
)

if "%INSTALLER%"=="" (
    echo [INFO] Certificate trusted successfully. No installer found in this folder to launch.
) else (
    echo [INFO] Launching installer: %INSTALLER%
    start "" "%INSTALLER%"
)

time /t >nul
