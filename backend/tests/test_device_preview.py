"""
Tests for GET /api/device/ios-preview (no hardware required).

Covers the two preview-dead-end fixes:
  * HEIF parity — ``.heif`` files are listed as photos (the scanner imports
    them, so the preview must show them);
  * error surfacing — listing failures return a JSON body naming the folder
    and the reason (the frontend renders this instead of a silent empty
    grid), rather than an empty 200.
"""

from __future__ import annotations

from types import SimpleNamespace

import backend.api.device_preview as device_preview
from backend.ios_device import DeviceStatus, IOSDevice


def _device(serial: str = "SERIAL-123") -> IOSDevice:
    return IOSDevice(
        serial=serial,
        name="iPhone",
        model="iPhone17,2",
        ios_version="18.4",
        connection_type="USB",
        status=DeviceStatus.READY,
    )


def _entry(name: str, is_dir: bool = False, size: int = 1000) -> SimpleNamespace:
    return SimpleNamespace(name=name, is_dir=is_dir, size=size, mtime=1700000000.0)


class _Manager:
    def __init__(self, devices, entries=None, error=None):
        self._devices = devices
        self._entries = entries if entries is not None else []
        self._error = error

    async def list_devices(self):
        return self._devices, "wpd"

    async def browse_device(self, device_id, path):
        if self._error is not None:
            raise self._error
        return self._entries


def _patch_manager(monkeypatch, manager) -> None:
    monkeypatch.setattr(device_preview, "get_device_manager", lambda: manager)


def test_heif_files_listed_as_photos(test_client, monkeypatch):
    entries = [
        _entry("IMG_0001.heif"),
        _entry("IMG_0002.HEIC"),
        _entry("notes.aae"),
        _entry("100APPLE", is_dir=True),
    ]
    _patch_manager(monkeypatch, _Manager([_device()], entries))
    r = test_client.get("/api/device/ios-preview", params={"device_id": "SERIAL-123", "path": "/DCIM"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert body["photos"] == 2
    assert {i["filename"] for i in body["items"]} == {"IMG_0001.heif", "IMG_0002.HEIC"}
    assert all(i["type"] == "photo" for i in body["items"])


def test_browse_failure_names_folder_and_reason(test_client, monkeypatch):
    _patch_manager(monkeypatch, _Manager([_device()], error=RuntimeError("[path_not_found] object not found")))
    r = test_client.get("/api/device/ios-preview", params={"device_id": "SERIAL-123", "path": "/DCIM/202207__"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["status"] == "error"
    assert "/DCIM/202207__" in detail["message"]
    assert "path_not_found" in detail["message"]


def test_missing_folder_returns_not_found(test_client, monkeypatch):
    _patch_manager(monkeypatch, _Manager([_device()], error=FileNotFoundError("/DCIM/nope")))
    r = test_client.get("/api/device/ios-preview", params={"device_id": "SERIAL-123", "path": "/DCIM/nope"})
    assert r.status_code == 404
    assert r.json()["detail"]["status"] == "not_found"


def test_unknown_device_returns_disconnected(test_client, monkeypatch):
    _patch_manager(monkeypatch, _Manager([]))
    r = test_client.get("/api/device/ios-preview", params={"device_id": "GONE", "path": "/DCIM"})
    assert r.status_code == 404
    assert r.json()["detail"]["status"] == "disconnected"
