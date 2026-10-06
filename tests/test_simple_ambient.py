from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app import database as db
from app.comfyui import simple_routes
from app.main import app


@pytest.fixture
def ambient_library(tmp_path):
    previous = db.get_db_path()
    db.set_db_path(tmp_path / "library.sqlite")
    db.init_db()
    try:
        yield tmp_path
    finally:
        db.set_db_path(previous)


def add_image(folder_id: int, directory: Path, name: str, *, missing=False, media_type="image", original=None):
    if not missing and original is None:
        directory.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 3), "green").save(directory / name)
    conn = db.get_conn()
    try:
        image_id = conn.execute(
            """INSERT INTO images (folder_id, rel_path, file_name, media_type, width, height, original_data)
               VALUES (?, ?, ?, ?, 4, 3, ?) RETURNING id""",
            (folder_id, name, name, media_type, original),
        ).fetchone()["id"]
        conn.commit()
        return image_id
    finally:
        conn.close()


def test_ambient_empty_library_never_reuses_a_previous_folder(ambient_library):
    source = ambient_library / "arbitrary-folder"
    folder_id = db.upsert_source(str(source))
    add_image(folder_id, source, "one.png")
    client = app.test_client()
    assert len(client.get("/api/simple/ambient").get_json()["items"]) == 1
    db.delete_folder(folder_id)
    assert client.get("/api/simple/ambient").get_json()["items"] == []


def test_ambient_uses_only_available_images_from_current_sources(ambient_library):
    source = ambient_library / "user selected folder"
    folder_id = db.upsert_source(str(source))
    good_id = add_image(folder_id, source, "good.png")
    add_image(folder_id, source, "missing.png", missing=True)
    add_image(folder_id, source, "video.png", media_type="video")
    disabled = ambient_library / "paused folder"
    disabled_id = db.upsert_source(str(disabled), enabled=False)
    add_image(disabled_id, disabled, "paused.png")
    assert {item["id"] for item in simple_routes._ambient_payload()} == {good_id}


def test_ambient_follows_registered_path_changes_and_new_folders(ambient_library):
    source = ambient_library / "first location"
    folder_id = db.upsert_source(str(source))
    image_id = add_image(folder_id, source, "one.png")
    assert [item["id"] for item in simple_routes._ambient_payload()] == [image_id]
    target = ambient_library / "new location"
    source.rename(target)
    conn = db.get_conn()
    try:
        conn.execute("UPDATE folders SET path = ? WHERE id = ?", (str(target), folder_id))
        conn.commit()
    finally:
        conn.close()
    assert [item["id"] for item in simple_routes._ambient_payload()] == [image_id]
    second = ambient_library / "second folder"
    second_id = db.upsert_source(str(second))
    second_image_id = add_image(second_id, second, "two.png")
    assert {item["id"] for item in simple_routes._ambient_payload(2)} == {image_id, second_image_id}


def test_ambient_random_pool_includes_small_and_large_folders(ambient_library):
    large = ambient_library / "large"
    large_id = db.upsert_source(str(large))
    for index in range(120):
        add_image(large_id, large, f"{index}.png")
    small = ambient_library / "small"
    small_id = db.upsert_source(str(small))
    small_image_id = add_image(small_id, small, "small.png")
    for _ in range(5):
        items = simple_routes._ambient_payload(2)
        assert len(items) == 2
        assert small_image_id in {item["id"] for item in items}


def test_ambient_can_use_uploaded_images_without_physical_folders(ambient_library):
    folder_id = db.upsert_source("__uploads__")
    original = io.BytesIO()
    Image.new("RGB", (4, 3), "blue").save(original, format="PNG")
    image_id = add_image(folder_id, ambient_library, "uploaded.png", original=original.getvalue())
    assert [item["id"] for item in simple_routes._ambient_payload()] == [image_id]
