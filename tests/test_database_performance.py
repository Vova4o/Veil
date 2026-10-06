from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import database as db


@pytest.fixture
def temp_db(tmp_path: Path):
    old_db_path = db.get_db_path()
    db_file = tmp_path / "test_perf.db"
    db.set_db_path(db_file)
    db.init_db()
    yield db_file
    db.set_db_path(old_db_path)


def test_composite_indexes_exist(temp_db):
    """Verify that all new performance composite indexes are created on DB init."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        index_names = {r["name"] for r in rows}

        expected_indexes = {
            "idx_images_mtime_id",
            "idx_images_media_mtime_id",
            "idx_images_folder_media_mtime_id",
            "idx_images_rating_media_mtime",
            "idx_images_folder_rating_media_mtime",
            "idx_album_images_album_image",
            "idx_album_images_album_pos",
            "idx_images_indexed_at",
            "idx_images_pending_processing",
        }
        for expected in expected_indexes:
            assert expected in index_names, f"Missing expected index: {expected}"
    finally:
        conn.close()


def test_connection_pragmas_and_cache(temp_db):
    """Verify that get_conn applies tuned PRAGMAs: cache_size, mmap, busy_timeout, WAL."""
    conn = db.get_conn()
    try:
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        sync_mode = conn.execute("PRAGMA synchronous").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        cache_size = conn.execute("PRAGMA cache_size").fetchone()[0]
        mmap_size = conn.execute("PRAGMA mmap_size").fetchone()[0]

        assert journal_mode == "wal"
        assert sync_mode == 1  # NORMAL
        assert foreign_keys == 1
        assert busy_timeout == 30000
        assert cache_size == -64000
        assert mmap_size == 268435456
    finally:
        conn.close()


def test_explain_query_plan_rating_filter_uses_expression_index(temp_db):
    """Verify rating filters use expression indexes without temp B-Tree sorts."""
    folder_id = db.upsert_folder("C:/test_perf_folder_rating")
    conn = db.get_conn()
    try:
        conn.executemany(
            """INSERT INTO images (folder_id, rel_path, file_name, file_mtime, media_type, rating)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (folder_id, f"file_{i}.jpg", f"file_{i}.jpg", 1000.0 + i, "image", i % 6 or None)
                for i in range(50)
            ],
        )
        conn.commit()

        # 1. Rating-only filter across all folders
        plan_rows_rating = conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT i2.id FROM images i2
               JOIN folders f2 ON f2.id = i2.folder_id
               WHERE f2.enabled = 1 AND i2.media_type IN ('image') AND COALESCE(i2.rating, 0) = ?
               ORDER BY i2.file_mtime DESC, i2.id DESC LIMIT 50 OFFSET 0""",
            (3,),
        ).fetchall()
        plan_text_rating = " ".join(r["detail"] for r in plan_rows_rating).lower()
        assert "idx_images_rating_media_mtime" in plan_text_rating or "using index" in plan_text_rating
        assert "use temp b-tree for order by" not in plan_text_rating

        # 2. Folder + rating filter
        plan_rows_folder_rating = conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT i2.id FROM images i2
               JOIN folders f2 ON f2.id = i2.folder_id
               WHERE i2.folder_id = ? AND f2.enabled = 1 AND i2.media_type IN ('image') AND COALESCE(i2.rating, 0) = ?
               ORDER BY i2.file_mtime DESC, i2.id DESC LIMIT 50 OFFSET 0""",
            (folder_id, 3),
        ).fetchall()
        plan_text_folder_rating = " ".join(r["detail"] for r in plan_rows_folder_rating).lower()
        assert "idx_images_folder_rating_media_mtime" in plan_text_folder_rating or "using index" in plan_text_folder_rating
        assert "use temp b-tree for order by" not in plan_text_folder_rating
    finally:
        conn.close()


def test_explain_query_plan_uses_indexes(temp_db):
    """Verify EXPLAIN QUERY PLAN confirms index scan usage without temp B-Tree sorts."""
    folder_id = db.upsert_folder("C:/test_perf_folder")
    conn = db.get_conn()
    try:
        # Populate dummy data
        conn.executemany(
            """INSERT INTO images (folder_id, rel_path, file_name, file_mtime, media_type)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (folder_id, f"file_{i}.jpg", f"file_{i}.jpg", 1000.0 + i, "image")
                for i in range(50)
            ],
        )
        conn.commit()

        # Run EXPLAIN QUERY PLAN on the deferred join subquery
        plan_rows = conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT i2.id FROM images i2
               JOIN folders f2 ON f2.id = i2.folder_id
               WHERE f2.enabled = 1 AND i2.media_type IN ('image')
               ORDER BY i2.file_mtime DESC, i2.id DESC LIMIT 50 OFFSET 0"""
        ).fetchall()

        plan_text = " ".join(r["detail"] for r in plan_rows).lower()
        # Verify index scan is mentioned and no temp B-tree is created for sorting
        assert "idx_images_media_mtime_id" in plan_text or "idx_images_mtime_id" in plan_text or "using index" in plan_text
        assert "use temp b-tree for order by" not in plan_text
    finally:
        conn.close()


def test_deferred_join_correctness_and_ordering(temp_db):
    """Verify that get_images_page returns items in exact descending order of mtime."""
    folder_id = db.upsert_folder("C:/test_correctness_folder")
    conn = db.get_conn()
    try:
        records = [
            (folder_id, f"img_{i}.png", f"img_{i}.png", 100.0 + i, "image")
            for i in range(30)
        ]
        conn.executemany(
            "INSERT INTO images (folder_id, rel_path, file_name, file_mtime, media_type) VALUES (?, ?, ?, ?, ?)",
            records,
        )
        conn.commit()

        page1 = db.get_images_page(folder_id, page=1, per_page=10, sort_by="date", sort_dir="desc")
        assert len(page1.images) == 10
        assert page1.total == 30
        assert page1.images[0].file_name == "img_29.png"
        assert page1.images[9].file_name == "img_20.png"

        page2 = db.get_images_page(folder_id, page=2, per_page=10, sort_by="date", sort_dir="desc")
        assert len(page2.images) == 10
        assert page2.images[0].file_name == "img_19.png"
        assert page2.images[9].file_name == "img_10.png"
    finally:
        conn.close()


def test_keyset_cursor_pagination(temp_db):
    """Verify cursor pagination (cursor_mtime, cursor_id) returns consecutive items without offset."""
    folder_id = db.upsert_folder("C:/test_cursor_folder")
    conn = db.get_conn()
    try:
        # Create records with duplicate mtimes to test tie-breaking by ID
        records = [
            (folder_id, f"file_{i}.jpg", f"file_{i}.jpg", 500.0 + (i // 2), "image")
            for i in range(20)
        ]
        conn.executemany(
            "INSERT INTO images (folder_id, rel_path, file_name, file_mtime, media_type) VALUES (?, ?, ?, ?, ?)",
            records,
        )
        conn.commit()

        page1 = db.get_images_page(folder_id, page=1, per_page=5, sort_by="date", sort_dir="desc")
        assert len(page1.images) == 5

        # Fetch mtime and ID of last item in page 1
        last_item_id = page1.images[-1].id
        last_item_mtime = conn.execute("SELECT file_mtime FROM images WHERE id = ?", (last_item_id,)).fetchone()["file_mtime"]

        # Fetch page 2 using cursor
        page2_cursor = db.get_images_page(
            folder_id,
            page=1,
            per_page=5,
            sort_by="date",
            sort_dir="desc",
            cursor_mtime=last_item_mtime,
            cursor_id=last_item_id,
        )

        assert len(page2_cursor.images) == 5
        # Ensure no overlap between page 1 and page 2
        page1_ids = {img.id for img in page1.images}
        page2_ids = {img.id for img in page2_cursor.images}
        assert page1_ids.isdisjoint(page2_ids)
    finally:
        conn.close()


def test_large_offset_performance_benchmark(temp_db):
    """Verify that deep pagination (e.g. 5,000 items offset) executes sub-second."""
    folder_id = db.upsert_folder("C:/test_benchmark_folder")
    conn = db.get_conn()
    try:
        # Insert 3,000 rows
        records = [
            (folder_id, f"bench_{i}.png", f"bench_{i}.png", 1000.0 + i, "image")
            for i in range(3000)
        ]
        conn.executemany(
            "INSERT INTO images (folder_id, rel_path, file_name, file_mtime, media_type) VALUES (?, ?, ?, ?, ?)",
            records,
        )
        conn.commit()

        t0 = time.monotonic()
        res = db.get_images_page(folder_id=None, page=50, per_page=50, sort_by="date", sort_dir="desc")
        elapsed_ms = (time.monotonic() - t0) * 1000.0

        assert len(res.images) == 50
        assert res.total == 3000
        # Deep pagination query must execute in < 200 ms
        assert elapsed_ms < 200.0, f"Query took too long: {elapsed_ms:.2f} ms"
    finally:
        conn.close()


def test_batch_generation_stability_with_identical_mtimes(temp_db):
    """Verify that multiple items with identical mtime sort consistently by ID in descending order."""
    folder_id = db.upsert_folder("C:/test_batch_folder")
    conn = db.get_conn()
    try:
        # Simulate a batch of 6 images generated in the same second
        records = [
            (folder_id, f"batch_000{i}.png", f"batch_000{i}.png", 1500.0, "image")
            for i in range(1, 7)
        ]
        conn.executemany(
            "INSERT INTO images (folder_id, rel_path, file_name, file_mtime, media_type) VALUES (?, ?, ?, ?, ?)",
            records,
        )
        conn.commit()

        # In DESC order (newest first), the latest created item (highest ID) should come first
        res = db.get_images_page(folder_id, page=1, per_page=10, sort_by="date", sort_dir="desc")
        assert len(res.images) == 6
        file_names = [img.file_name for img in res.images]
        assert file_names == [
            "batch_0006.png",
            "batch_0005.png",
            "batch_0004.png",
            "batch_0003.png",
            "batch_0002.png",
            "batch_0001.png",
        ]

        # In ASC order (oldest first), the earliest created item (lowest ID) should come first
        res_asc = db.get_images_page(folder_id, page=1, per_page=10, sort_by="date", sort_dir="asc")
        file_names_asc = [img.file_name for img in res_asc.images]
        assert file_names_asc == [
            "batch_0001.png",
            "batch_0002.png",
            "batch_0003.png",
            "batch_0004.png",
            "batch_0005.png",
            "batch_0006.png",
        ]
    finally:
        conn.close()


def test_case_insensitive_name_sorting(temp_db):
    """Verify that name sorting is case-insensitive (COLLATE NOCASE)."""
    folder_id = db.upsert_folder("C:/test_nocase_folder")
    conn = db.get_conn()
    try:
        records = [
            (folder_id, "b.png", "b.png", 100.0, "image"),
            (folder_id, "A.png", "A.png", 100.0, "image"),
            (folder_id, "c.png", "c.png", 100.0, "image"),
            (folder_id, "B2.png", "B2.png", 100.0, "image"),
        ]
        conn.executemany(
            "INSERT INTO images (folder_id, rel_path, file_name, file_mtime, media_type) VALUES (?, ?, ?, ?, ?)",
            records,
        )
        conn.commit()

        res_asc = db.get_images_page(folder_id, page=1, per_page=10, sort_by="name", sort_dir="asc")
        names_asc = [img.file_name for img in res_asc.images]
        assert names_asc == ["A.png", "b.png", "B2.png", "c.png"]

        res_desc = db.get_images_page(folder_id, page=1, per_page=10, sort_by="name", sort_dir="desc")
        names_desc = [img.file_name for img in res_desc.images]
        assert names_desc == ["c.png", "B2.png", "b.png", "A.png"]
    finally:
        conn.close()


def test_asset_blobs_table_and_columns_exist(temp_db):
    """Verify that asset_blobs table exists and rudiments (original_data, thumbnail_b64) are removed from images."""
    conn = db.get_conn()
    try:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "asset_blobs" in tables

        image_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(images)").fetchall()
        }
        assert "has_original_data" in image_cols
        assert "original_data" not in image_cols
        assert "thumbnail_b64" not in image_cols

        indexes = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_images_has_original_data" in indexes
    finally:
        conn.close()


def test_blob_storage_streaming_and_cascade_delete(temp_db):
    """Verify BLOB storage in asset_blobs, streaming via iter_asset_original_data, and CASCADE delete."""
    binary_payload = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"X" * 1024
    image_id, folder_id = db.insert_upload_asset(
        "uploaded_test.webp",
        binary_payload,
        media_type="image",
        width=64,
        height=64,
    )

    conn = db.get_conn()
    try:
        # Check images row has has_original_data = 1
        img_row = conn.execute(
            "SELECT has_original_data FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        assert img_row is not None
        assert img_row["has_original_data"] == 1

        # Check asset_blobs row exists
        blob_row = conn.execute(
            "SELECT data FROM asset_blobs WHERE image_id = ?", (image_id,)
        ).fetchone()
        assert blob_row is not None
        assert bytes(blob_row["data"]) == binary_payload

        # Test get_asset_original_data API
        retrieved = db.get_asset_original_data(image_id)
        assert retrieved == binary_payload

        # Test streaming via iter_asset_original_data
        chunks = list(db.iter_asset_original_data(image_id, chunk_size=256))
        assert b"".join(chunks) == binary_payload

        # Test stats
        stats = db.get_diagnostics()
        assert stats["uploads"] == 1

        # Test CASCADE delete
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        conn.commit()

        # asset_blobs row must be automatically removed
        blob_after = conn.execute(
            "SELECT COUNT(*) AS c FROM asset_blobs WHERE image_id = ?", (image_id,)
        ).fetchone()
        assert blob_after["c"] == 0
        assert db.get_asset_original_data(image_id) is None
    finally:
        conn.close()


def test_legacy_blob_and_thumbnail_migration(tmp_path: Path):
    """Verify automated migration from legacy schemas with original_data and thumbnail_b64 in images."""
    legacy_db_file = tmp_path / "legacy_migration.db"
    old_path = db.get_db_path()
    try:
        # Create legacy table manually
        import sqlite3
        raw_conn = sqlite3.connect(legacy_db_file)
        raw_conn.execute("PRAGMA foreign_keys = ON")
        raw_conn.executescript(
            """
            CREATE TABLE folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
                rel_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                file_mtime REAL DEFAULT 0,
                format TEXT,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                mode TEXT,
                error TEXT,
                metadata_json TEXT,
                thumbnail_b64 TEXT,
                original_data BLOB,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(folder_id, rel_path)
            );
            INSERT INTO folders (id, path, name) VALUES (1, '__uploads__', 'Uploads');
            INSERT INTO images (id, folder_id, rel_path, file_name, original_data, thumbnail_b64)
            VALUES (10, 1, 'legacy.png', 'legacy.png', X'DEADBEEF', 'old_thumb_b64');
            INSERT INTO images (id, folder_id, rel_path, file_name, original_data, thumbnail_b64)
            VALUES (11, 1, 'no_blob.png', 'no_blob.png', NULL, NULL);
            """
        )
        raw_conn.commit()
        raw_conn.close()

        # Run init_db() which runs migrations
        db.set_db_path(legacy_db_file)
        db.init_db()

        conn = db.get_conn()
        try:
            # Check asset_blobs has migrated binary
            migrated_blob = conn.execute(
                "SELECT data FROM asset_blobs WHERE image_id = 10"
            ).fetchone()
            assert migrated_blob is not None
            assert bytes(migrated_blob["data"]) == bytes.fromhex("DEADBEEF")

            # Check has_original_data flags
            img_10 = conn.execute(
                "SELECT has_original_data FROM images WHERE id = 10"
            ).fetchone()
            assert img_10["has_original_data"] == 1

            img_11 = conn.execute(
                "SELECT has_original_data FROM images WHERE id = 11"
            ).fetchone()
            assert img_11["has_original_data"] == 0

            # Verify columns original_data and thumbnail_b64 were dropped
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(images)").fetchall()
            }
            assert "original_data" not in cols
            assert "thumbnail_b64" not in cols
            assert "has_original_data" in cols
        finally:
            conn.close()
    finally:
        db.set_db_path(old_path)


def test_get_images_page_extracts_prompt_parameters_without_overfetching(temp_db):
    """Verify get_images_page extracts prompt_parameters without loading giant workflow JSON."""
    import json
    folder_id = db.upsert_folder("C:/test_perf_overfetching_images")
    conn = db.get_conn()
    try:
        # Create a large workflow payload (simulating complex ComfyUI graph ~500 KB)
        large_workflow = {
            "prompt_parameters": {"model": "sd_xl_base.safetensors", "steps": 30, "cfg": 7.5},
            "workflow": {
                "nodes": [{"id": i, "type": "KSampler", "data": "x" * 2000} for i in range(250)]
            },
        }
        large_json_str = json.dumps(large_workflow)

        small_meta = json.dumps({"file": "small.png", "prompt_parameters": None, "workflow": None})

        conn.execute(
            """INSERT INTO images (folder_id, rel_path, file_name, file_mtime, media_type, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (folder_id, "large.png", "large.png", 2000.0, "image", large_json_str),
        )
        conn.execute(
            """INSERT INTO images (folder_id, rel_path, file_name, file_mtime, media_type, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (folder_id, "small.png", "small.png", 1000.0, "image", small_meta),
        )
        conn.commit()

        page = db.get_images_page(folder_id=folder_id, page=1, per_page=10, sort_by="date", sort_dir="desc")
        assert len(page.images) == 2
        assert page.images[0].file_name == "large.png"
        assert page.images[0].prompt_parameters == {"model": "sd_xl_base.safetensors", "steps": 30, "cfg": 7.5}

        assert page.images[1].file_name == "small.png"
        assert page.images[1].prompt_parameters is None
    finally:
        conn.close()


def test_get_assets_deferred_join_and_flag_computation_without_overfetching(temp_db):
    """Verify get_assets computes metadata flags via SQL and preserves deferred join order."""
    import json
    from app import library as media_library

    folder_id = db.upsert_folder("C:/test_perf_overfetching_library")
    conn = db.get_conn()
    try:
        # 1. ComfyUI image with large workflow
        img_meta = json.dumps({
            "file": "comfy.png",
            "prompt_parameters": {"prompt": "masterpiece"},
            "workflow": {"nodes": [{"id": 1}]},
        })
        # 2. Plain image without generation metadata
        plain_meta = json.dumps({"file": "plain.jpg"})
        # 3. Video with available technical metadata
        video_avail_meta = json.dumps({
            "media_type": "video",
            "technical_metadata": {"status": "available", "duration": 12.0},
        })
        # 4. Video with unavailable technical metadata
        video_unavail_meta = json.dumps({
            "media_type": "video",
            "technical_metadata": {"status": "unavailable"},
        })
        # 5. Image with AI annotations
        ai_annot = json.dumps({"reconstructed_prompt": "cyberpunk city"})

        items = [
            (folder_id, "1_comfy.png", "1_comfy.png", 500.0, "image", img_meta, None),
            (folder_id, "2_plain.jpg", "2_plain.jpg", 400.0, "image", plain_meta, None),
            (folder_id, "3_video_ok.mp4", "3_video_ok.mp4", 300.0, "video", video_avail_meta, None),
            (folder_id, "4_video_bad.mp4", "4_video_bad.mp4", 200.0, "video", video_unavail_meta, None),
            (folder_id, "5_annotated.png", "5_annotated.png", 100.0, "image", None, ai_annot),
        ]
        conn.executemany(
            """INSERT INTO images (folder_id, rel_path, file_name, file_mtime, media_type, metadata_json, ai_annotations_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            items,
        )
        conn.commit()

        # Call get_assets
        res = media_library.get_assets(per_page=10, sort_by="date", sort_dir="desc")
        assert res["total"] == 5
        by_name = {a["file_name"]: a for a in res["assets"]}

        # Check neither metadata_json nor ai_annotations_json are present
        for asset in res["assets"]:
            assert "metadata_json" not in asset
            assert "ai_annotations_json" not in asset

        # Item 1: ComfyUI
        assert by_name["1_comfy.png"]["has_metadata"] is True
        assert by_name["1_comfy.png"]["has_embedded_metadata"] is True
        assert by_name["1_comfy.png"]["has_ai_annotations"] is False

        # Item 2: Plain
        assert by_name["2_plain.jpg"]["has_metadata"] is False
        assert by_name["2_plain.jpg"]["has_embedded_metadata"] is True
        assert by_name["2_plain.jpg"]["has_ai_annotations"] is False

        # Item 3: Video available
        assert by_name["3_video_ok.mp4"]["has_metadata"] is False
        assert by_name["3_video_ok.mp4"]["has_embedded_metadata"] is True
        assert by_name["3_video_ok.mp4"]["has_ai_annotations"] is False

        # Item 4: Video unavailable
        assert by_name["4_video_bad.mp4"]["has_metadata"] is False
        assert by_name["4_video_bad.mp4"]["has_embedded_metadata"] is False
        assert by_name["4_video_bad.mp4"]["has_ai_annotations"] is False

        # Item 5: Annotated
        assert by_name["5_annotated.png"]["has_metadata"] is False
        assert by_name["5_annotated.png"]["has_embedded_metadata"] is False
        assert by_name["5_annotated.png"]["has_ai_annotations"] is True

        # Check deferred join preserved descending date order
        assert [a["file_name"] for a in res["assets"]] == [
            "1_comfy.png",
            "2_plain.jpg",
            "3_video_ok.mp4",
            "4_video_bad.mp4",
            "5_annotated.png",
        ]
    finally:
        conn.close()



