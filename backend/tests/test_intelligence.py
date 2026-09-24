"""
Transfera v2 — Intelligence tests (offline, no models required).
Covers dHash perceptual grouping, keeper scoring, keyword semantic
search, moment clustering, capabilities, and trash/favorite curation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from PIL import Image


def _make_image(path, color, size=(64, 64)):
    img = Image.new("RGB", size, color)
    img.save(path, "PNG")


def test_dhash_identical_and_hamming():
    import tempfile
    from pathlib import Path

    from backend.engines.perceptual_hash import dhash64, hamming_distance

    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a.png"
        b = Path(td) / "b.png"
        _make_image(a, (200, 30, 30))
        _make_image(b, (200, 30, 30))
        ha, hb = dhash64(a), dhash64(b)
        assert ha and hb and len(ha) == 16
        assert hamming_distance(ha, hb) == 0
        assert hamming_distance(ha, "ffffffffffffffff") > 0


def test_grouping_and_keeper():
    from backend.engines.perceptual_hash import group_near_duplicates, suggest_keeper

    h = "a" * 16
    items = [
        {"id": 1, "phash": h, "file_size": 100, "width": 100, "height": 100, "favorite": False},
        {"id": 2, "phash": h, "file_size": 5000, "width": 4000, "height": 3000, "favorite": False},
        {"id": 3, "phash": "0" * 16, "file_size": 999, "width": 10, "height": 10, "favorite": False},
    ]
    groups = group_near_duplicates(items, threshold=0)
    assert len(groups) == 1 and len(groups[0]) == 2
    keeper = suggest_keeper(groups[0])
    assert keeper is not None and keeper["id"] == 2  # larger resolution wins


def test_semantic_keyword_and_moments():
    from backend.engines.intelligence import cluster_moments, semantic_search_keyword

    items = [
        {
            "id": 1,
            "file_name": "screenshot_whatsapp.png",
            "tags_json": '["screenshot"]',
            "caption": None,
            "camera_make": None,
            "camera_model": None,
        },
        {
            "id": 2,
            "file_name": "dsc_beach.jpg",
            "tags_json": '["photo"]',
            "caption": "sunset at sea",
            "camera_make": "Canon",
            "camera_model": "EOS R5",
        },
    ]
    hits = semantic_search_keyword("screenshot", items)
    assert [h["id"] for h in hits] == [1]
    hits2 = semantic_search_keyword("canon sunset", items)
    assert hits2 and hits2[0]["id"] == 2

    base = datetime(2026, 1, 1, tzinfo=UTC)
    m_items = [
        {"id": 1, "date_taken": base, "created_at": base, "file_size": 10},
        {"id": 2, "date_taken": base, "created_at": base, "file_size": 20},
        {"id": 10, "date_taken": datetime(2026, 3, 1, tzinfo=UTC), "created_at": base, "file_size": 5},
    ]
    moments = cluster_moments(m_items, gap_hours=12)
    assert len(moments) == 2
    assert moments[0]["count"] == 2 and moments[0]["cover_id"] == 2


def test_structured_from_tags():
    from backend.engines.intelligence import structured_from_tags

    tags = {
        "ImageWidth": "4000",
        "ImageHeight": "3000",
        "Make": "Canon",
        "Model": "EOS R5",
        "GPSLatitude": "48.85",
        "GPSLatitudeRef": "N",
        "GPSLongitude": "2.35",
        "GPSLongitudeRef": "E",
        "Duration": "00:00:10.5",
    }
    s = structured_from_tags(tags)
    assert s["width"] == 4000 and s["camera_model"] == "EOS R5"
    assert abs((s["gps_lat"] or 0) - 48.85) < 1e-6
    assert abs((s["duration_s"] or 0) - 10.5) < 1e-6


def test_capabilities_no_models(test_client):
    r = test_client.get("/api/intelligence/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["semantic_mode"] in ("keyword", "clip")
    assert "phash_available" in body


def test_manifest_chain_deterministic():
    from backend.api.intelligence_routes import _manifest_chain
    from backend.api.schemas import ManifestItemSchema

    items = [
        ManifestItemSchema(id=2, file_name="b.jpg", file_size=20, blake3="bb", phash="1" * 16),
        ManifestItemSchema(id=1, file_name="a.jpg", file_size=10, blake3="aa", phash="0" * 16),
    ]
    # Order-independent input, identical chain; different session -> different chain.
    assert _manifest_chain(items, 7) == _manifest_chain(list(reversed(items)), 7)
    assert _manifest_chain(items, 7) != _manifest_chain(items, 8)
    assert len(_manifest_chain(items, 7)) == 64


def test_manifest_verify_stats_review_endpoints(test_client):
    r = test_client.get("/api/intelligence/sessions/999999/manifest")
    assert r.status_code == 404
    r = test_client.post("/api/intelligence/sessions/999999/verify")
    assert r.status_code == 404
    r = test_client.get("/api/intelligence/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] >= 0 and body["recoverable_bytes"] >= 0
    r = test_client.get("/api/intelligence/review-queue")
    assert r.status_code == 200
    assert "blurry_ids" in r.json()


def test_trash_favorite_flow(test_client):
    # Seed one completed item directly via API-visible list; create via DB is complex
    # here, so exercise the 404 paths + empty-trash shape instead.
    r = test_client.patch("/api/media/999999", json={"favorite": True})
    assert r.status_code == 404
    r = test_client.get("/api/trash")
    assert r.status_code == 200
    assert "items" in r.json()
    r = test_client.post("/api/trash/empty")
    assert r.status_code == 200
    assert "emptied" in r.json()
    r = test_client.get("/api/intelligence/timeline?granularity=month")
    assert r.status_code == 200
    r = test_client.get("/api/intelligence/moments")
    assert r.status_code == 200
    r = test_client.post("/api/intelligence/search/semantic", json={"query": "beach", "limit": 10})
    assert r.status_code == 200
    assert r.json()["mode"] in ("keyword", "clip")
