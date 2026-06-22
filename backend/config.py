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
BACKEND_ROOT: Path = Path(__file__).resolve().parent
DATA_DIR: Path = BACKEND_ROOT / "data"
DB_DIR: Path = DATA_DIR / "db"
CACHE_DIR: Path = DATA_DIR / "cache"
LOG_DIR: Path = DATA_DIR / "logs"
EXPORT_DIR: Path = DATA_DIR / "exports"
EXIFTOOL_DIR: Path = BACKEND_ROOT / "bin" / "exiftool"
WPD_HELPER: Path = BACKEND_ROOT / "bin" / "wpd_helper.exe"

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
IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".tiff", ".tif", ".webp", ".heic", ".heif",
    ".svg", ".ico", ".raw", ".cr2", ".nef",
    ".arw", ".dng", ".avif", ".jxl",
})

VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".wmv",
    ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
    ".3gp", ".ts", ".vob", ".ogv", ".rm",
    ".rmvb", ".asf", ".divx",
})

AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp3", ".flac", ".wav", ".aac", ".ogg",
    ".wma", ".m4a", ".opus", ".aiff", ".ape",
    ".alac", ".mid", ".midi",
})

DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".txt", ".rtf", ".odt",
    ".ods", ".odp", ".csv", ".epub", ".mobi",
})

ALL_MEDIA_EXTENSIONS: frozenset[str] = (
    IMAGE_EXTENSIONS
    | VIDEO_EXTENSIONS
    | AUDIO_EXTENSIONS
    | DOCUMENT_EXTENSIONS
)

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
