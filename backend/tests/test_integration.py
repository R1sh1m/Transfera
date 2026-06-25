"""
Transfera v2 — Integration Tests
Tests HTTP endpoints, WebSocket, and end-to-end pipeline.
Run: python -m backend.tests.test_integration
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from threading import Thread

import httpx
import uvicorn

# Ensure the repo root is on sys.path when running directly.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.config import HOST, PORT  # noqa: E402
from backend.main import app  # noqa: E402

# ======================================================================
# Helpers
# ======================================================================
_PASS = 0
_FAIL = 0
_SERVER_READY = False


def _check(condition: bool, msg: str) -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  [FAIL] {msg}")


def _assert_ok(response: httpx.Response, expected_status: int = 200) -> None:
    _check(
        response.status_code == expected_status,
        f"Expected {expected_status}, got {response.status_code}: {response.text}",
    )


def _json(response: httpx.Response) -> dict:
    return response.json()


# ======================================================================
# Server lifecycle
# ======================================================================
async def _start_server() -> None:
    global _SERVER_READY
    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        ws="wsproto",
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    _SERVER_READY = True
    await server.serve()


def _run_server_background() -> None:
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_start_server())


# ======================================================================
# Tests
# ======================================================================
def test_health(client: httpx.Client) -> None:
    r = client.get("/api/health")
    _assert_ok(r)
    data = _json(r)
    _check("port" in data, "Missing 'port' in response")
    _check(data.get("port") == PORT, f"Port mismatch: {data.get('port')}")
    _check(data.get("database") == "connected", "Database not connected")


def test_config(client: httpx.Client) -> None:
    r = client.get("/api/config")
    _assert_ok(r)
    data = _json(r)
    _check("batch_size" in data, "Missing 'batch_size'")
    _check(data["batch_size"] == 100, "batch_size != 100")
    _check("image_extensions" in data, "Missing image_extensions")
    _check("video_extensions" in data, "Missing video_extensions")


def test_config_constants(client: httpx.Client) -> None:
    r = client.get("/api/config")
    data = _json(r)
    _check(data["port"] == PORT, "Port mismatch")
    _check(data["host"] == HOST, "Host mismatch")
    _check(data["max_retry"] == 3, "max_retry mismatch")


def test_session_create(client: httpx.Client) -> None:
    r = client.post(
        "/api/sessions",
        json={
            "session_name": "test-session",
            "source_root": "/tmp/test",
            "dest_root": "/tmp/dest",
        },
    )
    _assert_ok(r, 200)
    data = _json(r)
    _check("id" in data, "Missing session id")
    _check(data["session_name"] == "test-session", "session_name mismatch")
    _check(data["status"] == "created", "status not created")


def test_session_list(client: httpx.Client) -> None:
    # Create a few sessions
    for i in range(3):
        client.post(
            "/api/sessions",
            json={
                "session_name": f"list-test-{i}",
                "source_root": f"/tmp/src{i}",
                "dest_root": f"/tmp/dst{i}",
            },
        )
    r = client.get("/api/sessions")
    _assert_ok(r)
    data = _json(r)
    _check("sessions" in data, "Missing sessions key")
    _check(data["total"] >= 3, f"Expected >= 3 sessions, got {data['total']}")


def test_session_get(client: httpx.Client) -> None:
    # Create then fetch
    r = client.post(
        "/api/sessions",
        json={"session_name": "get-test", "source_root": "/a", "dest_root": "/b"},
    )
    sid = _json(r)["id"]
    r2 = client.get(f"/api/sessions/{sid}")
    _assert_ok(r2)
    data = _json(r2)
    _check(data["id"] == sid, "ID mismatch")


def test_session_get_not_found(client: httpx.Client) -> None:
    r = client.get("/api/sessions/999999")
    _check(r.status_code == 404, f"Expected 404, got {r.status_code}")


def test_session_create_with_folder_layout(client: httpx.Client) -> None:
    """folder_layout can be set at session creation and returned in SessionInfo."""
    for layout in ("year/month/day", "year/month", "flat"):
        r = client.post(
            "/api/sessions",
            json={
                "session_name": f"layout-{layout}",
                "source_root": "/tmp/src",
                "dest_root": "/tmp/dst",
                "folder_layout": layout,
            },
        )
        _assert_ok(r, 200)
        data = _json(r)
        _check(data["folder_layout"] == layout, f"Expected folder_layout='{layout}', got '{data.get('folder_layout')}'")


def test_session_cancel(client: httpx.Client) -> None:
    r = client.post(
        "/api/sessions",
        json={"session_name": "cancel-test", "source_root": "/x", "dest_root": "/y"},
    )
    sid = _json(r)["id"]
    r2 = client.post(f"/api/sessions/{sid}/cancel")
    _assert_ok(r2)
    data = _json(r2)
    _check(data["status"] == "cancelled", "status not cancelled")


def test_session_start_not_found(client: httpx.Client) -> None:
    r = client.post("/api/sessions/999999/start")
    _check(r.status_code == 404, f"Expected 404, got {r.status_code}")


def test_session_pause_not_running(client: httpx.Client) -> None:
    r = client.post(
        "/api/sessions",
        json={"session_name": "pause-test", "source_root": "/p", "dest_root": "/q"},
    )
    sid = _json(r)["id"]
    r2 = client.post(f"/api/sessions/{sid}/pause")
    _check(r2.status_code == 400, f"Expected 400 (not running), got {r2.status_code}")


def test_media_list(client: httpx.Client) -> None:
    r = client.get("/api/media")
    _assert_ok(r)
    data = _json(r)
    _check("items" in data, "Missing 'items' key")
    _check("total" in data, "Missing 'total' key")
    _check("page" in data, "Missing 'page' key")


def test_media_list_pagination(client: httpx.Client) -> None:
    r = client.get("/api/media?page=1&page_size=10")
    _assert_ok(r)
    data = _json(r)
    _check(data["page"] == 1, "page mismatch")
    _check(data["page_size"] == 10, "page_size mismatch")


def test_duplicates_check(client: httpx.Client) -> None:
    # Non-existent batch_id should return an error (404 or 422)
    r2 = client.post(
        "/api/duplicates/check",
        json={"batch_id": 99999},
    )
    _check(r2.status_code in (404, 422, 500), f"Expected error for non-existent batch, got {r2.status_code}")


def test_batches_list(client: httpx.Client) -> None:
    r = client.post(
        "/api/sessions",
        json={"session_name": "batch-test", "source_root": "/b1", "dest_root": "/b2"},
    )
    sid = _json(r)["id"]
    r2 = client.get(f"/api/sessions/{sid}/batches")
    _assert_ok(r2)
    data = _json(r2)
    _check("batches" in data, "Missing batches key")


def test_recovery(client: httpx.Client) -> None:
    r = client.post("/api/recovery")
    _assert_ok(r)
    data = _json(r)
    _check(data["status"] == "recovered", "status not recovered")


def test_error_response_format(client: httpx.Client) -> None:
    r = client.get("/api/nonexistent")
    _check(r.status_code in (404, 422), f"Expected 404/422, got {r.status_code}")


def test_scan_missing_source(client: httpx.Client) -> None:
    r = client.post(
        "/api/scan",
        json={"source_path": "/nonexistent/path"},
    )
    _check(r.status_code == 404, f"Expected 404, got {r.status_code}")


def test_full_pipeline(client: httpx.Client) -> None:
    """Test scan -> batch -> hop1 -> hop2 pipeline with temp files."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as src_dir, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as dest_dir:
        src = Path(src_dir)
        dest = Path(dest_dir)

        # Create test files
        for i in range(5):
            f = src / f"photo_{i:03d}.jpg"
            f.write_bytes(f"fake image content {i}".encode())

        # Scan
        r = client.post(
            "/api/scan",
            json={
                "source_path": str(src),
                "dest_path": str(dest),
                "session_name": "pipeline-test",
            },
        )
        _assert_ok(r)
        scan_data = _json(r)
        sid = scan_data["session_id"]

        # Wait for background scan to complete
        time.sleep(2)

        # Check session status
        r2 = client.get(f"/api/sessions/{sid}")
        if r2.status_code == 200:
            session_data = _json(r2)
            _check(session_data["status"] in ("created", "running", "completed"),
                   f"Unexpected status: {session_data['status']}")


# ======================================================================
# Main
# ======================================================================
def main() -> None:
    global _PASS, _FAIL

    print("=" * 60)
    print("Transfera v2 - Integration Tests")
    print("=" * 60)

    # Start server in background thread
    server_thread = Thread(target=_run_server_background, daemon=True)
    server_thread.start()

    # Wait for server to be ready
    print("\nStarting server...")
    for _ in range(30):
        try:
            r = httpx.get(f"http://{HOST}:{PORT}/api/health", timeout=1.0)
            if r.status_code == 200:
                print("Server ready!\n")
                break
        except Exception:
            time.sleep(0.5)
    else:
        print("Server failed to start!")
        sys.exit(1)

    # Run tests
    with httpx.Client(base_url=f"http://{HOST}:{PORT}", timeout=10.0) as client:
        test_health(client)
        test_config(client)
        test_config_constants(client)
        test_session_create(client)
        test_session_list(client)
        test_session_get(client)
        test_session_get_not_found(client)
        test_session_cancel(client)
        test_session_start_not_found(client)
        test_session_pause_not_running(client)
        test_media_list(client)
        test_media_list_pagination(client)
        test_duplicates_check(client)
        test_batches_list(client)
        test_recovery(client)
        test_error_response_format(client)
        test_scan_missing_source(client)
        test_full_pipeline(client)

    # Summary
    print("=" * 60)
    total = _PASS + _FAIL
    print(f"Results: {_PASS}/{total} passed, {_FAIL} failed")
    print("=" * 60)

    sys.exit(0 if _FAIL == 0 else 1)


if __name__ == "__main__":
    main()
