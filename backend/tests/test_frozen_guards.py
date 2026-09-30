"""
Frozen-build guards: pip-based self-install paths must never fire inside the
packaged app, where sys.executable is the engine exe (not a Python
interpreter) and the bundle is immutable. Spawning `exe -m pip install`
would boot a duplicate engine instead of installing anything.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from backend.api.auth import require_local_token
from backend.engines import clip
from backend.main import create_app


@pytest.fixture
def client():
    app = create_app()

    async def _skip_auth() -> None:
        return None

    app.dependency_overrides[require_local_token] = _skip_auth

    with TestClient(app) as c:
        yield c


def test_install_pymobiledevice3_refuses_when_frozen(client: TestClient):
    with patch.object(sys, "frozen", True, create=True):
        resp = client.post("/api/pymobiledevice3/install")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "packaged app" in data["message"]


def test_ensure_ai_packages_skips_pip_when_frozen():
    with (
        patch.object(sys, "frozen", True, create=True),
        patch.object(clip, "ai_packages_missing", return_value=True),
        patch("subprocess.run") as mock_run,
    ):
        assert clip.ensure_ai_packages() is False
    mock_run.assert_not_called()
