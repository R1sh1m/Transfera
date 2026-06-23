"""
Transfera v2 — Source Reader Abstraction
Unified interface for reading files from either a local filesystem or a
connected iOS device. The dispatch happens at a single boundary so that
everything downstream (scanner, cache manager, transfer engine) treats
both sources identically.

Usage:
    reader = create_source_reader(source_ref)
    # Then use reader.open(path), reader.stat(path), reader.walk(root)
    # without caring whether the source is local or a device.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from backend.api.source_types import (
    SourceRef,
    SourceRefDevice,
    SourceRefLocal,
)

logger = logging.getLogger(__name__)


class SourceFileHandle(ABC):
    """Abstract file handle for reading from any source type."""

    @abstractmethod
    async def read(self, n: int = -1) -> bytes:
        """Read up to *n* bytes. -1 reads all remaining."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the file handle."""
        ...

    @abstractmethod
    async def __aenter__(self):
        return self

    @abstractmethod
    async def __aexit__(self, *args):
        await self.close()

    @property
    @abstractmethod
    def size(self) -> int:
        """Total file size in bytes."""
        ...


class SourceReader(ABC):
    """
    Abstract interface for reading from a source location.

    Concrete implementations exist for local filesystem and iOS device.
    The rest of the codebase should depend only on this interface.
    """

    @abstractmethod
    def is_local(self) -> bool:
        """Return True if this reader is backed by the local filesystem."""
        ...

    @abstractmethod
    def is_device(self) -> bool:
        """Return True if this reader is backed by a connected device."""
        ...

    @abstractmethod
    def source_path_string(self) -> str:
        """
        Return the legacy string representation of the source.

        local_folder  -> the raw path (e.g. "C:\\Users\\...\\Photos")
        device        -> "ios://<serial>/path" format

        Used for backward compatibility with the database.
        """
        ...

    @abstractmethod
    async def open_file(self, path: str) -> SourceFileHandle:
        """
        Open a file for reading.

        Parameters
        ----------
        path : str
            For local: absolute filesystem path.
            For device: path on the device (e.g. "/DCIM/100APPLE/IMG_0001.HEIC").

        Returns
        -------
        SourceFileHandle
            An async file-like reader.
        """
        ...

    @abstractmethod
    async def stat_file(self, path: str) -> dict[str, Any]:
        """
        Return file metadata as a dict with at least:
        - "size": int (bytes)
        - "is_dir": bool
        - "mtime": float (unix timestamp)
        - "name": str
        """
        ...

    @abstractmethod
    def walk(self, root: str) -> AsyncIterator[dict[str, Any]]:
        """
        Recursively walk *root*, yielding file metadata dicts for each entry.

        Each yielded dict contains at least: name, path, is_dir, size, mtime.
        Directories should be yielded before their contents (pre-order traversal).
        """
        ...

    @abstractmethod
    async def list_directory(self, path: str) -> list[dict[str, Any]]:
        """
        List the immediate children of *path*, returning metadata dicts.

        Each dict contains at least: name, path, is_dir, size, mtime.
        """
        ...

    @abstractmethod
    def is_device_disconnected(self) -> bool:
        """
        Check if the underlying device is still connected.

        Returns False for local sources (always available).
        Returns True if the device was disconnected since the last operation,
        as detected by an exception in open_file(), stat_file(), or list_directory().

        NOTE: cache_manager.py does not go through SourceReader and instead uses
        _looks_like_disconnect() directly on raw exceptions for disconnect detection.
        This method is intended for callers that use SourceReader (e.g. scanner.py)
        to detect mid-walk disconnections.
        """
        ...


# ---------------------------------------------------------------------------
# Local filesystem implementation
# ---------------------------------------------------------------------------
class LocalFileHandle(SourceFileHandle):
    """File handle wrapping a local filesystem open file."""

    def __init__(self, fh: Any, path: str):
        self._fh = fh
        self._size = os.stat(path).st_size

    async def read(self, n: int = -1) -> bytes:
        return self._fh.read(n)

    async def close(self) -> None:
        self._fh.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    @property
    def size(self) -> int:
        return self._size


class LocalSourceReader(SourceReader):
    """Reads from a local filesystem directory."""

    def __init__(self, root_path: str):
        self._root = Path(root_path).resolve()

    def is_local(self) -> bool:
        return True

    def is_device(self) -> bool:
        return False

    def source_path_string(self) -> str:
        return str(self._root)

    async def open_file(self, path: str) -> SourceFileHandle:
        fh = open(path, "rb")
        return LocalFileHandle(fh, path)

    async def stat_file(self, path: str) -> dict[str, Any]:
        p = Path(path)
        stat = p.stat()
        return {
            "name": p.name,
            "path": str(p),
            "is_dir": p.is_dir(),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }

    async def walk(self, root: str) -> AsyncIterator[dict[str, Any]]:
        root_path = Path(root)

        def _walk_sync() -> list[dict[str, Any]]:
            entries: list[dict[str, Any]] = []
            for dirpath, dirnames, filenames in os.walk(root_path):
                for name in sorted(filenames):
                    fp = Path(dirpath) / name
                    try:
                        stat = fp.stat()
                        entries.append({
                            "name": name,
                            "path": str(fp),
                            "is_dir": False,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                        })
                    except OSError:
                        pass
                for name in sorted(dirnames):
                    dp = Path(dirpath) / name
                    entries.append({
                        "name": name,
                        "path": str(dp),
                        "is_dir": True,
                        "size": 0,
                        "mtime": 0,
                    })
            return entries

        entries = await asyncio.to_thread(_walk_sync)
        for entry in entries:
            yield entry

    async def list_directory(self, path: str) -> list[dict[str, Any]]:
        p = Path(path)
        entries = []
        for child in sorted(p.iterdir()):
            try:
                stat = child.stat()
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
            except OSError:
                pass
        return entries

    def is_device_disconnected(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# iOS device implementation
# ---------------------------------------------------------------------------
class DeviceFileHandle(SourceFileHandle):
    """File handle wrapping an AFCFileReader or BridgeFileReader for iOS devices."""

    def __init__(self, reader: Any):
        self._reader = reader

    async def read(self, n: int = -1) -> bytes:
        return await self._reader.read(n)

    async def close(self) -> None:
        await self._reader.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    @property
    def size(self) -> int:
        return self._reader.size


class DeviceSourceReader(SourceReader):
    """Reads from a connected iOS device via AFC (Tier 1) or WSL bridge (Tier 2)."""

    def __init__(self, device_id: str, device_path: str, device_name: str | None = None):
        self._device_id = device_id
        self._device_path = device_path
        self._device_name = device_name
        self._disconnected = False

    def is_local(self) -> bool:
        return False

    def is_device(self) -> bool:
        return True

    def source_path_string(self) -> str:
        path = self._device_path
        if path.startswith("/"):
            return f"ios://{self._device_id}{path}"
        return f"ios://{self._device_id}/{path}"

    async def open_file(self, path: str) -> SourceFileHandle:
        from backend.tier2_manager import get_device_manager

        manager = get_device_manager()
        tier = manager.get_device_tier(self._device_id)

        try:
            if tier is not None and tier.value == "wpd":
                # WPD backend -- streaming subprocess reader.
                from backend.config import WPD_HELPER
                from backend.wpd_backend import _WpdFileReader
                # Get file size from file_info if available.
                size = 0
                try:
                    info = await manager.get_device_file_info(self._device_id, path)
                    size = info.size
                except Exception:
                    pass
                reader = _WpdFileReader(WPD_HELPER, self._device_id, path, size=size)
                await reader.open()
            elif tier is not None and tier.value == "tier2":
                reader = manager.create_tier2_afc_reader(self._device_id, path)
                await reader.open()
            else:
                from backend.ios_device import AFCFileReader
                reader = AFCFileReader(self._device_id, path)
                await reader.open()
        except Exception:
            self._disconnected = True
            raise
        return DeviceFileHandle(reader)

    async def stat_file(self, path: str) -> dict[str, Any]:
        from backend.tier2_manager import get_device_manager

        manager = get_device_manager()
        try:
            info = await manager.get_device_file_info(self._device_id, path)
            return {
                "name": info.name,
                "path": info.path,
                "is_dir": info.is_dir,
                "size": info.size,
                "mtime": info.mtime,
            }
        except Exception:
            self._disconnected = True
            raise

    async def walk(self, root: str) -> AsyncIterator[dict[str, Any]]:
        from backend.tier2_manager import get_device_manager

        manager = get_device_manager()
        try:
            entries = await manager.browse_device(self._device_id, root)
        except Exception as exc:
            self._disconnected = True
            logger.error("Device disconnected during walk at %s: %s", root, exc)
            return

        for entry in entries:
            yield {
                "name": entry.name,
                "path": entry.path,
                "is_dir": entry.is_dir,
                "size": entry.size,
                "mtime": entry.mtime,
            }
            if entry.is_dir and entry.name not in (".", ".."):
                sub_path = f"{root.rstrip('/')}/{entry.name}"
                async for sub_entry in self.walk(sub_path):
                    yield sub_entry

    async def list_directory(self, path: str) -> list[dict[str, Any]]:
        from backend.tier2_manager import get_device_manager

        manager = get_device_manager()
        try:
            entries = await manager.browse_device(self._device_id, path)
            return [
                {
                    "name": e.name,
                    "path": e.path,
                    "is_dir": e.is_dir,
                    "size": e.size,
                    "mtime": e.mtime,
                }
                for e in entries
            ]
        except Exception:
            self._disconnected = True
            raise

    def is_device_disconnected(self) -> bool:
        return self._disconnected


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_source_reader(ref: SourceRef) -> SourceReader:
    """
    Create a SourceReader from a SourceRef.

    This is the single dispatch boundary. Everything upstream of this
    function is source-type-aware; everything downstream is not.
    """
    if isinstance(ref, SourceRefLocal):
        return LocalSourceReader(ref.path)
    elif isinstance(ref, SourceRefDevice):
        return DeviceSourceReader(
            device_id=ref.device_id,
            device_path=ref.device_path,
            device_name=ref.device_name,
        )
    else:
        raise ValueError(f"Unknown source ref type: {type(ref)}")
