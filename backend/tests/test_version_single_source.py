"""
Version single-source test (no hardware required).

 backend/config.py::APP_VERSION is the single source of truth for backend
 version strings. This test pins it against every release-versioned file
 so a bump can never again leave /api/health reporting a stale version.
"""

from __future__ import annotations

import json
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_backend_version_matches_release_files():
    from backend.config import APP_VERSION

    root = _repo_root()
    package_json = json.loads((root / "frontend" / "package.json").read_text())
    tauri_conf = json.loads((root / "frontend" / "src-tauri" / "tauri.conf.json").read_text())

    import re

    def _toml_version(path: Path) -> str:
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', path.read_text())
        assert match, f"no version found in {path}"
        return match.group(1)

    expected = {
        "backend/config.py::APP_VERSION": APP_VERSION,
        "frontend/package.json": package_json["version"],
        "frontend/src-tauri/tauri.conf.json": tauri_conf["version"],
        "frontend/src-tauri/Cargo.toml": _toml_version(root / "frontend" / "src-tauri" / "Cargo.toml"),
        "pyproject.toml": _toml_version(root / "pyproject.toml"),
    }
    versions = set(expected.values())
    assert len(versions) == 1, f"version drift detected: {expected}"
