# Transfera NSIS hooks — bundled VC++ redistributable install.
#
# python312.dll (frozen engine) links against VCRUNTIME140.dll, which is NOT
# inbox on Windows: machines without any VS/redist software show the
# bootloader's "Failed to load Python DLL" fatal dialog on first launch.
# This POSTINSTALL hook installs the redist silently when missing.
# The exe is bundled via bundle.resources (VC_redist.x64.exe, fetched at
# build time by scripts/build-sidecar.ps1) and cleaned up afterwards.
# The runtime is shared with other apps, so it is never uninstalled here.
!macro NSIS_HOOK_POSTINSTALL
  ; One DWORD covers every VS 2015+ (14.x) x64 runtime.
  ReadRegDWord $0 HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" "Installed"
  ${If} $0 == 1
    DetailPrint "Visual C++ Redistributable already installed"
    Goto vcredist_done
  ${EndIf}

  ${If} ${FileExists} "$INSTDIR\resources\VC_redist.x64.exe"
    DetailPrint "Installing Visual C++ Redistributable (one-time)..."
    CopyFiles /SILENT "$INSTDIR\resources\VC_redist.x64.exe" "$TEMP\VC_redist.x64.exe"
    ExecWait '"$TEMP\VC_redist.x64.exe" /install /passive /norestart' $0
    ; 0 = success, 1638 = a newer runtime is already installed.
    ${If} $0 == 0
    ${OrIf} $0 == 1638
      DetailPrint "Visual C++ Redistributable ready."
    ${Else}
      DetailPrint "Visual C++ install exited with code $0 - the engine may fail to start without the runtime."
    ${EndIf}
    Delete "$TEMP\VC_redist.x64.exe"
    Delete "$INSTDIR\resources\VC_redist.x64.exe"
  ${Else}
    DetailPrint "VC_redist.x64.exe not bundled - skipping runtime install."
  ${EndIf}

  vcredist_done:
!macroend

# Stop our own processes before (un)install file operations.
#
# The frozen engine keeps exiftool.exe (stay-open) alive after the app
# closes, and a locked exiftool.exe aborts setup with "Error opening file
# for writing" (same on uninstall: locked DLLs get left behind).
# taskkill misses are ignored (exit code unchecked); the Sleep lets file
# handles release before NSIS copies/removes files.
!macro NSIS_HOOK_PREINSTALL
  DetailPrint "Stopping any running Transfera processes..."
  nsExec::ExecToLog 'taskkill /F /IM transfera.exe /T'
  nsExec::ExecToLog 'taskkill /F /IM transfera-engine.exe /T'
  nsExec::ExecToLog 'taskkill /F /IM exiftool.exe /T'
  Sleep 2000
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "Stopping any running Transfera processes..."
  nsExec::ExecToLog 'taskkill /F /IM transfera.exe /T'
  nsExec::ExecToLog 'taskkill /F /IM transfera-engine.exe /T'
  nsExec::ExecToLog 'taskkill /F /IM exiftool.exe /T'
  Sleep 2000
!macroend
