"""
Transfera v2 -- WPD Backend (Windows Portable Devices)
Third DeviceBackend implementation that talks to any MTP/WPD device
( iPhone, Android, cameras, etc. ) via the wpd_helper.exe subprocess.

This backend is device-agnostic -- it doesn't require Apple drivers or
pymobiledevice3. It uses the WPD COM API through a small native helper
that is invoked as a subprocess per call.

The wpd_helper.exe path is injectable so packaging can wire in the
correct location without touching this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

creationflags = 0x08000000 if sys.platform == "win32" else 0

from backend.ios_device import DeviceFileInfo, DeviceStatus, IOSDevice

logger = logging.getLogger(__name__)

# Default chunk size for streaming reads -- matches BATCH_SIZE * 1024
# used by the cache manager (BATCH_SIZE=100 => 100 KB).
_DEFAULT_CHUNK_SIZE = 100 * 1024

# Subprocess timeouts (seconds)
_LIST_TIMEOUT = 10
_BROWSE_TIMEOUT = 15


class WpdError(RuntimeError):
    """Error originating from the WPD subprocess."""

    def __init__(self, category: str, message: str, hresult: str | None = None):
        self.category = category
        self.hresult = hresult
        super().__init__(f"[{category}] {message}" + (f" (HRESULT: {hresult})" if hresult else ""))


# ---------------------------------------------------------------------------
# Streaming file reader -- constant memory, subprocess pipe
# ---------------------------------------------------------------------------
class _WpdFileReader:
    """
    Async file reader backed by a wpd_helper.exe read-file subprocess.

    Reads from the subprocess stdout pipe in chunks so that memory
    usage stays constant regardless of file size.  The subprocess is
    launched on open() and terminated on close().
    """

    def __init__(
        self,
        wpd_helper: Path,
        device_id: str,
        path: str,
        size: int = 0,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ):
        self._wpd_helper = wpd_helper
        self._device_id = device_id
        self._path = path
        self._chunk_size = chunk_size

        self._proc: asyncio.subprocess.Process | None = None
        self._size = size
        self._pos = 0
        self._closed = False

    async def open(self) -> _WpdFileReader:
        """Launch the wpd_helper read-file subprocess."""
        self._proc = await asyncio.create_subprocess_exec(
            str(self._wpd_helper),
            "read-file",
            "--device", self._device_id,
            "--path", self._path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        return self

    async def read(self, n: int = -1) -> bytes:
        """
        Read up to *n* bytes from the subprocess stdout pipe.

        n=-1  : read all remaining (stream until EOF).
        n=0   : return b"" (consistent with AFCFileReader).
        n>0   : read exactly n bytes (may return fewer at EOF).
        """
        if self._proc is None or self._proc.stdout is None:
            return b""
        if self._proc.stdout.at_eof():
            return b""

        if n == -1:
            # Stream all remaining data in chunks.
            chunks: list[bytes] = []
            while True:
                chunk = await self._proc.stdout.read(self._chunk_size)
                if not chunk:
                    break
                chunks.append(chunk)
                self._pos += len(chunk)
            return b"".join(chunks)

        if n <= 0:
            return b""

        # Read exactly n bytes, buffering intermediate chunks.
        result = bytearray()
        while len(result) < n:
            remaining = n - len(result)
            chunk = await self._proc.stdout.read(min(remaining, self._chunk_size))
            if not chunk:
                break
            result.extend(chunk)
            self._pos += len(chunk)
        return bytes(result)

    async def close(self) -> None:
        """Terminate the subprocess and drain remaining pipes."""
        if self._closed:
            return
        self._closed = True

        if self._proc is None:
            return

        try:
            # If the process is still running (caller didn't read all data),
            # terminate it to avoid leaving orphans.
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3.0)
                except TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()

            # Check stderr for error JSON after stdout is consumed.
            if self._proc.stderr is not None:
                stderr_bytes = await self._proc.stderr.read()
                if stderr_bytes:
                    self._check_stderr(stderr_bytes)
        except Exception:
            pass
        finally:
            self._proc = None

    def _check_stderr(self, stderr_bytes: bytes) -> None:
        """Parse wpd_helper error JSON from stderr and raise WpdError."""
        try:
            text = stderr_bytes.decode("utf-8", errors="replace").strip()
            if not text:
                return
            err = json.loads(text)
            if "error" in err:
                raise WpdError(
                    category=err["error"],
                    message=err.get("message", "Unknown error"),
                    hresult=err.get("hresult"),
                )
        except WpdError:
            raise  # re-raise structured errors from wpd_helper
        except (json.JSONDecodeError, Exception):
            pass  # non-JSON stderr (COM debug output) is silently ignored

    async def __aenter__(self) -> _WpdFileReader:
        return await self.open()

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    @property
    def size(self) -> int:
        return self._size

    @property
    def position(self) -> int:
        return self._pos


# ---------------------------------------------------------------------------
# WPD Backend
# ---------------------------------------------------------------------------
class WpdBackend:
    """
    DeviceBackend implementation backed by wpd_helper.exe (WPD COM API).

    Device-agnostic: works with any MTP/WPD device, not just iOS.
    The wpd_helper.exe path is injectable for packaging flexibility.
    """

    def __init__(self, wpd_helper_path: Path | str | None = None):
        if wpd_helper_path is not None:
            self._wpd_helper = Path(wpd_helper_path)
        else:
            # Lazy import to avoid circular imports at module load time.
            from backend.config import WPD_HELPER
            self._wpd_helper = WPD_HELPER

    @property
    def tier(self):  # type: ignore[override]
        from backend.device_backend import DeviceAccessTier
        return DeviceAccessTier.WPD

    @property
    def is_configured(self) -> bool:
        return self._wpd_helper.exists()

    async def is_available(self):  # type: ignore[override]
        from backend.device_backend import DeviceAccessTier, TierProbeResult
        if not self._wpd_helper.exists():
            return TierProbeResult(
                tier=DeviceAccessTier.WPD,
                available=False,
                error=f"wpd_helper.exe not found at {self._wpd_helper}",
            )
        # Quick smoke test -- list-devices should run without crashing.
        try:
            stdout, _ = await self._run(["list-devices"], timeout=_LIST_TIMEOUT)
            # If we get here and stdout contains valid JSON, we're good.
            json.loads(stdout)
            return TierProbeResult(tier=DeviceAccessTier.WPD, available=True)
        except Exception as exc:
            return TierProbeResult(
                tier=DeviceAccessTier.WPD,
                available=False,
                error=f"wpd_helper smoke test failed: {exc}",
            )

    async def list_devices(self) -> list[IOSDevice]:  # type: ignore[override]
        stdout, _ = await self._run(["list-devices"], timeout=_LIST_TIMEOUT)
        devices_raw = json.loads(stdout)

        devices: list[IOSDevice] = []
        for d in devices_raw:
            device_id = d.get("device_id", "")
            friendly_name = d.get("friendly_name") or "Unknown Device"
            manufacturer = d.get("manufacturer") or ""

            # WPD doesn't provide iOS-specific metadata.  Map what we have
            # into the IOSDevice shape so callers can't tell which backend
            # produced it without checking explicitly.
            devices.append(IOSDevice(
                serial=device_id,
                name=friendly_name,
                model=manufacturer or "Unknown",
                ios_version="unknown",
                connection_type="USB",
                status=DeviceStatus.READY,
            ))
        return devices

    async def browse(self, serial: str, path: str) -> list[DeviceFileInfo]:  # type: ignore[override]
        # Normalize path: strip leading slash -- WPD helper expects "DCIM" not "/DCIM".
        clean_path = path.strip("/")
        if not clean_path:
            clean_path = "."

        args = ["list-folder", "--device", serial, "--path", clean_path]
        stdout, _ = await self._run(args, timeout=_BROWSE_TIMEOUT)
        entries_raw = json.loads(stdout)

        # Transparent container walk at root: some WPD devices (iPhones) expose
        # an intermediate storage object (e.g. "Internal Storage") between the
        # WPD root and the real filesystem.  This container reports as
        # type "file" because its WPD content type is FUNCTIONAL_OBJECT (not
        # FOLDER), but even when IsFolder is fixed to recognise functional
        # objects the user-facing path space should skip this intermediate layer
        # so that WPD browsing paths match AFC/Tier-1 conventions (e.g.
        # "/DCIM/…" not "/Internal Storage/DCIM/…").
        #
        # The rule: for every root entry that is NOT itself a real content folder
        # (heuristic: try listing its children; if it has children and every
        # child is itself a container — i.e. none are media files — this is an
        # intermediate container), flatten it by including its grandchildren.
        if clean_path == ".":
            flat: list[dict] = []
            for e in entries_raw:
                name = e.get("name", "")
                if name == "DCIM" or name == "Photos":
                    flat.append(e)
                    continue
                try:
                    args = ["list-folder", "--device", serial, "--path", name]
                    child_stdout, _ = await self._run(args, timeout=_BROWSE_TIMEOUT)
                    child_raw = json.loads(child_stdout)
                    if child_raw:
                        flat.extend(child_raw)
                        continue
                except WpdError:
                    pass
                flat.append(e)
            entries_raw = flat

        # -- build the response entries --------------------------------------
        # The frontend builds ios://SERIAL/PATH strings from entry.path, and the
        # actual WPD helper call that reads/resolves a file uses the same path
        # format.  Because the transparent walk above skipped any intermediate
        # storage containers, paths here start with "/" immediately followed by
        # the real filesystem folder (e.g. "/DCIM/…") — which matches AFC/Tier-1
        # convention.
        entries: list[DeviceFileInfo] = []
        for e in entries_raw:
            name = e.get("name", "")
            is_dir = e.get("type") == "folder"
            size = e.get("size") if not is_dir else 0
            mtime = self._parse_wpd_date(e.get("date_modified", ""))

            full_path = f"/{clean_path}/{name}" if clean_path != "." else f"/{name}"

            entries.append(DeviceFileInfo(
                name=name,
                path=full_path,
                is_dir=is_dir,
                size=int(size) if size is not None else 0,
                mtime=mtime,
            ))
        return entries

    async def file_info(self, serial: str, path: str) -> DeviceFileInfo:  # type: ignore[override]
        # WPD helper doesn't have a single-file info command.  Browse the
        # parent directory and find the matching entry.
        clean_path = path.strip("/")
        parent = os.path.dirname(clean_path) or "."
        child_name = os.path.basename(clean_path)

        entries = await self.browse(serial, parent)
        for entry in entries:
            if entry.name == child_name:
                return entry

        raise WpdError("path_not_found", f"File not found: {path}")

    async def read_file(self, serial: str, path: str) -> bytes:  # type: ignore[override]
        # Read the entire file into memory (for small files / backward compat).
        # For large files the caller should use create_file_reader() instead.
        reader = _WpdFileReader(self._wpd_helper, serial, path)
        try:
            await reader.open()
            return await reader.read(-1)
        finally:
            await reader.close()

    def create_file_reader(self, serial: str, path: str, size: int = 0):  # type: ignore[override]
        """
        Create a streaming async file reader backed by a wpd_helper subprocess.

        Parameters
        ----------
        serial : str
            WPD device ID (PnP device string).
        path : str
            Virtual path on the device (e.g. "DCIM/100APPLE/IMG_0001.JPG").
        size : int
            File size in bytes, if known from a prior browse/file_info call.
            Used to satisfy the ``size`` property on the reader.  Pass 0
            if unknown -- the reader will still stream correctly.
        """
        return _WpdFileReader(self._wpd_helper, serial, path, size=size)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _run(
        self,
        args: list[str],
        timeout: float = 30,
    ) -> tuple[bytes, bytes]:
        """Run wpd_helper.exe with *args*, return (stdout, stderr) bytes."""
        cmd = [str(self._wpd_helper)] + args
        logger.debug("WpdBackend: running %s", " ".join(cmd))

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            # Kill the hung process so it doesn't block the app.
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            raise WpdError("timeout", f"wpd_helper timed out after {timeout}s")

        if proc.returncode != 0:
            # Try to parse error JSON from stderr.
            stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
            if stderr_text:
                try:
                    err = json.loads(stderr_text)
                    raise WpdError(
                        category=err.get("error", "subprocess_error"),
                        message=err.get("message", stderr_text),
                        hresult=err.get("hresult"),
                    )
                except json.JSONDecodeError:
                    pass
            raise WpdError(
                "subprocess_error",
                f"wpd_helper exited with code {proc.returncode}: {stderr_text.strip()}",
            )

        return stdout, stderr

    @staticmethod
    def _parse_wpd_date(date_str: str) -> float:
        """
        Parse an ISO 8601 date string from WPD into a Unix timestamp.

        Handles common formats:
        - "2024-01-15T10:30:00"
        - "2024-01-15T10:30:00Z"
        - "2024-01-15T10:30:00.0000000"
        - "2024-01-15T10:30:00+00:00"

        Returns 0.0 on parse failure.
        """
        if not date_str:
            return 0.0
        try:
            # Normalize: strip trailing 'Z', truncate fractional seconds.
            normalized = date_str.rstrip("Z")

            # Handle fractional seconds (Python's fromisoformat doesn't like
            # 7-digit fractional seconds from WPD).
            if "." in normalized:
                base, frac = normalized.split(".", 1)
                # Truncate to 6 digits (microsecond precision).
                frac = frac[:6].ljust(6, "0")
                normalized = f"{base}.{frac}"

            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except (ValueError, TypeError):
            logger.debug("WpdBackend: failed to parse date %r", date_str)
            return 0.0
