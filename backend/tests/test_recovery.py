"""
Unit tests for iOS device self-healing: service lifeline, USB passthrough
recovery, and the recover/pymobiledevice3-install API endpoints.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

from backend.ios_driver_installer import (
    APPLE_SERVICE_NAME,
    ensure_apple_service_running,
)
from backend.wsl_orchestrator import USBDeviceInfo, WSLOrchestrator

# ===========================================================================
#  ensure_apple_service_running
# ===========================================================================


class TestEnsureServiceRunning:
    """ios_driver_installer.ensure_apple_service_running()"""

    @staticmethod
    def _mock_completed(
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["sc"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    @patch("backend.ios_driver_installer._run_sc_command")
    async def test_already_running(self, mock_sc):
        """Returns state=running when sc query shows RUNNING."""
        mock_sc.return_value = self._mock_completed(
            stdout="STATE               : 4  RUNNING",
        )
        result = await ensure_apple_service_running()
        assert result.state == "running"
        assert result.needs_elevation is False
        mock_sc.assert_called_once_with(["query", APPLE_SERVICE_NAME], 10)

    @patch("backend.ios_driver_installer._run_sc_command")
    async def test_not_installed(self, mock_sc):
        """Returns state=not_installed when sc query says service does not exist."""
        mock_sc.return_value = self._mock_completed(
            returncode=1060,
            stderr="The specified service does not exist as an installed service",
        )
        result = await ensure_apple_service_running()
        assert result.state == "not_installed"
        assert result.exit_code == 1060

    @patch("backend.ios_driver_installer._run_sc_command")
    async def test_stopped_and_start_succeeds(self, mock_sc):
        """Starts the service when stopped and returns state=running."""
        # First call: query → STOPPED
        # Second call: start → success
        mock_sc.side_effect = [
            self._mock_completed(stdout="STATE               : 1  STOPPED"),
            self._mock_completed(stdout="running", returncode=0),
        ]
        result = await ensure_apple_service_running()
        assert result.state == "running"
        assert result.exit_code == 0
        assert mock_sc.call_count == 2
        mock_sc.assert_has_calls([
            call(["query", APPLE_SERVICE_NAME], 10),
            call(["start", APPLE_SERVICE_NAME], 30),
        ])

    @patch("backend.ios_driver_installer._run_sc_command")
    async def test_stopped_and_start_needs_elevation(self, mock_sc):
        """Returns elevation_required when sc start fails with access denied."""
        mock_sc.side_effect = [
            self._mock_completed(stdout="STATE               : 1  STOPPED"),
            self._mock_completed(returncode=5, stderr="access denied"),
        ]
        result = await ensure_apple_service_running()
        assert result.state == "elevation_required"
        assert result.needs_elevation is True
        assert result.elevation_command == ["sc", "start", APPLE_SERVICE_NAME]
        assert result.exit_code == 5

    @patch("backend.ios_driver_installer._run_sc_command")
    async def test_stopped_and_start_unknown_error(self, mock_sc):
        """Returns state=error on unexpected sc start failure."""
        mock_sc.side_effect = [
            self._mock_completed(stdout="STATE               : 1  STOPPED"),
            self._mock_completed(returncode=1234, stderr="Something weird"),
        ]
        result = await ensure_apple_service_running()
        assert result.state == "error"
        assert result.needs_elevation is False
        assert result.exit_code == 1234

    @patch("backend.ios_driver_installer._run_sc_command")
    async def test_sc_not_on_path(self, mock_sc):
        """Handles FileNotFoundError when sc.exe is missing."""
        mock_sc.side_effect = FileNotFoundError("sc.exe not found")
        result = await ensure_apple_service_running()
        assert result.state == "error"
        assert "sc.exe not found" in result.message

    @patch("backend.ios_driver_installer._run_sc_command")
    async def test_unknown_state_returned(self, mock_sc):
        """Handles an unexpected STATE line gracefully."""
        mock_sc.return_value = self._mock_completed(
            stdout="STATE               : 99  UNKNOWN_STATE",
        )
        result = await ensure_apple_service_running()
        assert result.state == "error"
        assert "UNKNOWN_STATE" in result.message


# ===========================================================================
#  auto_recover_apple_device
# ===========================================================================


class TestAutoRecoverUSB:
    """WSLOrchestrator.auto_recover_apple_device()"""

    @staticmethod
    def _apple_device(
        busid: str = "1-1",
        bound: bool = True,
        attached: bool = False,
    ) -> USBDeviceInfo:
        state_parts = []
        if bound:
            state_parts.append("Shared")
        if attached:
            state_parts.append("Attached")
        return USBDeviceInfo(
            busid=busid,
            vid_pid="05ac:12a8",
            device_name="Apple iPhone",
            state=" / ".join(state_parts) if state_parts else "Not shared",
        )

    @staticmethod
    def _non_apple_device(busid: str = "2-1") -> USBDeviceInfo:
        return USBDeviceInfo(
            busid=busid,
            vid_pid="8087:0026",
            device_name="Intel USB Hub",
            state="Not shared",
        )

    @staticmethod
    def _mock_attach_result(attached: bool = True, error: str | None = None):
        from backend.wsl_orchestrator import Tier2DeviceStatus
        return Tier2DeviceStatus(
            busid="1-1",
            bound=True,
            attached=attached,
            error=error,
        )

    async def test_no_apple_devices(self):
        """When no Apple devices exist, returns success=False with no devices."""
        orch = WSLOrchestrator()
        orch.list_usb_devices = AsyncMock(return_value=[self._non_apple_device()])
        result = await orch.auto_recover_apple_device()
        assert result["apple_devices_found"] == 0
        assert result["success"] is False
        assert result["needs_elevation"] is False
        assert result["needs_bind"] == []

    async def test_apple_device_already_attached(self):
        """Already-attached Apple device is reported as success."""
        orch = WSLOrchestrator()
        orch.list_usb_devices = AsyncMock(
            return_value=[self._apple_device(attached=True)],
        )
        result = await orch.auto_recover_apple_device()
        assert result["apple_devices_found"] == 1
        assert result["success"] is True
        assert result["devices"][0]["attach_result"] == "already_attached"

    async def test_apple_device_not_bound(self):
        """Unbound Apple device is recorded in needs_bind."""
        orch = WSLOrchestrator()
        orch.list_usb_devices = AsyncMock(
            return_value=[self._apple_device(bound=False)],
        )
        result = await orch.auto_recover_apple_device()
        assert result["apple_devices_found"] == 1
        assert result["success"] is False
        assert result["needs_bind"] == ["1-1"]
        assert result["needs_elevation"] is True

    async def test_apple_device_bound_and_attached_successfully(self):
        """Bound-but-unattached device is automatically attached."""
        orch = WSLOrchestrator()
        orch.list_usb_devices = AsyncMock(
            return_value=[self._apple_device(bound=True, attached=False)],
        )
        orch.attach_device = AsyncMock(
            return_value=self._mock_attach_result(attached=True),
        )
        result = await orch.auto_recover_apple_device()
        assert result["apple_devices_found"] == 1
        assert result["success"] is True
        assert result["devices"][0]["attach_result"] == "attached"
        orch.attach_device.assert_awaited_once_with("1-1")

    async def test_apple_device_attach_fails_access_denied(self):
        """Attach failure with access denied sets needs_elevation."""
        orch = WSLOrchestrator()
        orch.list_usb_devices = AsyncMock(
            return_value=[self._apple_device(bound=True, attached=False)],
        )
        orch.attach_device = AsyncMock(
            return_value=self._mock_attach_result(
                attached=False, error="access denied (5)",
            ),
        )
        result = await orch.auto_recover_apple_device()
        assert result["success"] is False
        assert result["needs_elevation"] is True
        assert result["attach_errors"][0]["busid"] == "1-1"

    async def test_multiple_apple_devices_mixed_states(self):
        """Multiple devices: one attached, one needs bind, one auto-attached."""
        orch = WSLOrchestrator()
        orch.list_usb_devices = AsyncMock(return_value=[
            self._apple_device(busid="1-1", attached=True),        # already good
            self._apple_device(busid="1-2", bound=False),          # needs bind
            self._apple_device(busid="1-3", bound=True, attached=False),  # can attach
        ])
        orch.attach_device = AsyncMock(
            return_value=self._mock_attach_result(attached=True),
        )
        result = await orch.auto_recover_apple_device()
        assert result["apple_devices_found"] == 3
        assert result["success"] is True  # at least one is good
        assert result["needs_bind"] == ["1-2"]
        assert result["needs_elevation"] is True

    async def test_empty_device_list(self):
        """Empty list_usb_devices means no recovery possible."""
        orch = WSLOrchestrator()
        orch.list_usb_devices = AsyncMock(return_value=[])
        result = await orch.auto_recover_apple_device()
        assert result["apple_devices_found"] == 0
        assert result["success"] is False
        assert result["needs_elevation"] is False


# ===========================================================================
#  API endpoints
# ===========================================================================


class TestRecoveryRoutes:
    """POST /ios-devices/recover and POST /pymobiledevice3/install"""

    @staticmethod
    def _service_dict(
        state: str = "running",
        needs_elevation: bool = False,
        elevation_command: list[str] | None = None,
        exit_code: int | None = 0,
        message: str = "",
    ) -> dict:
        return {
            "state": state,
            "needs_elevation": needs_elevation,
            "elevation_command": elevation_command,
            "exit_code": exit_code,
            "message": message,
            "service_name": APPLE_SERVICE_NAME,
        }

    @patch("backend.api.routes.get_device_manager")
    def test_recover_service_restored(self, mock_get_mgr, test_client):
        """Phase 1 recovery: service restored to running."""
        mock_mgr = MagicMock()
        mock_mgr.ensure_apple_service_running = AsyncMock(
            return_value=self._service_dict(state="running"),
        )
        mock_get_mgr.return_value = mock_mgr

        resp = test_client.post("/api/ios-devices/recover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "service_restored"
        assert data["service"]["state"] == "running"

    @patch("backend.api.routes.get_device_manager")
    def test_recover_not_installed_then_usb(self, mock_get_mgr, test_client):
        """Phase 2 fallback when Apple service not installed."""
        mock_mgr = MagicMock()
        mock_mgr.ensure_apple_service_running = AsyncMock(
            return_value=self._service_dict(state="not_installed", exit_code=1060),
        )
        mock_mgr.auto_recover_apple_device = AsyncMock(
            return_value={
                "success": True,
                "apple_devices_found": 1,
                "devices": [{"busid": "1-1", "attach_result": "attached"}],
                "attach_errors": [],
                "needs_bind": [],
                "needs_elevation": False,
            },
        )
        mock_get_mgr.return_value = mock_mgr

        resp = test_client.post("/api/ios-devices/recover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "usb_passthrough_restored"
        assert data["service"]["state"] == "not_installed"
        assert data["usb"]["success"] is True

    @patch("backend.api.routes.get_device_manager")
    def test_recover_elevation_required(self, mock_get_mgr, test_client):
        """Service exists but needs elevation to start."""
        mock_mgr = MagicMock()
        mock_mgr.ensure_apple_service_running = AsyncMock(
            return_value=self._service_dict(
                state="elevation_required",
                needs_elevation=True,
                elevation_command=["sc", "start", APPLE_SERVICE_NAME],
                exit_code=5,
            ),
        )
        mock_get_mgr.return_value = mock_mgr

        resp = test_client.post("/api/ios-devices/recover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "elevation_required"
        assert data["service"]["needs_elevation"] is True

    @patch("backend.api.routes.get_device_manager")
    def test_recover_needs_bind(self, mock_get_mgr, test_client):
        """Service not installed and usbipd device needs bind."""
        mock_mgr = MagicMock()
        mock_mgr.ensure_apple_service_running = AsyncMock(
            return_value=self._service_dict(state="not_installed"),
        )
        mock_mgr.auto_recover_apple_device = AsyncMock(
            return_value={
                "success": False,
                "apple_devices_found": 1,
                "devices": [{"busid": "1-1", "error": "not bound"}],
                "attach_errors": [],
                "needs_bind": ["1-1"],
                "needs_elevation": True,
            },
        )
        mock_get_mgr.return_value = mock_mgr

        resp = test_client.post("/api/ios-devices/recover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "needs_bind"
        assert data["usb"]["needs_bind"] == ["1-1"]

    @patch("backend.api.routes.get_device_manager")
    def test_recover_no_device_found(self, mock_get_mgr, test_client):
        """Service not installed and no Apple device in usbipd list."""
        mock_mgr = MagicMock()
        mock_mgr.ensure_apple_service_running = AsyncMock(
            return_value=self._service_dict(state="not_installed"),
        )
        mock_mgr.auto_recover_apple_device = AsyncMock(
            return_value={
                "success": False,
                "apple_devices_found": 0,
                "devices": [],
                "attach_errors": [],
                "needs_bind": [],
                "needs_elevation": False,
            },
        )
        mock_get_mgr.return_value = mock_mgr

        resp = test_client.post("/api/ios-devices/recover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "no_device_found"

    @patch("backend.api.routes.get_device_manager")
    def test_recover_no_recovery_needed(self, mock_get_mgr, test_client):
        """Service running and usbmuxd reachable."""
        mock_mgr = MagicMock()
        mock_mgr.ensure_apple_service_running = AsyncMock(
            return_value=self._service_dict(state="running"),
        )
        mock_get_mgr.return_value = mock_mgr

        resp = test_client.post("/api/ios-devices/recover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "service_restored"


# ===========================================================================
#  pymobiledevice3 install endpoint
# ===========================================================================


class TestPymobiledevice3Install:
    """POST /pymobiledevice3/install"""

    @patch("backend.api.routes.asyncio.create_subprocess_exec")
    def test_install_success(self, mock_subproc, test_client):
        """Successful pip install returns success=True."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_subproc.return_value = mock_proc

        resp = test_client.post("/api/pymobiledevice3/install")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "installed" in data["message"]

    @patch("backend.api.routes.asyncio.create_subprocess_exec")
    def test_install_failure(self, mock_subproc, test_client):
        """Failed pip install returns success=False with error detail."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"ERROR: No matching distribution"),
        )
        mock_proc.returncode = 1
        mock_subproc.return_value = mock_proc

        resp = test_client.post("/api/pymobiledevice3/install")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "No matching distribution" in data["message"]

    @patch("backend.api.routes.asyncio.create_subprocess_exec")
    def test_install_exception(self, mock_subproc, test_client):
        """Subprocess-level exception returns success=False."""
        mock_subproc.side_effect = FileNotFoundError("pip not found")

        resp = test_client.post("/api/pymobiledevice3/install")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "pip not found" in data["message"]


# ===========================================================================
#  Standalone runner
# ===========================================================================


_PASS = 0
_FAIL = 0


def _check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f"  -- {detail}"
        print(msg)


async def _run_one(test_fn, name: str) -> int:
    """Run one test.  Returns 1 on failure, 0 on success."""
    try:
        await test_fn()
        print(f"  PASS  {name}")
        return 0
    except Exception as exc:
        print(f"  FAIL  {name}: {exc}")
        return 1


async def main() -> int:
    srv = TestEnsureServiceRunning()
    usb = TestAutoRecoverUSB()
    failed = 0
    total = 0

    print("--- ensure_apple_service_running ---")
    for attr in dir(srv):
        if attr.startswith("test_"):
            total += 1
            failed += await _run_one(getattr(srv, attr), attr)

    print("\n--- auto_recover_apple_device ---")
    for attr in dir(usb):
        if attr.startswith("test_"):
            total += 1
            failed += await _run_one(getattr(usb, attr), attr)

    passed = total - failed
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
