"""
Transfera v2 — Central Configuration
Single source of truth for all backend constants.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
PORT: int = 47821
HOST: str = "127.0.0.1"

# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
BATCH_SIZE: int = 100
MAX_RETRY: int = 3
PARTIAL_SUFFIX: str = ".partial"
TEMP_SUFFIX: str = ".tmp"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Frozen-sidecar support (Tauri `transfera-engine` via PyInstaller one-dir):
# when frozen, `__file__` points inside the bundle temp dir, so backend code
# and data files resolve through sys._MEIPASS. Tauri ships helper binaries
# (wpd_helper.exe, exiftool.exe + exiftool_files/ Perl runtime tree) in its
# `resources/` dir next to the sidecar — located via TRANSFERA_RESOURCE_DIR
# (set by the Tauri shell and release packaging) with a sibling-of-executable
# fallback.
import sys as _sys

_FROZEN: bool = getattr(_sys, "frozen", False)
_MEPASS: Path | None = Path(getattr(_sys, "_MEIPASS", "")) if _FROZEN else None

BACKEND_ROOT: Path = _MEPASS / "backend" if _MEPASS is not None else Path(__file__).resolve().parent

_env_resource_dir = os.environ.get("TRANSFERA_RESOURCE_DIR")
if _env_resource_dir:
    SIDECAR_RESOURCE_DIR: Path = Path(_env_resource_dir)
elif _FROZEN:
    # PyInstaller one-dir: sys.executable is
    # .../binaries/transfera-engine/transfera-engine.exe; Tauri resources
    # land in .../resources/.
    SIDECAR_RESOURCE_DIR = Path(_sys.executable).resolve().parent.parent / "resources"
else:
    # Dev: Tauri staging dir doubles as the resource dir so
    # `python run.py --tauri` resolves helpers without packaging.
    SIDECAR_RESOURCE_DIR = BACKEND_ROOT.parent / "frontend" / "src-tauri" / "resources"

# Support overriding the runtime data directory via environment variable (useful in production packaged app)
_env_data_dir = os.environ.get("TRANSFERA_DATA_DIR")
if _env_data_dir:
    DATA_DIR: Path = Path(_env_data_dir)
else:
    DATA_DIR: Path = BACKEND_ROOT / "data"

DB_DIR: Path = DATA_DIR / "db"
CACHE_DIR: Path = DATA_DIR / "cache"
LOG_DIR: Path = DATA_DIR / "logs"
EXPORT_DIR: Path = DATA_DIR / "exports"
EXIFTOOL_DIR: Path = DATA_DIR / "bin" / "exiftool"
PACKAGED_EXIFTOOL_DIR: Path = BACKEND_ROOT / "bin" / "exiftool"
# ExifTool launcher stub shipped as a Tauri resource. ExifTool v13.59+ uses a
# stub exe + sibling exiftool_files/ Perl runtime tree — both are staged into
# resources/ at build time. The bootstrapper in engines/metadata_extractor.py
# checks this path first (Tier 1 before the writable AppData copy).
PACKAGED_EXIFTOOL_EXE: Path = SIDECAR_RESOURCE_DIR / "exiftool.exe"


def _resolve_wpd_helper() -> Path:
    """Prefer the Tauri resource copy when frozen/staged, else the repo build.

    Dev ordering matters: src-tauri/resources/ holds 0-byte placeholders so
    `cargo check` passes without packaging — those must never shadow a real
    repo-built helper. A placeholder is 0 bytes; a real build never is.
    """
    staged = SIDECAR_RESOURCE_DIR / "wpd_helper.exe"
    repo = BACKEND_ROOT / "bin" / "wpd_helper.exe"

    def _real(p: Path) -> bool:
        try:
            return p.is_file() and p.stat().st_size > 0
        except OSError:
            return False

    if _FROZEN:
        return staged if _real(staged) else repo
    return repo if _real(repo) else staged


WPD_HELPER: Path = _resolve_wpd_helper()

# Ensure runtime directories exist at import time.
for _d in (DATA_DIR, DB_DIR, CACHE_DIR, LOG_DIR, EXPORT_DIR, EXIFTOOL_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASE_URL: str = f"sqlite+aiosqlite:///{DB_DIR / 'transfera.db'}"
DATABASE_URL_SYNC: str = f"sqlite:///{DB_DIR / 'transfera.db'}"

# ---------------------------------------------------------------------------
# Media Extension Sets  (frozensets for immutability & O(1) lookup)
# ---------------------------------------------------------------------------
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tiff",
        ".tif",
        ".webp",
        ".heic",
        ".heif",
        ".svg",
        ".ico",
        ".raw",
        ".cr2",
        ".cr3",
        ".nef",
        ".nrw",
        ".arw",
        ".srf",
        ".sr2",
        ".dng",
        ".orf",
        ".rw2",
        ".rwl",
        ".pef",
        ".raf",
        ".avif",
        ".jxl",
        ".kdc",
        ".dcr",
        ".iiq",
        ".3fr",
        ".erf",
        ".mef",
    }
)

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".wmv",
        ".flv",
        ".webm",
        ".m4v",
        ".mpg",
        ".mpeg",
        ".3gp",
        ".3g2",
        ".ts",
        ".mts",
        ".m2ts",
        ".vob",
        ".ogv",
        ".rm",
        ".rmvb",
        ".asf",
        ".divx",
        ".mxf",
    }
)

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".flac",
        ".wav",
        ".aac",
        ".ogg",
        ".wma",
        ".m4a",
        ".opus",
        ".aiff",
        ".ape",
        ".alac",
        ".mid",
        ".midi",
    }
)

DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".txt",
        ".rtf",
        ".odt",
        ".ods",
        ".odp",
        ".csv",
        ".epub",
        ".mobi",
    }
)

ALL_MEDIA_EXTENSIONS: frozenset[str] = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | DOCUMENT_EXTENSIONS

# ---------------------------------------------------------------------------
# Documents archive layout (date-wise skeleton under a sibling folder)
# ---------------------------------------------------------------------------
# Documents land under ``<dest_root>/Documents/<Kind>/YYYY/09-September/DD/``
# while photos/video/audio stay directly under ``<dest_root>/YYYY/...``.
# Kind buckets are a frozen extension map — deterministic, offline, no AI.
DOCUMENTS_DIR_NAME: str = "Documents"

DOCUMENT_KIND_MAP: dict[str, str] = {
    ".pdf": "PDFs",
    ".doc": "Word-Docs",
    ".docx": "Word-Docs",
    ".rtf": "Word-Docs",
    ".odt": "Word-Docs",
    ".xls": "Spreadsheets",
    ".xlsx": "Spreadsheets",
    ".ods": "Spreadsheets",
    ".csv": "Spreadsheets",
    ".ppt": "Presentations",
    ".pptx": "Presentations",
    ".odp": "Presentations",
    ".txt": "Text-CSV",
    ".epub": "eBooks",
    ".mobi": "eBooks",
}

DOCUMENT_KINDS: frozenset[str] = frozenset(sorted(set(DOCUMENT_KIND_MAP.values())))

DOCUMENT_KIND_FALLBACK: str = "Others"


def is_document_extension(ext: str | None) -> bool:
    """Return True when *ext* (e.g. ``".pdf"``) is a known document extension."""
    if not ext:
        return False
    return ext.lower() in DOCUMENT_EXTENSIONS


def document_kind_for_extension(ext: str | None) -> str:
    """Map a file extension to its Documents kind bucket (fallback: ``Others``)."""
    if not ext:
        return DOCUMENT_KIND_FALLBACK
    return DOCUMENT_KIND_MAP.get(ext.lower(), DOCUMENT_KIND_FALLBACK)


def document_kind_extensions(kind: str) -> frozenset[str]:
    """Return all extensions belonging to a kind bucket (empty set if unknown)."""
    return frozenset(ext for ext, k in DOCUMENT_KIND_MAP.items() if k == kind)


# ---------------------------------------------------------------------------
# Local secret token (destructive endpoint protection)
# ---------------------------------------------------------------------------
_TOKEN_FILE: Path = DATA_DIR / "local_secret.json"


def _load_or_create_token() -> str:
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text())["token"]
        except Exception:
            pass
    token = secrets.token_hex(32)
    _TOKEN_FILE.write_text(json.dumps({"token": token}))
    try:
        import os as _os

        # Owner-only on POSIX; on Windows the file lives under the user profile
        # DATA_DIR with default user-only ACLs. Best-effort hardening.
        _os.chmod(_TOKEN_FILE, 0o600)
    except OSError:
        pass
    return token


LOCAL_SECRET_TOKEN: str = _load_or_create_token()

# ---------------------------------------------------------------------------
# Supported Host Platforms
# ---------------------------------------------------------------------------
SUPPORTED_PLATFORMS: frozenset[str] = frozenset({"win32", "darwin", "linux"})

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.environ.get("TRANSFERA_LOG_LEVEL", "INFO")
LOG_FORMAT: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
