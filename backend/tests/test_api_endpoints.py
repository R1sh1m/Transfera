"""
Smoke tests for critical API endpoints.

Tests the HTTP interface layer using FastAPI TestClient with an in-memory
database, so no real filesystem or device dependencies are needed.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend.api.auth import require_local_token
from backend.main import create_app


@pytest.fixture
def client():
    app = create_app()

    async def _skip_auth() -> None:
        return None

    app.dependency_overrides[require_local_token] = _skip_auth

    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_health_returns_ok(self, client: TestClient):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "active_transfers" in data

    def test_active_transfers_endpoint(self, client: TestClient):
        resp = client.get("/api/transfers/active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False
        assert data["count"] == 0
        assert data["session_ids"] == []


class TestCORS:
    def test_tauri_origin_preflight_allowed(self, client: TestClient):
        # The packaged app runs at http://tauri.localhost and axios sends
        # Content-Type: application/json on every request, which forces a
        # CORS preflight. A 400 here means a permanent Engine Unavailable
        # screen with a perfectly healthy engine.
        resp = client.options(
            "/api/health",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://tauri.localhost"

    def test_unlisted_origin_preflight_rejected(self, client: TestClient):
        # DNS-rebinding guard: arbitrary sites must stay blocked from the
        # loopback engine.
        resp = client.options(
            "/api/health",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") is None


class TestConfig:
    def test_config_returns_settings(self, client: TestClient):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["port"] == 47821
        assert "image_extensions" in data
        assert "video_extensions" in data


class TestSessions:
    def test_list_sessions_returns_struct(self, client: TestClient):
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data


class TestSessionProgress:
    def test_progress_returns_404_for_missing_session(self, client: TestClient):
        resp = client.get("/api/sessions/99999/progress")
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "not found" in detail.lower()


class TestDeviceEndpoints:
    def test_ios_devices_returns_valid_struct(self, client: TestClient):
        resp = client.get("/api/ios-devices")
        assert resp.status_code == 200
        data = resp.json()
        assert "available" in data
        assert "devices" in data
        assert "driver_status" in data


class TestDevicePreview:
    def test_preview_flat(self, client: TestClient, tmp_path):
        (tmp_path / "img1.jpg").write_bytes(b"data")
        (tmp_path / "video.mp4").write_bytes(b"data")
        (tmp_path / "other.txt").write_bytes(b"data")

        resp = client.get(f"/api/device/preview?path={tmp_path}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert {item["filename"] for item in data["items"]} == {"img1.jpg", "video.mp4"}

    def test_preview_recursive(self, client: TestClient, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "img1.jpg").write_bytes(b"data")
        (sub / "nested.png").write_bytes(b"data")

        resp = client.get(f"/api/device/preview?path={tmp_path}&recursive=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert {item["filename"] for item in data["items"]} == {"img1.jpg", "nested.png"}
