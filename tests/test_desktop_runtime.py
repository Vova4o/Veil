from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import Flask

from app.desktop_data import import_legacy_library
from app.desktop_server import TOKEN_HEADER, install_desktop_guard


def test_import_captures_live_wal_and_preserves_original(tmp_path: Path):
    source = tmp_path / "legacy"
    source.mkdir()
    connection = sqlite3.connect(source / "meta.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE items (name TEXT)")
    connection.execute("INSERT INTO items VALUES ('library item')")
    connection.commit()
    (source / "config.json").write_text('{"sources": []}', encoding="utf-8")
    (source / "workflow_templates").mkdir()
    (source / "workflow_templates/user.json").write_text('{"name":"user"}', encoding="utf-8")
    destination = tmp_path / "desktop/data"
    assert import_legacy_library(source, destination)
    with sqlite3.connect(destination / "meta.db") as imported:
        assert imported.execute("SELECT name FROM items").fetchall() == [("library item",)]
    assert (source / "meta.db").exists()
    assert (destination / "workflow_templates/user.json").exists()
    assert not import_legacy_library(source, destination)
    connection.close()


def test_import_never_overwrites_existing_desktop_data(tmp_path: Path):
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "meta.db").touch()
    destination = tmp_path / "desktop"
    destination.mkdir()
    (destination / "config.json").write_text('keep', encoding="utf-8")
    assert not import_legacy_library(source, destination)
    assert (destination / "config.json").read_text() == 'keep'


def test_failed_import_does_not_publish_partial_library(tmp_path: Path):
    import pytest

    source = tmp_path / "legacy"
    source.mkdir()
    (source / "meta.db").write_bytes(b"not a database")
    destination = tmp_path / "desktop/data"
    with pytest.raises(sqlite3.DatabaseError):
        import_legacy_library(source, destination)
    assert not destination.exists()
    assert (source / "meta.db").read_bytes() == b"not a database"


def test_desktop_guard_requires_owner_token_and_keeps_range_responses():
    application = Flask(__name__)
    token = "a" * 64
    application.add_url_rule("/api/private", "private", lambda: (b"clip", 206, {"Content-Range": "bytes 0-3/10"}))
    install_desktop_guard(application, token)
    client = application.test_client()
    assert client.get("/api/private").status_code == 403
    assert client.get("/api/private", headers={TOKEN_HEADER: "wrong"}).status_code == 403
    response = client.get("/api/private", headers={TOKEN_HEADER: token})
    assert response.status_code == 206
    assert response.headers["Content-Range"] == "bytes 0-3/10"
    assert "object-src 'none'" in response.headers["Content-Security-Policy"]
    assert client.get("/_desktop/health", headers={TOKEN_HEADER: token}).json["pid"] > 0


def test_desktop_guard_preserves_only_the_state_checked_oauth_callback():
    application = Flask(__name__)
    application.add_url_rule("/api/social/vk/auth/callback", "callback", lambda: "handled", methods=["GET", "POST"])
    install_desktop_guard(application, "a" * 64)
    client = application.test_client()
    assert client.get("/api/social/vk/auth/callback").status_code == 200
    assert client.post("/api/social/vk/auth/callback").status_code == 403
