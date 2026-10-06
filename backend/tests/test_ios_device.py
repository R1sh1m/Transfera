"""
Tests for iOS device failure handling (no hardware required).

Covers the hang classes that surfaced as a bare 30 s client timeout with
an empty preview grid:
  * usbmux import failure degrades to [] with a single warning (the device
    list is polled every few seconds — it must not spam the log);
  * the lockdown trust probe is bounded (slow/locked device -> LOCKED);
  * AFC directory walks are bounded (stalled device -> RuntimeError, which
    the endpoint maps to a clear 503 instead of a hung request).
"""

from __future__ import annotations

import logging
import sys
import threading

import pytest

import backend.ios_device as ios_device
from backend.ios_device import (
    DeviceStatus,
    browse_device_directory,
    friendly_device_model,
    list_ios_devices,
    query_lockdown_versions,
)


async def test_usbmux_import_failure_warns_once_and_returns_empty(monkeypatch, caplog):
    """A broken usbmux import (e.g. missing win32security) degrades quietly."""
    monkeypatch.setitem(sys.modules, "pymobiledevice3.usbmux", None)
    # Reset the once-flag in case another test already tripped it.
    monkeypatch.setattr(ios_device, "_warned_usbmux_import", False)
    with caplog.at_level(logging.WARNING, logger="backend.ios_device"):
        assert await list_ios_devices() == []
        assert await list_ios_devices() == []
    warnings = [r for r in caplog.records if "Failed to import pymobiledevice3 usbmux" in r.message]
    assert len(warnings) == 1


class _BlockingLockdown:
    """Fake lockdown whose trust probe never answers."""

    short_info = {"DeviceName": "iPhone", "ProductType": "iPhone99,9", "ProductVersion": "99.9"}

    def __init__(self):
        self._gate = threading.Event()

    @property
    def all_values(self):
        self._gate.wait(30)
        return {}

    def close(self):
        pass


class _MuxDev:
    serial = "SERIAL-123"
    connection_type = "USB"


async def test_trust_probe_timeout_reports_locked(monkeypatch):
    """A trust probe that never answers is bounded and reports LOCKED."""
    import pymobiledevice3.lockdown as _lockdown_mod
    import pymobiledevice3.usbmux as _usbmux_mod

    lockdown = _BlockingLockdown()
    monkeypatch.setattr(_lockdown_mod, "create_using_usbmux", lambda *_args, **_kwargs: lockdown)
    monkeypatch.setattr(_usbmux_mod, "list_devices", lambda: [_MuxDev()])
    monkeypatch.setattr(ios_device, "_TRUST_PROBE_TIMEOUT", 0.2)
    devices = await list_ios_devices()
    assert len(devices) == 1
    assert devices[0].status == DeviceStatus.LOCKED


class _HangingAfc:
    def listdir(self, path):
        threading.Event().wait(30)
        return []

    def close(self):
        pass


class _NoopLockdown:
    def close(self):
        pass


async def test_browse_hang_raises_actionable_error(monkeypatch):
    """A stalled AFC walk raises instead of hanging past client timeouts."""
    monkeypatch.setattr(ios_device, "_BROWSE_TIMEOUT", 0.2)

    async def _fake_afc_service(serial):
        return _HangingAfc(), _NoopLockdown()

    monkeypatch.setattr(ios_device, "_get_afc_service", _fake_afc_service)
    with pytest.raises(RuntimeError, match="stopped responding"):
        await browse_device_directory("SERIAL-123", "/DCIM")


class _InfoLockdown:
    """Fake lockdown that answers short_info (no trust probe needed)."""

    short_info = {"DeviceName": "Rishi's iPhone", "ProductType": "iPhone99,9", "ProductVersion": "18.4"}

    def close(self):
        pass


async def test_query_lockdown_versions_returns_short_info_only(monkeypatch):
    """Version query uses short_info and never touches the trust probe."""
    import pymobiledevice3.lockdown as _lockdown_mod
    import pymobiledevice3.usbmux as _usbmux_mod

    lockdown = _InfoLockdown()
    monkeypatch.setattr(_lockdown_mod, "create_using_usbmux", lambda *_args, **_kwargs: lockdown)
    monkeypatch.setattr(_usbmux_mod, "list_devices", lambda: [_MuxDev()])
    versions = await query_lockdown_versions()
    assert versions == {
        "SERIAL-123": {"name": "Rishi's iPhone", "model": "iPhone99,9", "ios_version": "18.4"},
    }


def test_friendly_device_model_maps_known_types():
    assert friendly_device_model("iPhone10,2") == "iPhone 8 Plus"
    assert friendly_device_model("iPhone17,2") == "iPhone 16 Pro Max"
    assert friendly_device_model("iPhone12,1") == "iPhone 11"


def test_friendly_device_model_falls_back_to_raw():
    assert friendly_device_model("iPhone99,9") == "iPhone99,9"
    assert friendly_device_model("") == "iPhone"
    assert friendly_device_model(None) == "iPhone"


async def test_query_lockdown_versions_skips_untrusted_devices(monkeypatch):
    """Lockdown failures (untrusted/locked) omit the device instead of raising."""

    def _raise(*_args, **_kwargs):
        raise RuntimeError("not paired")

    import pymobiledevice3.lockdown as _lockdown_mod
    import pymobiledevice3.usbmux as _usbmux_mod

    monkeypatch.setattr(_lockdown_mod, "create_using_usbmux", _raise)
    monkeypatch.setattr(_usbmux_mod, "list_devices", lambda: [_MuxDev()])
    assert await query_lockdown_versions() == {}


async def test_query_lockdown_versions_empty_without_usbmux(monkeypatch):
    """No driver / no devices degrades to an empty mapping."""
    import pymobiledevice3.usbmux as _usbmux_mod

    monkeypatch.setattr(_usbmux_mod, "list_devices", lambda: [])
    assert await query_lockdown_versions() == {}


class _FakeAfc:
    """Fake AFC service recording how many instances were created."""

    created = 0

    def __init__(self):
        type(self).created += 1
        self.closed = False

    def listdir(self, path):
        return ["IMG_1.JPG"]

    def stat(self, path):
        return {"st_ifmt": "S_IFREG", "st_size": 10, "st_mtime": 0}

    def close(self):
        self.closed = True


class _FakePooledLockdown:
    def close(self):
        pass


def _patch_afc(monkeypatch, lockdown_factory=None):
    import pymobiledevice3.lockdown as _lockdown_mod
    import pymobiledevice3.services.afc as _afc_mod

    calls = {"create": 0}

    def _fake_create(*_args, **_kwargs):
        calls["create"] += 1
        if lockdown_factory is not None:
            return lockdown_factory()
        return _FakePooledLockdown()

    monkeypatch.setattr(_lockdown_mod, "create_using_usbmux", _fake_create)
    monkeypatch.setattr(_afc_mod, "AfcService", lambda **_kw: _FakeAfc())
    return calls


async def test_afc_pool_reuses_session(monkeypatch):
    """Three sequential browses pay one handshake, not three."""
    from backend.ios_device import _AFC_POOL

    _AFC_POOL.clear()
    _FakeAfc.created = 0
    calls = _patch_afc(monkeypatch)
    for _ in range(3):
        entries = await browse_device_directory("SERIAL-123", "/DCIM")
        assert [e.name for e in entries] == ["IMG_1.JPG"]
    assert calls["create"] == 1
    assert _FakeAfc.created == 1
    _AFC_POOL.clear()


async def test_afc_pool_reconnects_after_failure(monkeypatch):
    """A failed op drops its slot; the next op reconnects transparently."""
    from backend.ios_device import _AFC_POOL

    _AFC_POOL.clear()
    calls = _patch_afc(monkeypatch)

    import pymobiledevice3.services.afc as _afc_mod

    broken = {"fail": True}

    class _FlakyAfc(_FakeAfc):
        def listdir(self, path):
            if broken["fail"]:
                raise RuntimeError("stale handle")
            return ["IMG_1.JPG"]

    monkeypatch.setattr(_afc_mod, "AfcService", lambda **_kw: _FlakyAfc())
    with pytest.raises(RuntimeError, match="stale handle"):
        await browse_device_directory("SERIAL-123", "/DCIM")
    broken["fail"] = False
    entries = await browse_device_directory("SERIAL-123", "/DCIM")
    assert [e.name for e in entries] == ["IMG_1.JPG"]
    assert calls["create"] == 2
    _AFC_POOL.clear()


async def test_afc_pool_evicts_idle_slots(monkeypatch):
    """Idle-expired slots are not reused."""
    import backend.ios_device as _ios_mod
    from backend.ios_device import _AFC_POOL

    _AFC_POOL.clear()
    calls = _patch_afc(monkeypatch)
    monkeypatch.setattr(_ios_mod, "_AFC_IDLE_TTL_SECONDS", 0)
    await browse_device_directory("SERIAL-123", "/DCIM")
    await browse_device_directory("SERIAL-123", "/DCIM")
    assert calls["create"] == 2
    _AFC_POOL.clear()
