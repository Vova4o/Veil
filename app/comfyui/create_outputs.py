"""Create results share the physical ComfyUI output source with the Viewer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from flask import current_app, has_app_context

from app import database as db
from app.config_store import ConfigStore
from app.indexing import index_source_directory
from app.media import mime_type_for_path
from app.schemas import AssetInsertRow

from .desktop import comfyui_output_dir


def ensure_output_source(store: ConfigStore) -> None:
    root = comfyui_output_dir(store.comfyui_settings())
    if root is None or not root.is_dir():
        return
    existing = next((source for source in store.sources() if source.path == root), None)
    if existing and not existing.enabled:
        return
    if existing and existing.recursive:
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT recursive FROM folders WHERE path=?", (str(root),)).fetchone()
        finally:
            conn.close()
        if row and row["recursive"]:
            return
    settings = store.add_source(root, recursive=True)
    result = index_source_directory(
        root, name=settings.name, recursive=True,
        thumbnail_dir=Path(current_app.config["THUMBNAIL_FOLDER"]),
        cutout_dir=Path(current_app.config["CUTOUT_FOLDER"]),
        preview_dir=Path(current_app.config["PREVIEW_FOLDER"]),
    )
    from app.source_monitor import get_source_monitor
    monitor = get_source_monitor()
    if monitor:
        monitor.source_added(settings, result.folder_id)


def index_output_image(output: dict[str, Any], data: bytes, embedded: dict[str, Any], extracted) -> int | None:
    """Reuse a watched file's ID, avoiding a second copy under Uploads."""
    if not has_app_context() or output.get("type", "output") != "output":
        return None
    store = current_app.config.get("CONFIG_STORE")
    if store is None:
        return None
    root = comfyui_output_dir(store.comfyui_settings())
    if root is None:
        return None
    root = root.resolve()
    path = (root / str(output.get("subfolder") or "") / Path(str(output.get("filename") or "")).name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    # A connect-only ComfyUI may use another output root. Never attach an unrelated file.
    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(data).digest():
        return None
    settings = next((source for source in store.sources() if source.path == root), None)
    if settings is None:
        settings = store.add_source(root, recursive=True)
    elif not settings.recursive:
        settings = store.update_source(root, recursive=True)
    folder_id = db.upsert_source(str(root), name=settings.name, recursive=True, enabled=settings.enabled)
    relative = path.relative_to(root).as_posix()
    if relative in db.get_tombstoned_rel_paths(folder_id):
        return None
    stat = path.stat()
    db.insert_assets(folder_id, [AssetInsertRow(
        rel_path=relative, file_name=path.name, file_size=stat.st_size, file_mtime=stat.st_mtime,
        media_type="image", mime_type=mime_type_for_path(path), format=extracted.format,
        width=extracted.size[0] if extracted.size else 0, height=extracted.size[1] if extracted.size else 0,
        mode=extracted.mode, metadata_json=json.dumps(embedded, ensure_ascii=False),
        content_fingerprint=hashlib.sha256(data).hexdigest(), preview_status="ready",
    )])
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT id FROM images WHERE folder_id=? AND rel_path=?", (folder_id, relative)).fetchone()
        return int(row["id"]) if row else None
    finally:
        conn.close()
