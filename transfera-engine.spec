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

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ["backend/main.py"],
    pathex=[],
    binaries=[],
    datas=[
        # On-demand AI runtime manifest: backend/engines/clip.py resolves it
        # via BACKEND_ROOT (MEIPASS when frozen). The AI wheels themselves
        # stay on-demand — never frozen in.
        ("backend/requirements-ai.txt", "backend"),
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
