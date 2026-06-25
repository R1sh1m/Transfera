"""
Regression tests for recently-fixed bugs in the Transfera backend.

These tests document bugs that were identified and fixed:

1. ``/api/ios-driver/install`` returned 422 when called with no request
   body because ``InstallDriverRequest`` was a required empty Pydantic
   model.  The fix removed the body parameter entirely.

2. Same endpoint — the frontend's ``useInstallDriver`` mutation sent no
   body at all (Axios ``post(url)`` with no data argument), which also
   triggered the 422.  The fix on the backend side (removing the required
   body) resolved this for all callers.

3. The winget invocation originally used PowerShell ``Start-Process``
   with an ``-ArgumentList`` that could contain ``$null`` when the
   version was not set.  The fix builds args via ``build_install_args()``
   which only includes ``--version`` / ``--exact`` when version is truthy,
   and calls winget directly from Python ``subprocess`` (no PowerShell
   wrapper).  The "already installed" exit code (0x8A150011) is treated
   as success.

4. ``/api/tier2/setup`` (provision_linux step) returned a generic error
   when apt-get failed with "Could not get lock".  The fix added a
   lock-wait retry strategy and exposed ``error_code="APT_LOCK_TIMEOUT"``
   in the response so the frontend can show a targeted message.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.wsl_orchestrator import Tier2StepResult

# ===========================================================================
# /api/ios-driver/install
# ===========================================================================


class TestIOSDriverInstall:
    """Tests for the iOS driver installation endpoint.

    The endpoint now calls winget directly from Python subprocess and
    returns success / exit_code / error instead of a command skeleton.
    """

    @patch("backend.api.routes._install_driver")
    def test_no_body_succeeds(self, mock_install, test_client):
        """POST with no body returns 200 with installation result."""
        mock_install.return_value = {
            "success": True,
            "exit_code": 0,
            "error": None,
        }

        resp = test_client.post("/api/ios-driver/install")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["exit_code"] == 0
        assert "message" in data

    @patch("backend.api.routes._install_driver")
    def test_empty_body_succeeds(self, mock_install, test_client):
        """POST with empty JSON body also works."""
        mock_install.return_value = {
            "success": True,
            "exit_code": 0,
            "error": None,
        }

        resp = test_client.post("/api/ios-driver/install", json={})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    @patch("backend.api.routes._install_driver")
    def test_install_failure_returns_400(self, mock_install, test_client):
        """When winget install fails, the endpoint returns 400."""
        mock_install.return_value = {
            "success": False,
            "exit_code": 1,
            "error": "winget install failed (exit code 1)",
        }

        resp = test_client.post("/api/ios-driver/install")

        assert resp.status_code == 400
        assert "winget install failed" in resp.json()["detail"]


# ===========================================================================
# /api/tier2/setup — apt lock timeout
# ===========================================================================


class TestAptLockTimeout:
    """Tests that the provision_linux step surfaces ``APT_LOCK_TIMEOUT``."""

    @patch("backend.api.tier2_routes.get_device_manager")
    def test_error_code_surfaced(self, mock_get_manager, test_client):
        """When provision_linux fails due to apt lock, error_code is set."""
        mock_orch = MagicMock()
        mock_orch.provision_linux = AsyncMock(return_value=Tier2StepResult(
            step_id="provision_linux",
            completed=False,
            error="Failed to Update package lists: "
                  "E: Could not get lock /var/lib/apt/lists/lock. "
                  "It is held by process 1186 (apt-get)",
            error_code="APT_LOCK_TIMEOUT",
            details={"steps_completed": []},
        ))
        mock_manager = MagicMock()
        mock_manager.get_orchestrator.return_value = mock_orch
        mock_get_manager.return_value = mock_manager

        resp = test_client.post(
            "/api/tier2/setup",
            json={"step_id": "provision_linux", "confirmed": True},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["error_code"] == "APT_LOCK_TIMEOUT"
        assert data["completed"] is False
        assert "Could not get lock" in (data.get("error") or "")

    @patch("backend.api.tier2_routes.get_device_manager")
    def test_other_error_no_error_code(self, mock_get_manager, test_client):
        """A non-lock error does not set error_code."""
        mock_orch = MagicMock()
        mock_orch.provision_linux = AsyncMock(return_value=Tier2StepResult(
            step_id="provision_linux",
            completed=False,
            error="Failed to Update package lists: apt-get returned exit code 1",
            error_code=None,
            details={"steps_completed": []},
        ))
        mock_manager = MagicMock()
        mock_manager.get_orchestrator.return_value = mock_orch
        mock_get_manager.return_value = mock_manager

        resp = test_client.post(
            "/api/tier2/setup",
            json={"step_id": "provision_linux", "confirmed": True},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("error_code") is None
        assert data["completed"] is False

    @patch("backend.api.tier2_routes.get_device_manager")
    def test_success_no_error_code(self, mock_get_manager, test_client):
        """A successful provision does not set error_code or error."""
        mock_orch = MagicMock()
        mock_orch.provision_linux = AsyncMock(return_value=Tier2StepResult(
            step_id="provision_linux",
            completed=True,
            error=None,
            error_code=None,
            details={"steps_completed": ["Update package lists", "Install USB/IP tools, Python, usbmuxd"]},
        ))
        mock_manager = MagicMock()
        mock_manager.get_orchestrator.return_value = mock_orch
        mock_get_manager.return_value = mock_manager

        resp = test_client.post(
            "/api/tier2/setup",
            json={"step_id": "provision_linux", "confirmed": True},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["completed"] is True
        assert data.get("error_code") is None
        assert data.get("error") is None


# ===========================================================================
# Thumbnail: broken / corrupt file handling
# ===========================================================================


class TestThumbnailBrokenFile:
    """Tests that broken/corrupt source files produce placeholders, not crashes.

    Verifies:
    - ``generate_thumbnail_bytes`` never raises, returns None for broken files.
    - ``_schedule_hop1_thumbnail`` marks the item as ``"failed"`` in the DB.
    - The thumbnail endpoint returns the placeholder JPEG with
      ``X-Thumbnail-Status: failed`` header.
    """

    def test_generate_returns_none_for_broken_image(self, tmp_path):
        """A corrupt/zero-byte image returns None (no exception)."""
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"\x00\x00\x00\x00")

        from backend.engines.thumbnailer import generate_thumbnail_bytes

        result = generate_thumbnail_bytes(broken)
        assert result is None

    def test_generate_returns_none_for_broken_video(self, tmp_path):
        """A zero-byte video file returns None (no exception)."""
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"")

        from backend.engines.thumbnailer import generate_thumbnail_bytes

        result = generate_thumbnail_bytes(broken)
        assert result is None

    def test_generate_returns_none_for_nonexistent_file(self, tmp_path):
        """A nonexistent path returns None (no exception)."""
        missing = tmp_path / "does_not_exist.jpg"

        from backend.engines.thumbnailer import generate_thumbnail_bytes

        result = generate_thumbnail_bytes(missing)
        assert result is None

    def test_schedule_hop1_marks_failed_on_none_return(self, db_session):
        """When generation returns None, the item is marked ``failed``."""
        import time
        from unittest.mock import patch

        from backend.database.models import MediaItem
        from backend.engines.cache_manager import _schedule_hop1_thumbnail
        from backend.engines.thumbnail_cache import thumbnail_cache

        item = MediaItem(
            source_path="/nonexistent/schedule_test_input.jpg",
            file_name="schedule_test_input.jpg",
            file_size=0,
        )
        db_session.add(item)
        import asyncio
        asyncio.run(db_session.commit())

        cached_path = Path("/nonexistent/schedule_test_cached.jpg")

        with patch(
            "backend.engines.thumbnailer.generate_thumbnail_bytes",
            return_value=None,
        ):
            _schedule_hop1_thumbnail(item.id, cached_path)

        # Poll the DB until the background worker processes the update,
        # with a generous timeout to avoid flakiness.
        deadline = time.monotonic() + 5.0
        status = None
        while time.monotonic() < deadline:
            import asyncio
            async def _check():
                async with __import__("backend.database.manager", fromlist=["session_scope"]).session_scope() as s:
                    upd = await s.get(MediaItem, item.id)
                    return upd.thumbnail_status if upd else None
            status = asyncio.run(_check())
            if status == "failed":
                break
            time.sleep(0.05)

        assert status == "failed", f"Expected 'failed', got {status!r}"
        assert not thumbnail_cache.has(item.id)

    def test_placeholder_jpeg_is_valid(self):
        """The placeholder JPEG is non-empty and decodes by Pillow."""
        from backend.api.routes import _get_placeholder_jpeg

        data = _get_placeholder_jpeg()
        assert data, "Placeholder JPEG is empty"

        import io

        from PIL import Image
        img = Image.open(io.BytesIO(data))
        assert img.format == "JPEG"
        assert img.size == (120, 120)

    def test_thumbnail_endpoint_returns_placeholder_with_header(self, test_client):
        """A failed thumbnail returns the placeholder JPEG and the
        ``X-Thumbnail-Status: failed`` header."""
        from unittest.mock import AsyncMock, patch

        from backend.database.models import MediaItem

        fake_item = MediaItem(
            source_path="/nonexistent/input.jpg",
            file_name="input.jpg",
            file_size=0,
            thumbnail_status="failed",
        )
        fake_item.id = 9999

        with patch("backend.api.routes.session_scope") as mock_scope:
            mock_ctx = AsyncMock()
            mock_session = AsyncMock()
            mock_session.get = AsyncMock(return_value=fake_item)
            mock_ctx.__aenter__.return_value = mock_session
            mock_scope.return_value = mock_ctx

            resp = test_client.get("/api/media/9999/thumbnail")

        assert resp.status_code == 200
        assert resp.headers.get("x-thumbnail-status") == "failed"
        assert resp.headers.get("content-type") == "image/jpeg"
        assert len(resp.content) > 0
