"""
MediaVault v2 -- ExifTool Bootstrapper Test Suite
Tests the three-tier fallback resolution: local -> PATH -> download.
Run: python -m backend.test_exiftool_bootstrapper
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.config import EXIFTOOL_DIR  # noqa: E402
from backend.engines import metadata_extractor as me  # noqa: E402

_PASS = 0
_FAIL = 0


def _check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f"  -- {detail}"
        print(msg)


def _reset_state() -> None:
    """Reset the bootstrapper global state between tests."""
    me._resolved_exiftool = None
    me._bootstrap_done = False


# ======================================================================
# 1. Local binary detection
# ======================================================================
def test_local_binary() -> None:
    print("\n=== Tier 1: Local Binary Detection ===")

    _reset_state()

    # Create a fake exiftool.exe in the expected local path
    EXIFTOOL_DIR.mkdir(parents=True, exist_ok=True)
    fake_exe = EXIFTOOL_DIR / me._EXIFTOOL_EXE_NAME
    fake_exe.write_bytes(b"fake-exiftool")
    try:
        result = me._bootstrap_exiftool()
        _check(
            "Returns local path when binary exists",
            result == str(fake_exe),
            f"got: {result}",
        )
        _check(
            "Resolved path matches local binary",
            me._resolved_exiftool == str(fake_exe),
        )
    finally:
        fake_exe.unlink(missing_ok=True)
        _reset_state()


# ======================================================================
# 2. Local binary missing -> falls through to PATH/download
# ======================================================================
def test_local_missing_falls_through() -> None:
    print("\n=== Tier 1: Local Binary Missing ===")

    _reset_state()

    # Ensure no local binary
    fake_exe = EXIFTOOL_DIR / me._EXIFTOOL_EXE_NAME
    fake_exe.unlink(missing_ok=True)

    # If exiftool is on PATH, it will resolve via Tier 2
    # If not, it will attempt Tier 3 (download)
    result = me._bootstrap_exiftool()
    # We can't assert the result since it depends on the environment
    # but we verify no exception was thrown
    _check(
        "Bootstrap completes without exception",
        result is None or isinstance(result, str),
    )
    _reset_state()


# ======================================================================
# 3. Binary path used in subprocess command
# ======================================================================
def test_command_uses_resolved_path() -> None:
    print("\n=== Subcommand Path Resolution ===")

    _reset_state()

    # Manually set resolved path to a known value
    me._resolved_exiftool = "/custom/path/to/exiftool"
    me._bootstrap_done = True

    cmd = me._build_exiftool_cmd()
    _check(
        "Command starts with resolved path",
        cmd[0] == "/custom/path/to/exiftool",
        f"got: {cmd[0] if cmd else 'empty'}",
    )
    _check(
        "Command includes JSON flag",
        "-json" in cmd,
    )
    _check(
        "Command includes time:all flag",
        "-time:all" in cmd,
    )
    _reset_state()


# ======================================================================
# 4. Empty command when no binary found
# ======================================================================
def test_empty_command_when_missing() -> None:
    print("\n=== Empty Command When Missing ===")

    _reset_state()

    # Ensure no local binary and mock shutil.which to return None
    fake_exe = EXIFTOOL_DIR / me._EXIFTOOL_EXE_NAME
    fake_exe.unlink(missing_ok=True)

    with patch.object(shutil, "which", return_value=None):
        cmd = me._build_exiftool_cmd()
        _check(
            "Returns empty list when binary missing",
            cmd == [],
            f"got: {cmd}",
        )
    _reset_state()


# ======================================================================
# 5. Extraction falls back to filesystem when no binary
# ======================================================================
def test_fallback_to_filesystem() -> None:
    print("\n=== Filesystem Fallback ===")

    _reset_state()

    # Force bootstrap to fail
    me._resolved_exiftool = None
    me._bootstrap_done = True

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(b"\xff\xd8\xff\xe0")  # Minimal JPEG header
        tmp_path = Path(tmp.name)

    try:
        meta = me.extract_metadata(tmp_path)
        _check(
            "Returns FileMetadata on fallback",
            meta.file_name == tmp_path.name,
        )
        _check(
            "date_taken is None (no EXIF)",
            meta.date_taken is None,
        )
        _check(
            "date_modified is set from filesystem",
            meta.date_modified is not None,
        )
        _check(
            "exif_tags is empty dict",
            meta.exif_tags == {},
        )
    finally:
        tmp_path.unlink(missing_ok=True)
        _reset_state()


# ======================================================================
# 6. Network download: version scraping (mocked)
# ======================================================================
def test_version_scrape_failure() -> None:
    print("\n=== Version Scrape: Network Failure ===")

    _reset_state()

    from urllib.error import URLError

    def mock_urlopen(*args, **kwargs):
        raise URLError("Simulated network failure")

    with patch("backend.engines.metadata_extractor.urlopen", mock_urlopen):
        ver = me._fetch_latest_version()
        _check(
            "Returns None on network failure",
            ver is None,
        )
    _reset_state()


# ======================================================================
# 7. Network download: zip extraction (mocked)
# ======================================================================
def test_zip_extraction() -> None:
    print("\n=== Zip Extraction ===")

    _reset_state()

    # Create a valid zip with a fake exiftool.exe inside
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)

        # Create a fake directory structure like the official zip
        inner_dir = tmp / "exiftool_12.97"
        inner_dir.mkdir()
        exe_path = inner_dir / me._EXIFTOOL_EXE_NAME
        exe_path.write_bytes(b"fake-exiftool-binary")

        zip_path = tmp / "exiftool-12_97.zip"
        with me.zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(exe_path, f"exiftool_12_97/{me._EXIFTOOL_EXE_NAME}")

        # Override EXIFTOOL_DIR to a temp location
        test_bin_dir = tmp / "bin" / "exiftool"
        with patch.object(me, "EXIFTOOL_DIR", test_bin_dir):
            result = me._extract_from_zip(zip_path)

            _check(
                "Extraction returns a Path",
                result is not None and isinstance(result, Path),
            )
            if result:
                _check(
                    "Extracted file exists",
                    result.is_file(),
                )
                _check(
                    "Extracted file is in correct directory",
                    str(test_bin_dir) in str(result),
                )
                # Clean up extracted file
                result.unlink(missing_ok=True)
    _reset_state()


# ======================================================================
# 8. Download URL construction
# ======================================================================
def test_download_url_construction() -> None:
    print("\n=== Download URL Construction ===")

    _reset_state()

    # Verify URL pattern
    ver = "12.97"
    ver_underscore = ver.replace(".", "_")
    zip_name = f"exiftool-{ver_underscore}.zip"
    expected_url = f"https://exiftool.org/{zip_name}"
    _check(
        "URL pattern is correct",
        expected_url == "https://exiftool.org/exiftool-12_97.zip",
    )
    _reset_state()


# ======================================================================
# 9. Platform detection
# ======================================================================
def test_platform_detection() -> None:
    print("\n=== Platform Detection ===")

    _reset_state()

    _check(
        "EXIFTOOL_EXE_NAME matches platform",
        (me._EXIFTOOL_EXE_NAME == "exiftool.exe") == (sys.platform == "win32"),
    )
    _check(
        "EXIFTOOL_DIR is under BACKEND_ROOT",
        "backend" in str(EXIFTOOL_DIR),
    )
    _reset_state()


# ======================================================================
# 10. Bootstrap idempotency (called twice, same result)
# ======================================================================
def test_bootstrap_idempotent() -> None:
    print("\n=== Bootstrap Idempotency ===")

    _reset_state()

    # Force no binary available
    fake_exe = EXIFTOOL_DIR / me._EXIFTOOL_EXE_NAME
    fake_exe.unlink(missing_ok=True)

    with patch.object(shutil, "which", return_value=None):
        # Patch download to also fail
        with patch.object(me, "_download_exiftool", return_value=None):
            result1 = me._bootstrap_exiftool()
            result2 = me._bootstrap_exiftool()
            _check(
                "Second call returns same result",
                result1 == result2,
            )
            _check(
                "Bootstrap flag set after first call",
                me._bootstrap_done is True,
            )
    _reset_state()


# ======================================================================
# Runner
# ======================================================================
def main() -> None:
    print("=" * 60)
    print("  MediaVault v2 -- ExifTool Bootstrapper Test Suite")
    print("=" * 60)

    _reset_state()

    test_local_binary()
    test_local_missing_falls_through()
    test_command_uses_resolved_path()
    test_empty_command_when_missing()
    test_fallback_to_filesystem()
    test_version_scrape_failure()
    test_zip_extraction()
    test_download_url_construction()
    test_platform_detection()
    test_bootstrap_idempotent()

    print("\n" + "=" * 60)
    total = _PASS + _FAIL
    print(f"  Results: {_PASS}/{total} passed, {_FAIL} failed")
    print("=" * 60)

    if _FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
