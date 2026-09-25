"""
Transfera v2 — Metadata Extractor
ExifTool subprocess integration with automated bootstrapper and filesystem fallback.

Bootstrapper priority:
  1. Packaged read-only app resources (backend/bin/exiftool/exiftool.exe)
  2. Writable user AppData local storage (AppData/Roaming/.../bin/exiftool/exiftool.exe)
  3. Global system PATH lookup
  4. Auto-download from official distribution (Windows only, with full error handling)
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from backend.config import EXIFTOOL_DIR, PACKAGED_EXIFTOOL_DIR

logger = logging.getLogger(__name__)

# Register pillow-heif opener so Image.open() can decode HEIC/HEIF files
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    logger.debug("pillow-heif registered for HEIC/HEIF support")
except ImportError:
    logger.warning(
        "pillow-heif not installed — HEIC files will fall back to mtime. Install with: pip install pillow-heif"
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_EXIFTOOL_EXE_NAME = "exiftool.exe" if sys.platform == "win32" else "exiftool"
_LOCAL_EXIFTOOL: Path = EXIFTOOL_DIR / _EXIFTOOL_EXE_NAME
_PACKAGED_EXIFTOOL: Path = PACKAGED_EXIFTOOL_DIR / _EXIFTOOL_EXE_NAME

# Official ExifTool Windows zip download page
_EXIFTOOL_HOME = "https://exiftool.org"
_EXIFTOOL_ZIP_PATTERN = "exiftool-{ver}_?\\.zip"

# Network protections
_CONNECT_TIMEOUT = 10  # seconds
_READ_TIMEOUT = 60  # seconds
_DOWNLOAD_CHUNK = 8192  # bytes


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class FileMetadata:
    """Normalised metadata for a single media file."""

    file_path: str
    file_name: str
    file_size: int
    extension: str
    mime_type: str | None = None

    # Prioritised date fields (oldest -> newest preference in the scanner)
    date_taken: datetime | None = None  # EXIF DateTimeOriginal
    date_created: datetime | None = None  # EXIF CreateDate / filesystem ctime
    date_modified: datetime | None = None  # EXIF ModifyDate / filesystem mtime

    # Raw EXIF tags (if available)
    exif_tags: dict[str, str] = field(default_factory=dict)

    # Structured intelligence fields (persisted to media_items when present)
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------
_EXIF_DATETIME_FORMATS: list[str] = [
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f%z",
]


def _parse_exif_datetime(raw: str | None) -> datetime | None:
    """Attempt to parse an EXIF datetime string into a timezone-aware datetime.

    EXIF DateTimeOriginal is always local time at the capture location with
    NO timezone information. Formats without an explicit timezone offset are
    treated as local time using the system timezone. Formats with an explicit
    offset (e.g. "+05:30") are parsed as-is.
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()

    for fmt in _EXIF_DATETIME_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return dt
        except ValueError:
            continue
    return None


def _ts_to_datetime(ts: float) -> datetime:
    """Convert a POSIX timestamp to a UTC datetime."""
    return datetime.fromtimestamp(ts, tz=UTC)


# ---------------------------------------------------------------------------
# ExifTool Bootstrapper
# ---------------------------------------------------------------------------
# Resolved absolute path to the ExifTool binary; None until bootstrap runs.
_resolved_exiftool: str | None = None
_bootstrap_done = False


def _bootstrap_exiftool() -> str | None:
    """
    Resolve the ExifTool binary path using a four-tier fallback:

      1. Packaged read-only app resources (backend/bin/exiftool/)
      2. Writable user AppData local storage (AppData/Roaming/.../bin/exiftool/)
      3. System PATH via shutil.which()
      4. Auto-download from official source (Windows only)

    Returns the absolute path string on success, or None if unavailable.
    """
    global _resolved_exiftool, _bootstrap_done
    if _bootstrap_done:
        return _resolved_exiftool
    _bootstrap_done = True

    # Tier 1: Packaged binary (prioritized for Microsoft Store and offline support)
    if _PACKAGED_EXIFTOOL.is_file():
        logger.info("ExifTool found in package resources at %s", _PACKAGED_EXIFTOOL)
        _resolved_exiftool = str(_PACKAGED_EXIFTOOL)
        return _resolved_exiftool

    # Tier 2: Writable AppData local binary (fallback for non-Store download channel)
    if _LOCAL_EXIFTOOL.is_file():
        logger.info("ExifTool found in AppData local storage at %s", _LOCAL_EXIFTOOL)
        _resolved_exiftool = str(_LOCAL_EXIFTOOL)
        return _resolved_exiftool

    # Tier 3: System PATH
    path_loc = shutil.which("exiftool")
    if path_loc:
        logger.info("ExifTool found on system PATH at %s", path_loc)
        _resolved_exiftool = path_loc
        return _resolved_exiftool

    logger.info("ExifTool not found in package, AppData, or on PATH -- attempting automated download")

    # Tier 3: Auto-download (Windows only)
    if sys.platform != "win32":
        logger.warning(
            "Auto-download only supported on Windows; ExifTool unavailable -- falling back to filesystem timestamps"
        )
        return None

    try:
        downloaded = _download_exiftool()
        if downloaded and downloaded.is_file():
            _resolved_exiftool = str(downloaded)
            logger.info("ExifTool bootstrapped successfully at %s", _resolved_exiftool)
            return _resolved_exiftool
    except Exception:
        logger.warning(
            "ExifTool auto-download failed -- falling back to filesystem timestamps",
            exc_info=True,
        )

    return None


def _fetch_zip_candidates() -> list[str]:
    """
    Scrape the ExifTool homepage for Windows zip *download URLs*.

    exiftool.org no longer hosts the zips itself — the Windows builds
    (e.g. ``exiftool-13.59_32.zip`` / ``exiftool-13.59_64.zip``, where the
    underscore separates the CPU arch, NOT a version dot) live on
    SourceForge behind ``.../files/<name>.zip/download`` links.
    Reconstructing ``https://exiftool.org/<name>.zip`` therefore 404s, so
    the downloader must follow these verbatim hrefs. Returns deduplicated
    absolute URLs in page order (latest first), preferring 64-bit builds.
    """
    import re

    try:
        req = Request(
            _EXIFTOOL_HOME,
            headers={"User-Agent": "Transfera/2.0"},
        )
        with urlopen(req, timeout=_CONNECT_TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Anchor hrefs whose target is an official Windows exiftool-*.zip
        hrefs = re.findall(r"""<a[^>]*href\s*=\s*["']([^"']+)["']""", html, re.IGNORECASE)
        seen: list[str] = []
        for href in hrefs:
            # Official builds are `exiftool-<version>.zip` (dash). The dash
            # requirement excludes third-party zips like `modExiftool_101`.
            m = re.search(r"(exiftool-[\d][\d._]*\.zip)(/download)?", href, re.IGNORECASE)
            if not m:
                continue
            # Only official distribution hosts (sourceforge project files);
            # skip third-party zips (List_Exif_Metadata, modExiftool, ...).
            if "sourceforge.net/projects/exiftool/files/" not in href and not href.startswith(_EXIFTOOL_HOME):
                continue
            url = href if href.lower().endswith("/download") else href
            if url not in seen:
                seen.append(url)
        # Prefer 64-bit archives on 64-bit Windows, keep page order otherwise
        if sys.platform == "win32":
            seen.sort(key=lambda u: 0 if "_64" in u else 1)
        return seen
    except (URLError, OSError, ValueError):
        return []


def _normalise_candidate_version(zip_name_or_url: str) -> str | None:
    """Derive a display version from a scraped zip filename or URL.

    ``.../exiftool-13.59_64.zip/download`` -> ``13.59`` (trailing _32/_64
    is the CPU arch); ``exiftool-12_97.zip`` -> ``12.97``
    (legacy underscore-as-dot).
    """
    import re

    base = zip_name_or_url.rsplit("/", 1)[-1]
    m = re.match(r"exiftool[_-]([\d._]+)\.zip$", base, re.IGNORECASE)
    if not m:
        return None
    ver = m.group(1).replace("_", ".")
    parts = ver.split(".")
    if len(parts) == 3 and parts[2] in ("32", "64"):
        ver = ".".join(parts[:2])
    return ver


def _fetch_latest_version() -> str | None:
    """
    Scrape the ExifTool homepage to find the latest Windows zip filename.
    Returns the version string (e.g. '12.97', '13.59') or None on failure.
    """
    candidates = _fetch_zip_candidates()
    if not candidates:
        return None
    return _normalise_candidate_version(candidates[0])


def _download_exiftool() -> Path | None:
    """
    Download the official ExifTool Windows zip, extract exiftool.exe,
    and clean up the archive. Returns the path to the extracted binary.
    """
    import tempfile

    candidates = _fetch_zip_candidates()
    if not candidates:
        logger.warning("Could not determine latest ExifTool version from homepage")
        return None

    tmp_dir = Path(tempfile.mkdtemp(prefix="exiftool_dl_"))
    last_error: Exception | None = None
    try:
        # Try each verbatim download URL from the homepage in order —
        # older entries may have been removed upstream (404), so fall
        # through. SourceForge /download links redirect to a mirror;
        # urlopen follows redirects automatically.
        for url in candidates:
            version = _normalise_candidate_version(url) or "unknown"
            zip_name = url.rsplit("/", 1)[-1]
            if zip_name.lower() == "download":
                zip_name = f"exiftool-{version.replace('.', '_')}.zip"
            logger.info("Downloading ExifTool %s from %s", version, url)
            zip_path = tmp_dir / zip_name
            try:
                req = Request(url, headers={"User-Agent": "Transfera/2.0"})
                with urlopen(req, timeout=_READ_TIMEOUT) as resp:
                    total = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    with open(zip_path, "wb") as fh:
                        while True:
                            chunk = resp.read(_DOWNLOAD_CHUNK)
                            if not chunk:
                                break
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if total and downloaded % (1024 * 1024) == 0:
                                pct = (downloaded / total) * 100 if total else 0
                                logger.debug(
                                    "Download progress: %d/%d bytes (%.0f%%)",
                                    downloaded,
                                    total,
                                    pct,
                                )

                logger.info("Download complete: %d bytes -- extracting", zip_path.stat().st_size)

                extracted = _extract_from_zip(zip_path)
                if extracted and extracted.is_file():
                    return extracted
            except (URLError, OSError, TimeoutError) as exc:
                logger.warning("ExifTool download failed for %s: %s", url, exc)
                last_error = exc
                continue

        logger.warning("All ExifTool download candidates failed (last: %s)", last_error)
        return None
    finally:
        # Always clean up the temp directory and zip
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _extract_from_zip(zip_path: Path) -> Path | None:
    """
    Extract the ExifTool distribution from the downloaded zip into the local
    bin directory, resolving to EXIFTOOL_DIR/exiftool.exe.

    Layouts handled:
      * legacy: single dir with `exiftool.exe` / `exiftool(-k).exe` at root.
      * current (v13.59+): `exiftool(-k).exe` stub plus a bundled
        `exiftool_files/` runtime tree (perl DLLs, exiftool.pl, lib/).
        The stub locates its runtime in `exiftool_files/` next to the exe,
        so the whole tree must be extracted alongside it.
    """
    from pathlib import PurePath as _PurePath

    EXIFTOOL_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Find the exiftool executable inside the archive. Current
            # official zips ship it as `exiftool(-k).exe` at the archive
            # root (upstream tells users to rename it to `exiftool.exe`;
            # the bracket options only apply when kept in the filename, so
            # our renamed copy runs without the -k pause). Match on the
            # *basename* — directory names like `exiftool_files/` must not
            # count, otherwise perl.exe would be picked by accident.
            from pathlib import PurePath as _PurePath

            wanted = _EXIFTOOL_EXE_NAME.lower()
            exe_names = [
                n
                for n in zf.namelist()
                if _PurePath(n).suffix.lower() == ".exe" and "exiftool" in _PurePath(n).stem.lower()
            ]
            if not exe_names:
                logger.warning("ExifTool executable not found inside zip archive")
                return None
            exe_names.sort(key=lambda n: 0 if n.lower().endswith(wanted) else 1)

            # Extract the executable plus its bundled runtime tree (if any).
            # The exe lives one directory below the archive root
            # (<root>/exiftool(-k).exe); strip that root so the layout lands
            # as EXIFTOOL_DIR/exiftool.exe + EXIFTOOL_DIR/exiftool_files/.
            exe_entry = exe_names[0]
            root_prefix = exe_entry.rsplit("/", 1)[0] + "/"
            target = EXIFTOOL_DIR / _EXIFTOOL_EXE_NAME

            for member in zf.namelist():
                if not member.startswith(root_prefix):
                    continue
                rel = member[len(root_prefix) :]
                if not rel or rel.endswith("/"):
                    continue
                # Zip-slip guard: stay inside EXIFTOOL_DIR
                dest = EXIFTOOL_DIR / _PurePath(rel)
                try:
                    dest.relative_to(EXIFTOOL_DIR)
                except ValueError:
                    logger.warning("Skipping suspicious zip entry: %s", member)
                    continue
                if _PurePath(rel).name.lower() == _PurePath(exe_entry).name.lower():
                    dest = target
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as dst:
                    while True:
                        chunk = src.read(_DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        dst.write(chunk)

            # Mark as executable on Unix-like systems
            if sys.platform != "win32":
                target.chmod(0o755)

            logger.info("Extracted %s -> %s", exe_entry, target)
            return target if target.is_file() else None

    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("Failed to extract ExifTool zip: %s", exc)
        return None


# ---------------------------------------------------------------------------
# ExifTool extraction
# ---------------------------------------------------------------------------


def _build_exiftool_cmd() -> list[str]:
    """Build the ExifTool command list using the resolved binary path."""
    exe = _bootstrap_exiftool()
    if not exe:
        return []
    return [
        exe,
        "-json",
        "-time:all",
        "-s3",
        "-charset",
        "utf8",
    ]


def _extract_via_exiftool(file_path: Path) -> FileMetadata:
    """Run ExifTool and parse its JSON output into a FileMetadata."""
    cmd = _build_exiftool_cmd()
    if not cmd:
        return _extract_via_filesystem(file_path)

    try:
        result = subprocess.run(
            cmd + [str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            logger.warning("ExifTool returned %d for %s", result.returncode, file_path)
            return _extract_via_filesystem(file_path)

        data = json.loads(result.stdout)
        if not data:
            return _extract_via_filesystem(file_path)

        tags = data[0] if isinstance(data, list) else data

        # Extract timestamps -- ExifTool returns ISO-ish strings with -time:all
        date_taken = _parse_exif_datetime(tags.get("DateTimeOriginal")) or _parse_exif_datetime(tags.get("CreateDate"))
        date_created = _parse_exif_datetime(tags.get("CreateDate"))
        date_modified = _parse_exif_datetime(tags.get("ModifyDate"))

        stat = file_path.stat()
        fs_mtime = _ts_to_datetime(stat.st_mtime)
        return FileMetadata(
            file_path=str(file_path.resolve()),
            file_name=file_path.name,
            file_size=stat.st_size,
            extension=file_path.suffix.lower(),
            mime_type=tags.get("MIMEType"),
            date_taken=date_taken,
            date_created=date_created or fs_mtime,
            date_modified=date_modified or fs_mtime,
            exif_tags={k: str(v) for k, v in tags.items() if v},
        )
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "ExifTool failed for %s: %s -- using filesystem fallback",
            file_path,
            exc,
        )
        return _extract_via_filesystem(file_path)


# ---------------------------------------------------------------------------
# ExifTool -stay_open persistent batch session
# ---------------------------------------------------------------------------
import io as _io
import queue as _queue
import threading as _threading


class _ExifToolSession:
    """
    Persistent ExifTool process using ``-stay_open True -@ -``.

    Instead of spawning a new ``exiftool.exe`` per file (50-200 ms overhead on
    Windows per spawn), this keeps one process alive and sends file paths over
    stdin, reading JSON back from stdout.  A single lock serialises calls so it
    is safe to call from multiple threads.

    The session is lazy-started on first use and transparently restarted if
    the process dies unexpectedly.
    """

    _SENTINEL = "{ready}"

    def __init__(self) -> None:
        self._lock = _threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stdout_queue: _queue.Queue[bytes] = _queue.Queue()
        self._reader_thread: _threading.Thread | None = None

    def _read_stdout_loop(self, stdout: _io.BufferedReader, q: _queue.Queue[bytes]) -> None:
        """Daemon thread reading lines from stdout and putting them in the queue."""
        try:
            while True:
                line = stdout.readline()
                if not line:
                    break
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(b"")  # sentinel for EOF

    def _start(self) -> bool:
        """Start the persistent ExifTool process. Returns False if unavailable."""
        exe = _bootstrap_exiftool()
        if not exe:
            return False
        try:
            self._proc = subprocess.Popen(
                [
                    exe,
                    "-stay_open",
                    "True",
                    "-@",
                    "-",
                    "-common_args",
                    "-json",
                    "-time:all",
                    "-s3",
                    "-charset",
                    "utf8",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            logger.info("ExifTool stay_open session started (PID %d)", self._proc.pid)

            # Start the reader thread
            self._stdout_queue = _queue.Queue()
            self._reader_thread = _threading.Thread(
                target=self._read_stdout_loop,
                args=(self._proc.stdout, self._stdout_queue),
                daemon=True,
                name="exiftool-reader",
            )
            self._reader_thread.start()

            return True
        except OSError as exc:
            logger.warning("ExifTool stay_open failed to start: %s", exc)
            self._proc = None
            return False

    def _ensure_running(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        return self._start()

    def _send_command(self, paths: list[Path]) -> str | None:
        """Send paths to ExifTool and return raw JSON stdout, or None on failure."""
        if self._proc is None or self._proc.stdin is None:
            return None

        # Each path on its own line, terminated by -execute\n
        cmd_block = "\n".join(str(p) for p in paths) + "\n-execute\n"
        try:
            self._proc.stdin.write(cmd_block.encode("utf-8"))
            self._proc.stdin.flush()
        except OSError as exc:
            logger.warning("ExifTool stay_open stdin write failed: %s", exc)
            return None

        buf = _io.BytesIO()
        while True:
            try:
                # 10.0 seconds timeout to prevent thread deadlock
                line = self._stdout_queue.get(timeout=10.0)
            except _queue.Empty:
                logger.warning("ExifTool command timed out! Terminating hung process...")
                try:
                    if self._proc:
                        self._proc.kill()
                        self._proc.wait(timeout=2.0)
                except Exception:
                    pass
                self._proc = None
                return None

            if not line:
                return None  # EOF — process died or was terminated
            stripped = line.decode("utf-8", errors="replace").rstrip()
            if stripped == self._SENTINEL:
                break
            buf.write(line)

        return buf.getvalue().decode("utf-8", errors="replace")

    def extract_batch(self, paths: list[Path]) -> dict[str, FileMetadata]:
        """
        Extract metadata for all *paths* in a single ExifTool round-trip.

        Returns a dict mapping resolved path str -> FileMetadata.
        Falls back to filesystem extraction for any paths that fail.
        """
        if not paths:
            return {}

        with self._lock:
            if not self._ensure_running():
                return {str(p.resolve()): _extract_via_filesystem(p) for p in paths}
            raw = self._send_command(paths)
            if raw is None:
                logger.warning("ExifTool stay_open died mid-batch; will restart on next call")
                self._proc = None
                return {str(p.resolve()): _extract_via_filesystem(p) for p in paths}

        # Parse JSON outside the lock
        try:
            data = json.loads(raw) if raw.strip() else []
            if isinstance(data, dict):
                data = [data]
        except json.JSONDecodeError as exc:
            logger.warning("ExifTool batch JSON parse failed: %s", exc)
            data = []

        # ExifTool reports SourceFile for each record
        exiftool_by_path: dict[str, dict] = {}
        for record in data:
            if isinstance(record, dict):
                src = record.get("SourceFile") or ""
                exiftool_by_path[src] = record

        results: dict[str, FileMetadata] = {}
        for path in paths:
            resolved = str(path.resolve())
            tags = exiftool_by_path.get(resolved) or exiftool_by_path.get(str(path)) or {}
            if not tags:
                results[resolved] = _extract_via_filesystem(path)
                continue
            try:
                stat = path.stat()
                date_taken = _parse_exif_datetime(tags.get("DateTimeOriginal")) or _parse_exif_datetime(
                    tags.get("CreateDate")
                )
                date_created = _parse_exif_datetime(tags.get("CreateDate"))
                date_modified_tag = _parse_exif_datetime(tags.get("ModifyDate"))
                fs_mtime = _ts_to_datetime(stat.st_mtime)
                results[resolved] = FileMetadata(
                    file_path=resolved,
                    file_name=path.name,
                    file_size=stat.st_size,
                    extension=path.suffix.lower(),
                    mime_type=tags.get("MIMEType"),
                    date_taken=date_taken,
                    date_created=date_created or fs_mtime,
                    date_modified=date_modified_tag or fs_mtime,
                    exif_tags={k: str(v) for k, v in tags.items() if v},
                )
            except Exception as exc:
                logger.debug("Batch metadata parse error for %s: %s", path.name, exc)
                results[resolved] = _extract_via_filesystem(path)

        return results

    def close(self) -> None:
        """Gracefully shut down the persistent ExifTool process."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.write(b"-stay_open\nFalse\n")
                        self._proc.stdin.flush()
                        self._proc.stdin.close()
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None
            logger.info("ExifTool stay_open session closed")


# Module-level singleton — one persistent ExifTool process per backend process
_exiftool_session = _ExifToolSession()


# ---------------------------------------------------------------------------
# Filesystem fallback
# ---------------------------------------------------------------------------
def _extract_via_filesystem(file_path: Path) -> FileMetadata:
    """Derive metadata purely from the OS filesystem."""
    stat = file_path.stat()
    mtime = _ts_to_datetime(stat.st_mtime)
    # On Windows st_ctime is "creation time" (not change time) and os.utime
    # cannot set it, so we mirror mtime for date_created to keep sort stable.
    return FileMetadata(
        file_path=str(file_path.resolve()),
        file_name=file_path.name,
        file_size=stat.st_size,
        extension=file_path.suffix.lower(),
        mime_type=None,
        date_taken=None,
        date_created=mtime,
        date_modified=mtime,
        exif_tags={},
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def extract_metadata(file_path: str | Path) -> FileMetadata:
    """
    Extract metadata for a single file.

    Uses ExifTool when available; falls back to filesystem timestamps.
    For bulk directory scans, prefer ``extract_metadata_batch()`` which uses
    a persistent ExifTool session and is dramatically faster.
    """
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    if _bootstrap_exiftool():
        # Prefer the persistent stay-open session: one ~15 ms round-trip
        # instead of a ~500 ms process spawn per file on Windows. The
        # one-shot subprocess remains as fallback (session dead), then
        # filesystem timestamps.
        try:
            via_session = _exiftool_session.extract_batch([path]).get(str(path))
            if via_session is not None:
                return via_session
        except Exception:
            pass
        return _extract_via_exiftool(path)
    return _extract_via_filesystem(path)


def extract_metadata_batch(file_paths: list[Path]) -> dict[str, FileMetadata]:
    """
    Extract metadata for a list of files in a single ExifTool round-trip.

    Uses the module-level ``_ExifToolSession`` (``-stay_open True``) to keep
    ExifTool alive between calls, avoiding per-file subprocess creation overhead.

    Returns a dict mapping resolved path string -> FileMetadata.
    Falls back gracefully to filesystem extraction if ExifTool is unavailable.
    """
    if not file_paths:
        return {}

    if _bootstrap_exiftool():
        return _exiftool_session.extract_batch(file_paths)

    return {str(p.resolve()): _extract_via_filesystem(p) for p in file_paths}
