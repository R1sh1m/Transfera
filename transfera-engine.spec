# -*- mode: python ; coding: utf-8 -*-
"""
Transfera v2 — PyInstaller spec for the Tauri sidecar (`transfera-engine`).

One-dir (not one-file): faster cold start and smaller delta updates, since
only changed files re-download on update. Built by scripts/build-sidecar.ps1
(CI release job runs it before `tauri build`); the whole one-dir folder is
mirrored into frontend/src-tauri/resources/transfera-engine/ and shipped as
a Tauri *resource* (externalBin only supports single files, so shipping just
the exe leaves its _internal/ runtime behind and the engine dies on launch
with "Failed to load Python DLL ... python312.dll").

Size discipline: base requirements only (requirements.txt — which now
includes the AI runtime from day one; ~120 MB accepted per product call).
Model *weights* (~207 MB) stay on-demand and are NEVER frozen in.
"""

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

block_cipher = None

# pywin32 native bits (win32/*.pyd incl. win32security, plus the
# pywin32_system32 DLLs they link against). pymobiledevice3's Windows usbmux
# path does `import win32security` at module top — without these files the
# frozen engine loses all Tier 1 iPhone access with a bare ImportError.
# Guarded: pywin32 is Windows-only, so non-Windows builds skip silently.
pywin32_binaries: list = []
try:
    pywin32_binaries += collect_dynamic_libs("win32")
except Exception as exc:  # noqa: BLE001 - optional platform dependency
    print(f"pywin32 win32/*.pyd collection skipped: {exc}")
try:
    pywin32_binaries += collect_dynamic_libs("pywin32_system32")
except Exception as exc:  # noqa: BLE001 - directory is not an importable package
    print(f"pywin32_system32 DLL collection skipped: {exc}")

a = Analysis(
    ["backend/main.py"],
    pathex=[],
    binaries=[*pywin32_binaries],
    datas=[
        # On-demand AI runtime manifest: backend/engines/clip.py resolves it
        # via BACKEND_ROOT (MEIPASS when frozen). The AI wheels themselves
        # stay on-demand — never frozen in.
        ("backend/requirements-ai.txt", "backend"),
        # WSL bridge script: the Tier 2 orchestrator copies this file into
        # the WSL distro at setup time (resolved via BACKEND_ROOT, which
        # points at MEIPASS when frozen — __file__-relative paths do not
        # exist on disk inside the bundle).
        ("backend/wsl_bridge.py", "backend"),
    ],
    hiddenimports=[
        # uvicorn dynamic imports (protocols/loops/lifespan selected by string)
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.middleware.proxy_headers",
        # ASGI / HTTP plumbing
        "wsproto",
        "h11",
        "httptools",
        "websockets",
        "watchfiles",
        # DB stack
        "aiosqlite",
        "sqlite3",
        *collect_submodules("sqlalchemy.dialects"),
        # Imaging
        "PIL",
        "pillow_heif",
        # Device stack (pymobiledevice3 pulls these dynamically)
        "construct",
        "cryptography",
        "hexdump",
        # pywin32: win32security is imported at module top by
        # pymobiledevice3.osu.win_util on Windows (admin check). Without an
        # explicit entry PyInstaller can miss the extension module.
        "win32security",
        "win32api",
        "pywintypes",
        # Validation / misc
        "pydantic",
        "email_validator",
        "multipart",
        # On-board AI runtime (bundled from day one — weights stay on-demand)
        "onnxruntime",
        "tokenizers",
        "numpy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "matplotlib",
        "pandas",
        "scipy",
        "notebook",
        "jupyter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="transfera-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    # System runtimes must never be UPX-packed: a packed python312.dll can
    # fail LoadLibrary on launch with the bootloader's "Failed to load
    # Python DLL" fatal dialog. (UPX isn't installed in CI today, so this
    # is insurance for future environments where it is.)
    upx_exclude=["python*.dll", "vcruntime*.dll", "msvcp*.dll", "ucrtbase.dll"],
    name="transfera-engine",
)

# Known-benign PyInstaller warnings (do not "fix" — they are the
# dependencies probing for optional components):
#   Hidden import "pysqlite2"/"MySQLdb" not found  — sqlalchemy DBAPI probes;
#     we use sqlite3/aiosqlite.  coredll.lib via ctypes not found — WinCE
#     artifact.  Suppressing warnings globally would also hide real bundle
#     gaps (e.g. a missing hidden import that breaks a frozen feature).
