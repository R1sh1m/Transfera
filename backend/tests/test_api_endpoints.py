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
