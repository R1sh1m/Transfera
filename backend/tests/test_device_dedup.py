"""
Tests for same-phone dedup across tiers (no hardware required).

One physical iPhone can surface with two rows when its WPD PnP id does not
embed the UDID (SWD#WPDBUSENUM form): a Tier 1 row keyed by UDID plus a WPD
row keyed by PnP path. list_devices() must collapse those to the Tier 1
row — Tier 1/2 supersede WPD for the same device.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import backend.device_backend as device_backend
from backend.device_backend import (
    DeviceAccessTier,
    DeviceBackendManager,
    TierProbeResult,
)
from backend.ios_device import DeviceStatus, IOSDevice

_UDID_A = "A" * 40
_UDID_B = "B" * 40


def _tier1_row(serial=_UDID_A, status=DeviceStatus.READY, model="iPhone 8 Plus") -> IOSDevice:
    return IOSDevice(
        serial=serial,
        name="Rishi's iPhone",
        model=model,
        ios_version="16.7.16",
        connection_type="USB",
        status=status,
    )


def _wpd_row(device_id: str) -> IOSDevice:
    return IOSDevice(
        serial=device_id,
        name="Apple iPhone",
        model="Apple Inc.",
        ios_version="unknown",
        connection_type="USB",
        status=DeviceStatus.READY,
    )


class _FakeBackend:
    """Configurable stub backend for list_devices() tests."""

    def __init__(self, tier: DeviceAccessTier, devices: list[IOSDevice], configured: bool = True):
        self._tier = tier
        self.is_configured = configured
        self.list_devices = AsyncMock(return_value=list(devices))

    @property
    def tier(self):
        return self._tier

    async def is_available(self):
        return TierProbeResult(tier=self._tier, available=True)


def _make_manager(tier1_devices, wpd_devices) -> DeviceBackendManager:
    manager = DeviceBackendManager.__new__(DeviceBackendManager)
    manager._device_tier_prefs = {}
    manager._device_tier_map = {}
    manager._prefer_tier2 = False
    manager._wsl_orchestrator = None
    manager._ios_serials = set()
    manager._apple_driver_installable = False
    manager._apple_driver_package_name = None
    manager._apple_driver_package_version = None
    manager._bridge_auto_started = False
    manager._wsl_setup_suggested = False
    manager._tier2_error = None
    manager._tier1 = _FakeBackend(DeviceAccessTier.TIER_1, tier1_devices)
    manager._tier2 = _FakeBackend(DeviceAccessTier.TIER_2, [], configured=False)
    manager._wpd = _FakeBackend(DeviceAccessTier.WPD, wpd_devices)
    return manager


async def test_wpd_row_without_udid_collapses_to_tier1(monkeypatch):
    """SWD-form WPD id + READY Tier 1 row for the same phone -> one row."""
    monkeypatch.setattr(device_backend, "_save_device_tier_prefs", lambda _prefs: None)
    manager = _make_manager(
        [_tier1_row()],
        [_wpd_row(r"SWD#WPDBUSENUM#{0107123a-3f5e-4c9a-8b2d-1e6f5a9b0c3d}#0000000000000000")],
    )
    devices, _tier = await manager.list_devices()
    assert [d.serial for d in devices] == [_UDID_A]
    assert manager.get_device_tier(_UDID_A) == DeviceAccessTier.TIER_1


async def test_wpd_row_with_udid_collapses_to_tier1(monkeypatch):
    """PnP id embedding the UDID is an exact shadow -> one row."""
    monkeypatch.setattr(device_backend, "_save_device_tier_prefs", lambda _prefs: None)
    manager = _make_manager(
        [_tier1_row()],
        [_wpd_row(f"\\\\?\\USB#VID_05AC&PID_12A8#{_UDID_A}#{{36fc9e60-c465-11cf-8056-444553540000}}")],
    )
    devices, _tier = await manager.list_devices()
    assert [d.serial for d in devices] == [_UDID_A]


async def test_wpd_row_kept_without_tier1(monkeypatch):
    """No Tier 1 row -> the WPD fallback row is kept (usable path)."""
    monkeypatch.setattr(device_backend, "_save_device_tier_prefs", lambda _prefs: None)
    manager = _make_manager([], [_wpd_row("SWD#WPDBUSENUM#{guid}#0000")])
    devices, _tier = await manager.list_devices()
    assert len(devices) == 1
    assert manager.get_device_tier(devices[0].serial) == DeviceAccessTier.WPD


async def test_two_phones_keep_both_rows(monkeypatch):
    """Two physical iPhones must never collapse into one row."""
    monkeypatch.setattr(device_backend, "_save_device_tier_prefs", lambda _prefs: None)
    manager = _make_manager(
        [_tier1_row(_UDID_A), _tier1_row(_UDID_B)],
        [_wpd_row("SWD#WPDBUSENUM#{guid}#0000")],
    )
    devices, _tier = await manager.list_devices()
    serials = sorted(d.serial for d in devices)
    assert serials == sorted([_UDID_A, _UDID_B, "SWD#WPDBUSENUM#{guid}#0000"])


async def test_non_apple_devices_untouched(monkeypatch):
    """A Pixel on Tier 1 + Apple WPD row coexist (different devices)."""
    monkeypatch.setattr(device_backend, "_save_device_tier_prefs", lambda _prefs: None)
    pixel = IOSDevice(
        serial="PIXEL123",
        name="Pixel",
        model="Pixel 8",
        ios_version="unknown",
        connection_type="USB",
        status=DeviceStatus.READY,
    )
    manager = _make_manager([pixel], [_wpd_row("SWD#WPDBUSENUM#{guid}#0000")])
    devices, _tier = await manager.list_devices()
    assert sorted(d.serial for d in devices) == sorted(["PIXEL123", "SWD#WPDBUSENUM#{guid}#0000"])
