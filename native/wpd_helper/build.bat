@echo off
setlocal enabledelayedexpansion

set "CONFIG=Release"
if /I "%~1"=="debug" set "CONFIG=Debug"

REM Paths — source is here, output goes to backend/bin/
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=!SCRIPT_DIR!..\.."
set "OUT_DIR=!PROJECT_ROOT!\backend\bin"
set "SRC=!SCRIPT_DIR!wpd_helper.cpp"
set "OUT=!OUT_DIR!\wpd_helper.exe"

if /I "%CONFIG%"=="Debug" (
    set "OUT=!PROJECT_ROOT!\backend\bin_debug\wpd_helper.exe"
)

REM ---- Incremental build check ----
REM Skip the full compile+link if the output exe already exists and is newer
REM than every source input (the .cpp and this build script itself).  This
REM prevents the race where the linker tries to overwrite an exe that the
REM backend's device-probe subprocess has just spawned.
if exist "!OUT!" (
    set "_NEEDS_BUILD=0"
    powershell -NoProfile -Command ^
        "$out = Get-Item -LiteralPath '!OUT!'; " ^
        "$src = Get-Item -LiteralPath '!SRC!'; " ^
        "$bat = Get-Item -LiteralPath '%~f0'; " ^
        "if ($src.LastWriteTime -gt $out.LastWriteTime -or $bat.LastWriteTime -gt $out.LastWriteTime) { exit 1 } else { exit 0 }"
    if errorlevel 1 (
        echo Source changed -- rebuilding wpd_helper...
    ) else (
        echo wpd_helper.exe is up to date -- skipping build.
        exit /b 0
    )
)

REM Find vswhere.exe
set "VSWHERE="
for %%p in (
    "%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe"
    "%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
) do (
    if exist %%p set "VSWHERE=%%~p"
)

if not defined VSWHERE (
    echo ERROR: vswhere.exe not found.
    exit /b 1
)

REM Get VS installation path
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do (
    set "VS_PATH=%%i"
)

if not defined VS_PATH (
    echo ERROR: No Visual Studio with C++ tools found.
    exit /b 1
)

set "VCVARSALL=!VS_PATH!\VC\Auxiliary\Build\vcvarsall.bat"
if not exist "!VCVARSALL!" (
    echo ERROR: vcvarsall.bat not found.
    exit /b 1
)

echo Setting up MSVC environment...
call "!VCVARSALL!" amd64 >nul 2>&1
if errorlevel 1 (
    echo ERROR: Failed to set up MSVC environment.
    exit /b 1
)

REM Compiler flags
set "CFLAGS=/nologo /W4 /WX /permissive- /Zc:wchar_t /utf-8 /EHsc /std:c++17"

if /I "%CONFIG%"=="Debug" (
    set "CFLAGS=!CFLAGS! /Od /Zi /RTC1 /MDd /D_DEBUG"
) else (
    set "CFLAGS=!CFLAGS! /O2 /Oi /DNDEBUG /MT"
)

REM Ensure output directory exists
if not exist "!OUT_DIR!" mkdir "!OUT_DIR!"
if /I "%CONFIG%"=="Debug" (
    if not exist "!PROJECT_ROOT!\backend\bin_debug" mkdir "!PROJECT_ROOT!\backend\bin_debug"
)

REM Build
echo Building wpd_helper (%CONFIG%)...
cl.exe !CFLAGS! "!SRC!" /Fe:"!OUT!" /link ^
    /SUBSYSTEM:CONSOLE ^
    ole32.lib ^
    portabledeviceguids.lib ^
    shlwapi.lib ^
    propsys.lib ^
    /OPT:REF ^
    /OPT:ICF

if errorlevel 1 (
    echo.
    echo BUILD FAILED
    exit /b 1
)

echo.
echo BUILD SUCCESSFUL: !OUT!
echo.

endlocal
