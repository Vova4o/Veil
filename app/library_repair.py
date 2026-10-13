"""Durable, non-destructive repair of library metadata and disposable previews."""
from __future__ import annotations

import json
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from PIL import Image

from . import database as db
from .asset_work import asset_work_lock
from .indexing import index_source_directory, scan_source_directory
from .schemas import ImageMetadata
from .worker import process_asset


def initialize(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS library_repairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL DEFAULT 'pending',
        state_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")


def get_repair(repair_id: int | None = None) -> dict[str, Any] | None:
    with closing(db.get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM library_repairs WHERE id = ?" if repair_id is not None
            else "SELECT * FROM library_repairs ORDER BY id DESC LIMIT 1",
            (repair_id,) if repair_id is not None else (),
        ).fetchone()
    if row is None:
        return None
    return {**json.loads(row['state_json']), 'id': row['id'], 'status': row['status']}


def start_repair(paths: dict[str, str], source_id: int | None = None) -> dict[str, Any]:
    with closing(db.get_conn()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute(
            "SELECT id FROM library_repairs WHERE status IN ('pending', 'running') ORDER BY id LIMIT 1"
        ).fetchone()
        if active is not None:
            conn.rollback()
            return get_repair(active['id']) or {}
        state = {'phase': 'queued', 'source_id': source_id, 'total': 0, 'checked': 0,
                 'repaired': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0, 'errors': []}
        cursor = conn.execute(
            'INSERT INTO library_repairs (state_json) VALUES (?)', (json.dumps(state),)
        )
        repair_id = int(cursor.lastrowid)
        db.enqueue_job_conn(conn, 'repair_library', {'repair_id': repair_id, 'paths': paths})
        conn.commit()
    return get_repair(repair_id) or {}


def _save(repair_id: int, state: dict[str, Any], status: str = 'running') -> None:
    with closing(db.get_conn()) as conn:
        conn.execute(
            "UPDATE library_repairs SET state_json = ?, status = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(state), status, repair_id),
        )
        conn.commit()


def _valid_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        # JPEG verify checks headers; loading also catches truncated pixel data.
        with Image.open(path) as image:
            image.load()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def _metadata_needs_repair(row) -> bool:
    if row['error'] or not row['metadata_json'] or not row['width'] or not row['height']:
        return True
    try:
        parsed = json.loads(row['metadata_json'])
        if not isinstance(parsed, dict):
            return True
        if row['media_type'] == 'image':
            ImageMetadata.model_validate(parsed)
        else:
            technical = parsed.get('technical_metadata')
            if not isinstance(technical, dict) or technical.get('status') in ('unavailable', 'error'):
                return True
        return False
    except (ValueError, TypeError):
        return True


def run_repair(payload: dict[str, Any], *, stop_requested=lambda: False) -> None:
    repair_id = int(payload['repair_id'])
    previous = get_repair(repair_id)
    if previous is None or previous['status'] == 'completed':
        return
    state = {'phase': 'checking_sources', 'source_id': previous['source_id'], 'total': 0,
             'checked': 0, 'repaired': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0, 'errors': []}
    thumbnails = Path(payload['paths']['thumbnails'])
    previews = Path(payload['paths']['previews'])
    cutouts = Path(payload['paths']['cutouts'])
    _save(repair_id, state)

    def error(message: str) -> None:
        if len(state['errors']) < 20:
            state['errors'].append(message)

    # Replayed jobs check current derived data again; valid results are retained.
    sources = db.get_source_records()
    unsettled_sources = set()
    for source in sources:
        if stop_requested():
            return
        if state['source_id'] is not None and source['id'] != state['source_id']:
            continue
        if not source['enabled']:
            continue
        root = Path(source['path'])
        if not root.is_dir():
            error(f"{source['name']}: source unavailable")
            continue
        try:
            first = scan_source_directory(root, recursive=bool(source['recursive']))
            time.sleep(0.1)
            if stop_requested():
                return
            second = scan_source_directory(root, recursive=bool(source['recursive']))
            if first.signature != second.signature:
                unsettled_sources.add(source['id'])
                error(f"{source['name']}: files are changing; try again after syncing finishes")
                continue
            with asset_work_lock:
                current_source = db.get_folder_record(source['id'])
                if (current_source is None or not current_source['enabled']
                        or current_source['recursive'] != source['recursive']):
                    unsettled_sources.add(source['id'])
                    continue
                result = index_source_directory(root, thumbnail_dir=thumbnails,
                    preview_dir=previews, cutout_dir=cutouts, name=current_source['name'],
                    recursive=bool(source['recursive']), snapshot=second, prune_missing=False)
            for message in result.errors:
                error(message)
        except OSError as exc:
            error(f"{source['name']}: {exc}")

    with closing(db.get_conn()) as conn:
        rows = conn.execute(
            """SELECT i.id FROM images i JOIN folders f ON f.id = i.folder_id
               WHERE f.enabled = 1 AND (? IS NULL OR f.id = ?) ORDER BY i.id""",
            (state['source_id'], state['source_id']),
        ).fetchall()
    state['total'] = len(rows)
    state['phase'] = 'checking_assets'
    _save(repair_id, state)
    preview_files: dict[int, list[Path]] = {}
    if previews.is_dir():
        for path in previews.iterdir():
            prefix = path.name.split('-', 1)[0]
            if prefix.isdecimal() and path.suffix in ('.jpg', '.webp'):
                preview_files.setdefault(int(prefix), []).append(path)
    for item in rows:
        if stop_requested():
            return
        asset_id = int(item['id'])
        with asset_work_lock:
            with closing(db.get_conn()) as conn:
                row = conn.execute(
                    """SELECT i.*, f.path AS folder_path, f.enabled FROM images i
                       JOIN folders f ON f.id = i.folder_id WHERE i.id = ?""", (asset_id,)
                ).fetchone()
            if row is None or not row['enabled'] or row['folder_id'] in unsettled_sources:
                state['skipped'] += 1
            elif not row['has_original_data'] and not (Path(row['folder_path']) / row['rel_path']).is_file():
                state['skipped'] += 1
                error(f"{row['file_name']}: original unavailable")
            else:
                metadata = _metadata_needs_repair(row)
                thumbnail = not _valid_image(thumbnails / f'{asset_id}.jpg')
                status_needs_repair = not thumbnail and row['preview_status'] != 'ready'
                if status_needs_repair:
                    db.update_asset_preview_status(asset_id, 'ready')
                bad_previews = [path for path in preview_files.get(asset_id, [])
                                if not _valid_image(path)]
                for path in bad_previews:
                    path.unlink(missing_ok=True)
                repaired = metadata or thumbnail or status_needs_repair or bool(bad_previews)
                success = True
                if metadata or thumbnail:
                    success = process_asset(asset_id, thumbnails, metadata=metadata, thumbnail=thumbnail)
                    if not success:
                        state['failed'] += 1
                        error(f"{row['file_name']}: metadata or preview could not be repaired")
                if repaired:
                    if success:
                        state['repaired'] += 1
                    # Folder revision also invalidates details after repairing corrupt JSON.
                    db.mark_folder_scanned(row['folder_id'], changed=True)
                else:
                    state['unchanged'] += 1
        state['checked'] += 1
        _save(repair_id, state)
    state['phase'] = 'finished'
    with closing(db.get_conn()) as conn:
        conn.execute("""UPDATE folders SET status = 'completed'
            WHERE status = 'processing' AND NOT EXISTS (
                SELECT 1 FROM images WHERE images.folder_id = folders.id
                  AND images.metadata_json IS NULL AND images.error IS NULL
            )""")
        conn.commit()
    _save(repair_id, state, 'completed')
