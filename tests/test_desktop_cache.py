import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.desktop_cache import configure_legacy_cache, legacy_preview, legacy_thumbnail


@pytest.fixture(autouse=True)
def reset_cache_registration(tmp_path):
    yield
    configure_legacy_cache(tmp_path / "none")


def make_library(directory: Path, *, changed: bool = False):
    directory.mkdir(parents=True)
    with closing(sqlite3.connect(directory / "meta.db")) as connection:
        connection.executescript("""
            CREATE TABLE folders (id INTEGER PRIMARY KEY, path TEXT);
            INSERT INTO folders VALUES (1, 'same-folder');
            CREATE TABLE images (id INTEGER PRIMARY KEY, folder_id INTEGER, rel_path TEXT,
                file_size INTEGER, file_mtime REAL, media_type TEXT, has_original_data INTEGER,
                content_fingerprint TEXT);
            INSERT INTO images VALUES (1, 1, 'a.png', 100, 1.5, 'image', 0, 'a');
            INSERT INTO images VALUES (2, 1, 'b.png', 100, 2, 'image', 0, 'b');
            INSERT INTO images VALUES (3, 1, 'c.png', 100, 3, 'image', 0, 'c');
        """)
        if changed:
            connection.execute("UPDATE images SET file_mtime = 9 WHERE id = 2")
        connection.commit()


def test_cache_reuse_checks_source_version_and_preserves_existing_files(tmp_path: Path):
    legacy = tmp_path / "legacy/data"
    destination = tmp_path / "desktop/data"
    make_library(legacy)
    make_library(destination, changed=True)
    (destination / "desktop-import.json").write_text(json.dumps({"source": str(legacy)}))
    source_cache = legacy.parent / "cache"
    (source_cache / "thumbnails").mkdir(parents=True)
    (source_cache / "previews").mkdir()
    for image_id in (1, 2, 3, 99):
        (source_cache / "thumbnails" / f"{image_id}.jpg").write_bytes(b"legacy")
    (source_cache / "previews/1-1500-100-4096.jpg").write_bytes(b"valid preview")
    (source_cache / "previews/1-1400-100-4096.jpg").write_bytes(b"stale preview")
    target_cache = destination.parent / "cache"
    (target_cache / "thumbnails").mkdir(parents=True)
    (target_cache / "thumbnails/3.jpg").write_bytes(b"keep current")
    assert configure_legacy_cache(destination) == 2
    source = {"folder_path": "same-folder", "rel_path": "a.png", "file_size": 100,
              "file_mtime": 1.5, "media_type": "image", "has_original_data": False, "content_fingerprint": "a"}
    assert legacy_thumbnail(1, source) == source_cache / "thumbnails/1.jpg"
    assert legacy_preview(1, source, 4096) == source_cache / "previews/1-1500-100-4096.jpg"
    assert legacy_thumbnail(2, {**source, "rel_path": "b.png", "file_mtime": 9, "content_fingerprint": "b"}) is None
    assert legacy_thumbnail(99, source) is None
    assert legacy_thumbnail(1, {**source, "file_mtime": 2}) is None
    assert legacy_thumbnail(1, {**source, "content_fingerprint": "changed"}) is None
    assert not (target_cache / "thumbnails/1.jpg").exists()
    assert (target_cache / "thumbnails/3.jpg").read_bytes() == b"keep current"
    assert not (target_cache / "thumbnails/2.jpg").exists()
    assert not (target_cache / "thumbnails/99.jpg").exists()
    assert not (target_cache / "previews/1-1400-100-4096.jpg").exists()
    assert (source_cache / "thumbnails/1.jpg").read_bytes() == b"legacy"
    (source_cache / "thumbnails/1.jpg").unlink()
    assert legacy_thumbnail(1, source) is None


def test_missing_legacy_cache_does_not_block_or_mark_import(tmp_path: Path):
    destination = tmp_path / "data"
    destination.mkdir()
    (destination / "desktop-import.json").write_text(json.dumps({"source": str(tmp_path / "missing/data")}))
    cache = tmp_path / "cache"
    assert configure_legacy_cache(destination) == 0
    assert not (cache / "desktop-cache-import-v1.json").exists()
