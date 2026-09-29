"""
Transfera v2 — Documents separate-tree tests.

Documents must land under ``<dest_root>/Documents/<Kind>/`` with the same
date-wise skeleton as photos; photos/video/audio stay directly under
``<dest_root>/``. Covers organizer routing, legacy-tree fallback, the
doc_kind library filter, and the manual preview/execute migration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

DT = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


def _item(**kwargs):
    from backend.database.models import MediaItem

    base = {
        "source_path": "/src/file.bin",
        "file_name": "file.bin",
        "file_size": 10,
    }
    base.update(kwargs)
    return MediaItem(**base)


# ---------------------------------------------------------------------------
# Kind mapping
# ---------------------------------------------------------------------------
def test_document_kind_mapping():
    from backend.config import (
        DOCUMENT_KIND_FALLBACK,
        DOCUMENT_KINDS,
        document_kind_extensions,
        document_kind_for_extension,
        is_document_extension,
    )

    assert is_document_extension(".pdf")
    assert is_document_extension(".XLSX")
    assert not is_document_extension(".jpg")
    assert not is_document_extension(None)
    assert document_kind_for_extension(".pdf") == "PDFs"
    assert document_kind_for_extension(".xlsx") == "Spreadsheets"
    assert document_kind_for_extension(".pptx") == "Presentations"
    assert document_kind_for_extension(".docx") == "Word-Docs"
    assert document_kind_for_extension(".txt") == "Text-CSV"
    assert document_kind_for_extension(".epub") == "eBooks"
    assert document_kind_for_extension(".unknown") == DOCUMENT_KIND_FALLBACK
    assert document_kind_for_extension(None) == DOCUMENT_KIND_FALLBACK
    # Every mapped kind is advertised, and every advertised kind has members.
    for kind in DOCUMENT_KINDS:
        assert document_kind_extensions(kind), kind
    assert not document_kind_extensions("Nope")


# ---------------------------------------------------------------------------
# Organizer routing
# ---------------------------------------------------------------------------
def test_photos_unchanged_across_layouts(tmp_path):
    from backend.engines.organizer import resolve_archive_path

    dest = tmp_path / "archive"
    photo = _item(file_name="a.jpg", extension=".jpg", date_taken=DT)
    assert resolve_archive_path(dest, photo).as_posix().endswith("2024/06-June/15/a.jpg")
    assert resolve_archive_path(dest, photo, layout="year/month").as_posix().endswith("2024/06-June/a.jpg")
    assert resolve_archive_path(dest, photo, layout="flat").as_posix().endswith("archive/a.jpg")
    assert "Documents" not in resolve_archive_path(dest, photo).as_posix()


def test_documents_routed_by_kind(tmp_path):
    from backend.engines.organizer import document_kind_for_item, is_document_item, resolve_archive_path

    dest = tmp_path / "archive"
    cases = [
        ("r.pdf", ".pdf", "PDFs"),
        ("s.xlsx", ".xlsx", "Spreadsheets"),
        ("d.pptx", ".pptx", "Presentations"),
        ("w.docx", ".docx", "Word-Docs"),
        ("n.txt", ".txt", "Text-CSV"),
        ("b.epub", ".epub", "eBooks"),
    ]
    for name, ext, kind in cases:
        item = _item(file_name=name, extension=ext, date_taken=DT)
        assert is_document_item(item)
        assert document_kind_for_item(item) == kind
        p = resolve_archive_path(dest, item).as_posix()
        assert p.endswith(f"Documents/{kind}/2024/06-June/15/{name}"), p
        pm = resolve_archive_path(dest, item, layout="year/month").as_posix()
        assert pm.endswith(f"Documents/{kind}/2024/06-June/{name}"), pm
        pf = resolve_archive_path(dest, item, layout="flat").as_posix()
        assert pf.endswith(f"Documents/{kind}/{name}"), pf


def test_documents_unsorted_under_kind(tmp_path):
    from backend.engines.organizer import resolve_archive_path

    dest = tmp_path / "archive"
    item = _item(file_name="u.pdf", extension=".pdf")
    item.date_taken = None
    item.original_capture_time = None
    p = resolve_archive_path(dest, item).as_posix()
    assert p.endswith("Documents/PDFs/_unsorted/u.pdf"), p


def test_extension_falls_back_to_filename_suffix(tmp_path):
    from backend.engines.organizer import document_kind_for_item, is_document_item

    item = _item(file_name="scan.PDF", extension=None, date_taken=DT)
    assert is_document_item(item)
    assert document_kind_for_item(item) == "PDFs"


def test_legacy_tree_fallback(tmp_path):
    from backend.engines.organizer import locate_archive_file

    dest = tmp_path / "archive"
    item = _item(file_name="r.pdf", extension=".pdf", file_size=10, date_taken=DT)
    # Pre-split file sitting in the unified media tree.
    legacy = dest / "2024" / "06-June" / "15" / "r.pdf"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"0123456789")
    found = locate_archive_file(dest, item)
    assert found is not None and found.as_posix() == legacy.as_posix()
    # New-tree file wins when both exist.
    new = dest / "Documents" / "PDFs" / "2024" / "06-June" / "15" / "r.pdf"
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_bytes(b"0123456789")
    found = locate_archive_file(dest, item)
    assert found is not None and found.as_posix() == new.as_posix()


# ---------------------------------------------------------------------------
# API: config + doc_kind filter + migration
# ---------------------------------------------------------------------------
def _seed_session_with_docs(test_client, dest: Path):
    """Seed one session: 2 legacy docs, 1 photo, 1 trashed doc. Returns ids."""
    from backend.database.manager import session_scope
    from backend.database.models import HopStatus, MediaItem, TransferSession
    from backend.engines.organizer import build_folder, derive_timestamp

    async def _seed():
        async with session_scope() as session:
            ts = TransferSession(
                session_name="docs-seed",
                source_root=str(dest / "src"),
                dest_root=str(dest),
                folder_layout="year/month/day",
            )
            session.add(ts)
            await session.flush()

            def _mk(name, ext, content: bytes, trashed=False, doc_dt=DT):
                item = MediaItem(
                    source_path=f"/src/{name}",
                    file_name=name,
                    file_size=len(content),
                    extension=ext,
                    date_taken=doc_dt if ext != ".jpg" else doc_dt,
                    hop1_status=HopStatus.COMPLETED.value,
                    hop2_status=HopStatus.COMPLETED.value,
                    final_status=HopStatus.COMPLETED.value,
                    trashed=trashed,
                    session_id=ts.id,
                )
                return item, content

            rows = [
                _mk("report.pdf", ".pdf", b"pdf-bytes-01"),
                _mk("sheet.xlsx", ".xlsx", b"xls-bytes-0123"),
                _mk("photo.jpg", ".jpg", b"jpg-bytes-01234"),
                _mk("old.pdf", ".pdf", b"trashed-doc!", trashed=True),
            ]
            ids = {}
            for item, content in rows:
                session.add(item)
                await session.flush()
                ids[item.file_name] = item.id
                # Plant each file at its legacy unified-tree location.
                folder = build_folder(dest, derive_timestamp(item), "year/month/day")
                folder.mkdir(parents=True, exist_ok=True)
                (folder / item.file_name).write_bytes(content)
            await session.commit()
            return ts.id, ids

    import asyncio

    return asyncio.run(_seed())


def test_config_exposes_document_kinds(test_client):
    body = test_client.get("/api/config").json()
    assert body["documents_dir"] == "Documents"
    assert set(["PDFs", "Spreadsheets", "Presentations", "Word-Docs", "Text-CSV", "eBooks"]) <= set(
        body["document_kinds"]
    )


def test_doc_kind_filter_and_migration(test_client, tmp_path):
    dest = tmp_path / "vault"
    dest.mkdir()
    _sid, ids = _seed_session_with_docs(test_client, dest)

    # doc_kind filter: only the xlsx matches Spreadsheets.
    r = test_client.get("/api/media", params={"doc_kind": "Spreadsheets", "page_size": 50})
    assert r.status_code == 200, r.text
    names = [i["file_name"] for i in r.json()["items"]]
    assert names == ["sheet.xlsx"], names

    # Unknown kind is a 400, not a silent empty list.
    r = test_client.get("/api/media", params={"doc_kind": "Invoices"})
    assert r.status_code == 400

    # Preview: the 2 live legacy docs (photo + trashed excluded).
    preview = test_client.get("/api/library/migrate-documents/preview").json()
    assert preview["total"] == 2, preview
    kinds = {m["file_name"]: m["kind"] for m in preview["moves"]}
    assert kinds == {"report.pdf": "PDFs", "sheet.xlsx": "Spreadsheets"}, kinds
    for m in preview["moves"]:
        assert Path(m["dest_path"]).as_posix().startswith((dest / "Documents").as_posix())
        assert Path(m["src_path"]).is_file()

    # Execute: files move, preview empties, re-run is a no-op.
    out = test_client.post("/api/library/migrate-documents/execute", json={}).json()
    assert out["moved"] == 2 and out["failed"] == 0, out
    assert (dest / "Documents" / "PDFs" / "2024" / "06-June" / "15" / "report.pdf").is_file()
    assert (dest / "Documents" / "Spreadsheets" / "2024" / "06-June" / "15" / "sheet.xlsx").is_file()
    # Photo untouched in the unified tree.
    assert (dest / "2024" / "06-June" / "15" / "photo.jpg").is_file()

    preview2 = test_client.get("/api/library/migrate-documents/preview").json()
    assert preview2["total"] == 0, preview2
    out2 = test_client.post("/api/library/migrate-documents/execute", json={}).json()
    assert out2["moved"] == 0 and out2["failed"] == 0, out2

    # Subset execute by item id still works after full migration (all skipped).
    out3 = test_client.post("/api/library/migrate-documents/execute", json={"item_ids": [ids["report.pdf"]]}).json()
    assert out3["moved"] == 0, out3


def test_doc_end_to_end_import_uses_documents_tree(test_client, tmp_path):
    """Full pipeline: a scanned PDF must land in Documents/PDFs/, a JPG beside it."""
    import time

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "note.pdf").write_bytes(b"%PDF-1.4 fake content for e2e")
    (src / "pic.jpg").write_bytes(b"\xff\xd8\xff fake-jpeg-bytes" + b"0" * 64)

    s = test_client.post(
        "/api/sessions",
        json={
            "session_name": "docs-e2e",
            "source_root": str(src),
            "dest_root": str(dst),
            "transfer_mode": "copy",
        },
    )
    assert s.status_code == 200, s.text
    sid = s.json()["id"]
    assert test_client.post(f"/api/sessions/{sid}/start").status_code == 200

    final = None
    for _ in range(60):
        time.sleep(1)
        g = test_client.get(f"/api/sessions/{sid}").json()
        if g.get("status") in ("completed", "failed"):
            final = g
            break
    assert final is not None and final.get("status") == "completed", final

    pdfs = list((dst / "Documents" / "PDFs").rglob("note.pdf"))
    assert len(pdfs) == 1, [str(p) for p in dst.rglob("*")]
    jpgs = [p for p in dst.rglob("pic.jpg") if "Documents" not in p.as_posix()]
    assert len(jpgs) == 1, [str(p) for p in dst.rglob("*")]
