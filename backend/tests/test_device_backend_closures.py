"""
Unit tests for DeviceBackendManager closure argument forwarding.

Ensures that browse_device, get_device_file_info, and read_device_file
correctly forward (serial, path) to the underlying backend methods.

These tests caught BUG A: the inner closures were defined with an extra
parameter that _run_operation never supplied, causing a TypeError on every
call.

Run: python -m pytest backend/tests/test_device_backend_closures.py -v
  or: python -m backend.tests.test_device_backend_closures
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure backend package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.device_backend import (
    DeviceAccessTier,
    DeviceBackendManager,
    DeviceFileInfo,
    TierProbeResult,
)
from backend.ios_device import DeviceStatus, IOSDevice


class _StubBackend:
    """Minimal mock backend that records calls and returns canned results."""

    def __init__(self):
        self.browse = AsyncMock(return_value=[
            DeviceFileInfo(name="test.txt", path="/DCIM/test.txt", is_dir=False, size=100, mtime=0),
        ])
        self.file_info = AsyncMock(return_value=
            DeviceFileInfo(name="test.txt", path="/DCIM/test.txt", is_dir=False, size=100, mtime=0),
        )
        self.read_file = AsyncMock(return_value=b"file contents")
        self.list_devices = AsyncMock(return_value=[
            IOSDevice(
                serial="TEST123", name="Test iPhone", model="iPhone 15",
                ios_version="17.0", connection_type="USB", status=DeviceStatus.READY,
            ),
        ])

    @property
    def tier(self):
        return DeviceAccessTier.WPD

    @property
    def is_configured(self):
        return True

    async def is_available(self):
        return TierProbeResult(tier=self.tier, available=True)

    def create_file_reader(self, serial, path):
        return MagicMock()


def _make_manager(backend: _StubBackend) -> DeviceBackendManager:
    """Create a DeviceBackendManager with a single stub backend in the chain."""
    manager = DeviceBackendManager.__new__(DeviceBackendManager)
    manager._device_tier_prefs = {}
    manager._device_tier_map = {}
    manager._prefer_tier2 = False
    manager._wsl_orchestrator = None

    # iOS isolation tracking set (must be initialised for _is_ios_device)
    manager._ios_serials = set()

    # Auto-activation state fields (set during initialize())
    manager._apple_driver_installable = False
    manager._apple_driver_package_name = None
    manager._apple_driver_package_version = None
    manager._bridge_auto_started = False
    manager._wsl_setup_suggested = False
    manager._tier2_error = None

    manager._tier1 = MagicMock()
    manager._tier1.is_configured = False
    manager._tier1.tier = DeviceAccessTier.TIER_1
    manager._tier2 = MagicMock()
    manager._tier2.is_configured = False
    manager._tier2.tier = DeviceAccessTier.TIER_2
    manager._wpd = backend
    return manager


@pytest.mark.asyncio
async def test_browse_device_forwards_serial_and_path():
    """browse_device must pass (serial, path) to backend.browse()."""
    stub = _StubBackend()
    manager = _make_manager(stub)

    result = await manager.browse_device("REAL_SERIAL_123", "/DCIM/100APPLE")

    stub.browse.assert_awaited_once()
    args = stub.browse.call_args
    assert args.args == ("REAL_SERIAL_123", "/DCIM/100APPLE"), (
        f"Expected browse('REAL_SERIAL_123', '/DCIM/100APPLE'), got {args}"
    )
    assert len(result) == 1
    assert result[0].name == "test.txt"


@pytest.mark.asyncio
async def test_file_info_device_forwards_serial_and_path():
    """get_device_file_info must pass (serial, path) to backend.file_info()."""
    stub = _StubBackend()
    manager = _make_manager(stub)

    result = await manager.get_device_file_info("DEV_ABC", "/DCIM/photo.jpg")

    stub.file_info.assert_awaited_once()
    args = stub.file_info.call_args
    assert args.args == ("DEV_ABC", "/DCIM/photo.jpg"), (
        f"Expected file_info('DEV_ABC', '/DCIM/photo.jpg'), got {args}"
    )
    assert result.name == "test.txt"


@pytest.mark.asyncio
async def test_read_device_file_forwards_serial_and_path():
    """read_device_file must pass (serial, path) to backend.read_file()."""
    stub = _StubBackend()
    manager = _make_manager(stub)

    result = await manager.read_device_file("MY_DEVICE", "/DCIM/video.mov")

    stub.read_file.assert_awaited_once()
    args = stub.read_file.call_args
    assert args.args == ("MY_DEVICE", "/DCIM/video.mov"), (
        f"Expected read_file('MY_DEVICE', '/DCIM/video.mov'), got {args}"
    )
    assert result == b"file contents"


@pytest.mark.asyncio
async def test_browse_device_with_special_characters_in_serial():
    """Device IDs with URL-hostile characters must be forwarded verbatim."""
    stub = _StubBackend()
    manager = _make_manager(stub)

    real_id = r"\\?\usb#vid_05ac&pid_12a8&mi_00#6&3659d4bd&4&0000#{6ac27878-...}"
    await manager.browse_device(real_id, "/DCIM")

    stub.browse.assert_awaited_once()
    args = stub.browse.call_args
    assert args.args[0] == real_id, (
        f"Serial with special chars was not forwarded correctly: {args.args[0]!r}"
    )


async def main():
    tests = [
        test_browse_device_forwards_serial_and_path,
        test_file_info_device_forwards_serial_and_path,
        test_read_device_file_forwards_serial_and_path,
        test_browse_device_with_special_characters_in_serial,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            await test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
