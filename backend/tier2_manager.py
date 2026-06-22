"""
Transfera v2 -- Unified Device Manager (Facade)
Thin facade over DeviceBackendManager that preserves the existing public API
so existing callers (routes, source_reader, scanner) continue to work
unchanged.

The actual two-tier logic, fallback, persistence, and settings live in
DeviceBackendManager (device_backend.py). This module just re-exports
the singleton and provides backward-compatible method signatures.
"""

from __future__ import annotations

import logging

from backend.device_backend import (
    DeviceAccessTier,
    DeviceBackendManager,
    get_device_backend_manager,
)

logger = logging.getLogger(__name__)

# Re-export for backward compatibility (used by tier2_routes.py)
DeviceAccessTier = DeviceAccessTier  # noqa: F811


class UnifiedDeviceManager:
    """
    Backward-compatible facade over DeviceBackendManager.

    All existing callers (routes.py, source_reader.py, scanner.py) use
    this class. It delegates every call to DeviceBackendManager, which
    contains the real tier-selection and fallback logic.
    """

    def __init__(self):
        self._backend = get_device_backend_manager()

    async def initialize(self) -> None:
        await self._backend.initialize()

    async def get_active_tier(self):  # -> DeviceAccessTier
        return await self._backend.get_active_tier()

    def get_device_tier(self, serial: str):  # -> DeviceAccessTier | None
        return self._backend.get_device_tier(serial)

    def get_orchestrator(self):
        return self._backend.get_orchestrator()

    async def list_devices(self):
        return await self._backend.list_devices()

    async def browse_device(self, serial: str, path: str):
        return await self._backend.browse_device(serial, path)

    async def get_device_file_info(self, serial: str, path: str):
        return await self._backend.get_device_file_info(serial, path)

    async def read_device_file(self, serial: str, path: str):
        return await self._backend.read_device_file(serial, path)

    def create_tier2_afc_reader(self, serial: str, path: str):
        return self._backend.create_tier2_afc_reader(serial, path)


# ---------------------------------------------------------------------------
# Singleton (backward-compatible)
# ---------------------------------------------------------------------------
_instance: UnifiedDeviceManager | None = None


def get_device_manager() -> UnifiedDeviceManager:
    global _instance
    if _instance is None:
        _instance = UnifiedDeviceManager()
    return _instance
