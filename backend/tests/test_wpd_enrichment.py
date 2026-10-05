"""
Tests for WPD lockdown enrichment (no hardware required).

WPD exposes no iOS metadata, so ``WpdBackend.list_devices()`` opportunistically
fills name/model/ios_version from usbmux lockdown ``short_info``, matched by
the UDID embedded in the WPD PnP device ID. Enrichment must never fail the
listing: no driver, untrusted phone, or no match all keep "unknown".
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import backend.wpd_backend as wpd_backend
from backend.wpd_backend import WpdBackend

_UDID = "A" * 40
_WPD_ID = "\\\\?\\USB#VID_05AC&PID_12A8#" + _UDID + "#{36fc9e60-c465-11cf-8056-444553540000}"

_LOCKDOWN_INFO = {
    _UDID.lower(): {"name": "Rishi's iPhone", "model": "iPhone17,2", "ios_version": "18.4"},
}


def _helper_payload(device_id: str = _WPD_ID) -> tuple[bytes, bytes]:
    rows = [{"device_id": device_id, "friendly_name": "Apple iPhone", "manufacturer": "Apple Inc."}]
    return json.dumps(rows).encode(), b""


def _make_backend(monkeypatch, payload: tuple[bytes, bytes]) -> WpdBackend:
    backend = WpdBackend(wpd_helper_path="C:\\nonexistent\\wpd_helper.exe")

    async def _fake_run(self, args, timeout=30):
        return payload

    monkeypatch.setattr(WpdBackend, "_run", _fake_run)
    return backend


def _mock_versions(monkeypatch, value) -> AsyncMock:
    mock = AsyncMock(return_value=value)
    monkeypatch.setattr(wpd_backend, "query_lockdown_versions", mock)
    return mock


async def test_enrichment_fills_version_on_udid_match(monkeypatch):
    backend = _make_backend(monkeypatch, _helper_payload())
    _mock_versions(monkeypatch, dict(_LOCKDOWN_INFO))
    (dev,) = await backend.list_devices()
    assert dev.ios_version == "18.4"
    assert dev.name == "Rishi's iPhone"
    assert dev.model == "iPhone17,2"
    # Identity keys for browse/read paths are untouched.
    assert dev.serial == _WPD_ID
    assert dev.status.value == "ready"


async def test_enrichment_match_is_case_insensitive(monkeypatch):
    backend = _make_backend(monkeypatch, _helper_payload(_WPD_ID.lower()))
    _mock_versions(monkeypatch, {_UDID.upper(): _LOCKDOWN_INFO[_UDID.lower()]})
    (dev,) = await backend.list_devices()
    assert dev.ios_version == "18.4"


async def test_enrichment_keeps_unknown_without_lockdown_match(monkeypatch):
    backend = _make_backend(monkeypatch, _helper_payload())
    _mock_versions(monkeypatch, {})
    (dev,) = await backend.list_devices()
    assert dev.ios_version == "unknown"
    assert dev.name == "Apple iPhone"
    assert dev.model == "Apple Inc."


async def test_enrichment_never_raises(monkeypatch):
    backend = _make_backend(monkeypatch, _helper_payload())
    mock = AsyncMock(side_effect=RuntimeError("usbmux exploded"))
    monkeypatch.setattr(wpd_backend, "query_lockdown_versions", mock)
    (dev,) = await backend.list_devices()
    assert dev.ios_version == "unknown"


async def test_enrichment_skips_ids_without_udid(monkeypatch):
    backend = _make_backend(monkeypatch, _helper_payload("USBSTOR\\DISK&VEN_SANDISK&PROD_ULTRA"))
    mock = _mock_versions(monkeypatch, dict(_LOCKDOWN_INFO))
    (dev,) = await backend.list_devices()
    assert dev.ios_version == "unknown"
    assert dev.model == "Apple Inc."
    mock.assert_awaited_once()
