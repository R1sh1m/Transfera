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
from backend.api.auth import require_local_token_or_query
from backend.config import LOCAL_SECRET_TOKEN
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


async def test_thumbnail_auth_accepts_header_or_query():
    """<img> tags can't send headers, so thumbnails also accept ?token=."""
    import pytest

    assert await require_local_token_or_query(LOCAL_SECRET_TOKEN, None) is None
    assert await require_local_token_or_query(None, LOCAL_SECRET_TOKEN) is None
    with pytest.raises(Exception) as exc_info:
        await require_local_token_or_query(None, None)
    assert exc_info.value.status_code == 403
    with pytest.raises(Exception) as exc_info:
        await require_local_token_or_query("wrong", "also-wrong")
    assert exc_info.value.status_code == 403


def test_ios_thumbnail_rejects_bad_token(test_client):
    r = test_client.get(
        "/api/device/ios-thumbnail",
        params={"device_id": "SERIAL-123", "path": "/DCIM/x.jpg", "token": "wrong"},
    )
    assert r.status_code == 403


def test_ios_thumbnail_accepts_query_token(test_client):
    # Unknown device + valid ?token= passes auth and degrades to the gray
    # fallback (200) instead of 403 — no hardware needed.
    r = test_client.get(
        "/api/device/ios-thumbnail",
        params={"device_id": "NOPE", "path": "/DCIM/x.jpg", "token": LOCAL_SECRET_TOKEN},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_local_thumbnail_query_token(test_client):
    bad = test_client.get("/api/device/thumbnail", params={"path": "/nope.jpg", "token": "wrong"})
    assert bad.status_code == 403
    good = test_client.get("/api/device/thumbnail", params={"path": "/nope.jpg", "token": LOCAL_SECRET_TOKEN})
    assert good.status_code == 200


def test_disk_thumb_key_stable_and_sensitive():
    from backend.api import device_preview as _dp

    k1 = _dp._device_thumb_disk_key("dev", "/DCIM/a.jpg", 200, 100, 1.0)
    assert k1 == _dp._device_thumb_disk_key("dev", "/DCIM/a.jpg", 200, 100, 1.0)
    assert k1 != _dp._device_thumb_disk_key("dev", "/DCIM/a.jpg", 200, 101, 1.0)
    assert k1 != _dp._device_thumb_disk_key("dev", "/DCIM/a.jpg", 200, 100, 2.0)
    assert k1 != _dp._device_thumb_disk_key("dev", "/DCIM/b.jpg", 200, 100, 1.0)


def test_disk_thumb_roundtrip_and_sweep(tmp_path, monkeypatch):
    import backend.config as _config
    from backend.api import device_preview as _dp

    monkeypatch.setattr(_config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(_dp, "_DEVICE_THUMB_DISK_MAX_FILES", 3)
    assert _dp._read_disk_thumb("nope.jpg") is None
    payload = b"\xff\xd8" + b"x" * 100
    _dp._write_disk_thumb("a.jpg", payload)
    assert _dp._read_disk_thumb("a.jpg") == payload
    # Over the file cap -> oldest evicted, newest kept.
    _dp._write_disk_thumb("b.jpg", payload)
    _dp._write_disk_thumb("c.jpg", payload)
    _dp._write_disk_thumb("d.jpg", payload)
    assert _dp._read_disk_thumb("a.jpg") is None
    assert _dp._read_disk_thumb("d.jpg") == payload


def test_extract_frame_garbage_returns_none():
    from backend.api import device_preview as _dp

    assert _dp._extract_frame_from_bytes(b"", 200, ".mp4") is None
    assert _dp._extract_frame_from_bytes(b"\x00" * 50, 200, ".mp4") is None
    # Random bytes are not a video container (ffmpeg may be absent too).
    assert _dp._extract_frame_from_bytes(bytes(range(256)) * 40, 200, ".mp4") is None


def test_embedded_thumbnail_no_exiftool(monkeypatch):
    import backend.engines.metadata_extractor as _me

    monkeypatch.setattr(_me, "_bootstrap_exiftool", lambda: None)
    # Binary session start fails without an exe -> None, no spawn attempted.
    assert _me.extract_embedded_thumbnail_bytes("whatever.jpg") is None
