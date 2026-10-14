"""
Tests for per-device persistent driver picker with guaranteed fallback.

Covers: user override persists + reloads, auto success never clobbers the
override, fallback order is [user_tier] + rest, retry purges caches.
Fast, no real USB.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.device_backend as device_backend
from backend.device_backend import (
    DeviceAccessTier,
    DeviceBackendManager,
    DeviceFileInfo,
    TierProbeResult,
)
from backend.ios_device import DeviceStatus, IOSDevice


def _dev(serial: str) -> IOSDevice:
    return IOSDevice(
        serial=serial,
        name="Test iPhone",
        model="iPhone",
        ios_version="17.0",
        connection_type="USB",
        status=DeviceStatus.READY,
    )


class _FakeBackend:
    def __init__(self, tier: DeviceAccessTier, configured: bool = True, available: bool = True):
        self._tier = tier
        self.is_configured = configured
        self._available = available
        self.browse = AsyncMock(
            return_value=[DeviceFileInfo(name="a.jpg", path="/DCIM/a.jpg", is_dir=False, size=10, mtime=0)]
        )
        self.list_devices = AsyncMock(return_value=[])

    @property
    def tier(self):
        return self._tier

    async def is_available(self):
        return TierProbeResult(tier=self._tier, available=self._available, error=None if self._available else "down")

    async def file_info(self, serial, path):
        return DeviceFileInfo(name="a.jpg", path=path, is_dir=False, size=10, mtime=0)

    async def read_file(self, serial, path):
        return b"x"

    async def read_partial(self, serial, path, max_bytes):
        return b"x"


def _make_manager(monkeypatch=None, prefer_tier2: bool = False):
    m = DeviceBackendManager.__new__(DeviceBackendManager)
    m._device_tier_prefs = {}
    m._device_tier_map = {}
    m._device_tier_user_prefs = {}
    m._prefer_tier2 = prefer_tier2
    m._wsl_orchestrator = None
    m._ios_serials = set()
    m._apple_driver_installable = False
    m._apple_driver_package_name = None
    m._apple_driver_package_version = None
    m._bridge_auto_started = False
    m._wsl_setup_suggested = False
    m._tier2_error = None
    m._tier1 = cast(Any, _FakeBackend(DeviceAccessTier.TIER_1))
    m._tier2 = cast(Any, _FakeBackend(DeviceAccessTier.TIER_2))
    m._wpd = cast(Any, _FakeBackend(DeviceAccessTier.WPD))
    if monkeypatch is not None:
        monkeypatch.setattr(device_backend, "_save_device_tier_prefs", lambda _p: None)
        monkeypatch.setattr(device_backend, "_save_device_tier_user_prefs", lambda _p: None)
    return m


def test_set_user_tier_persists_and_reloads(monkeypatch, tmp_path):
    import json

    user_file = tmp_path / "user.json"
    auto_file = tmp_path / "auto.json"
    monkeypatch.setattr(device_backend, "_DEVICE_TIER_USER_FILE", user_file)
    monkeypatch.setattr(device_backend, "_DEVICE_TIER_FILE", auto_file)
    m = _make_manager()
    # don't stub saves here: let real save run against tmp files
    monkeypatch.setattr(device_backend, "_save_device_tier_prefs", lambda _p: None)
    ckey = m.set_user_tier("ABC123", "tier2")
    assert m.get_user_tier("ABC123") == "tier2"
    assert user_file.exists()
    data = json.loads(user_file.read_text(encoding="utf-8"))
    assert data[ckey] == "tier2"
    # reload from disk
    m2 = _make_manager()
    m2._device_tier_user_prefs = device_backend._load_device_tier_user_prefs()
    assert m2.get_user_tier("ABC123") == "tier2"
    # auto clears
    m.set_user_tier("ABC123", "auto")
    assert m.get_user_tier("ABC123") is None


def test_invalid_user_tier_rejected():
    m = _make_manager()
    with pytest.raises(ValueError):
        m.set_user_tier("ABC", "tier9")


def test_auto_success_does_not_clobber_user_override(monkeypatch):
    m = _make_manager(monkeypatch)
    m.set_user_tier("DEV1", "tier2")
    # simulate success via tier1 fallback path writing auto prefs
    m._device_tier_map["DEV1"] = DeviceAccessTier.TIER_1
    m._device_tier_prefs["DEV1"] = "tier1"
    assert m.get_user_tier("DEV1") == "tier2"


@pytest.mark.asyncio
async def test_fallback_order_user_first(monkeypatch):
    m = _make_manager(monkeypatch)
    m.set_user_tier("DEV1", "tier2")
    order = m._waterfall_order(is_ios_query=False, serial="DEV1")
    assert [b.tier for b in order][0] == DeviceAccessTier.TIER_2
    assert m._resolve_backend("DEV1").tier == DeviceAccessTier.TIER_2
    # rest still present for fallback
    assert {b.tier for b in order} == {DeviceAccessTier.TIER_1, DeviceAccessTier.WPD, DeviceAccessTier.TIER_2}


@pytest.mark.asyncio
async def test_run_operation_falls_back_and_records_attempted(monkeypatch):
    m = _make_manager(monkeypatch)
    m.set_user_tier("DEV1", "tier2")
    # user tier fails, tier1 succeeds
    m._tier2.browse = AsyncMock(side_effect=RuntimeError("bridge down"))

    async def _browse(backend, p):
        return await backend.browse("DEV1", p)

    result = await m._run_operation("DEV1", "browse", _browse, "/DCIM")
    assert len(result) == 1
    # user override untouched, auto map updated
    assert m.get_user_tier("DEV1") == "tier2"
    assert (m._device_tier_prefs.get("dev1") or m._device_tier_prefs.get("DEV1")) == "tier1"


@pytest.mark.asyncio
async def test_run_operation_terminal_does_not_fallback(monkeypatch):
    from backend.device_backend import DeviceLockedError

    m = _make_manager(monkeypatch)
    m._tier1.browse = AsyncMock(side_effect=DeviceLockedError("D", "locked"))
    m._tier2.browse = AsyncMock(return_value=[])

    async def _browse(backend, p):
        return await backend.browse("D", p)

    with pytest.raises(DeviceLockedError):
        await m._run_operation("D", "browse", _browse, "/DCIM")
    # tier2 never attempted after terminal
    m._tier2.browse.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_purges_caches(monkeypatch):
    from backend.api import device_preview as preview_mod

    # seed listing cache with keys for the serial
    key = ("DEV9", "/DCIM", True, "newest")
    with preview_mod._listing_cache_lock:
        preview_mod._listing_cache[key] = preview_mod._ListingCacheEntry([], 0, 0, 0, None, 0.0)
    n = preview_mod.purge_listing_cache_for_serial("DEV9")
    assert n >= 1
    with preview_mod._listing_cache_lock:
        assert key not in preview_mod._listing_cache


@pytest.mark.asyncio
async def test_get_available_tiers_shape(monkeypatch):
    m = _make_manager(monkeypatch)
    out = await m.get_available_tiers()
    assert set(out) >= {"tier1", "wpd", "tier2"}
    for v in out.values():
        assert "available" in v and "error" in v


@pytest.mark.asyncio
async def test_one_shot_override_does_not_mutate_user_prefs(monkeypatch):
    m = _make_manager(monkeypatch)
    # No stored override; one-shot reorder must not write user prefs.
    order = m._waterfall_order(is_ios_query=False, serial="DEV1", override_tier="tier2")
    assert [b.tier for b in order][0] == DeviceAccessTier.TIER_2
    assert m.get_user_tier("DEV1") is None
    assert m._device_tier_user_prefs == {}
    assert m._resolve_backend("DEV1", override_tier="tier2").tier == DeviceAccessTier.TIER_2
    assert m.get_user_tier("DEV1") is None

    async def _browse(backend, p):
        return await backend.browse("DEV1", p)

    await m._run_operation("DEV1", "browse", _browse, "/DCIM", override_tier="tier2")
    assert m.get_user_tier("DEV1") is None
    assert m._device_tier_user_prefs == {}

    # One-shot wins over a stored override without clobbering it.
    m.set_user_tier("DEV1", "tier1")
    assert m._resolve_backend("DEV1", override_tier="tier2").tier == DeviceAccessTier.TIER_2
    assert m.get_user_tier("DEV1") == "tier1"


@pytest.mark.asyncio
async def test_wpd_on_ios_returns_warning(monkeypatch):
    from backend.api import routes as routes_mod
    from backend.api.schemas import DeviceTierPreferenceRequest

    m = _make_manager(monkeypatch)
    monkeypatch.setattr(routes_mod, "get_device_backend_manager", lambda: m)
    ios_serial = "00008101-001A2B3C"  # modern iOS UDID shape
    assert device_backend.is_ios_serial(ios_serial)
    resp = await routes_mod.set_preferred_tier(DeviceTierPreferenceRequest(serial=ios_serial, tier="wpd"), None)
    assert resp.tier == "wpd"
    assert resp.warning is not None and "DCIM-only" in resp.warning
    # Non-iOS serial gets no warning.
    resp2 = await routes_mod.set_preferred_tier(DeviceTierPreferenceRequest(serial="ANDROID123", tier="wpd"), None)
    assert resp2.warning is None
