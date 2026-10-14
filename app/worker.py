from __future__ import annotations

import json
import sys
import threading
import traceback
from contextlib import closing, nullcontext
from pathlib import Path

from PIL import Image

from . import database as db
from .asset_work import serialized_asset_work
from .extractor import (
    extract_metadata,
    extract_metadata_from_bytes,
    make_thumbnail_bytes,
    make_thumbnail_bytes_from_bytes,
)
from .media import (
    MediaToolUnavailableError,
    VideoProcessingError,
    make_video_thumbnail,
    probe_video,
    temporary_media_file,
)
from .paths import build_runtime_paths, normalize_path

# Disable PIL limit globally
Image.MAX_IMAGE_PIXELS = None

_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()
_stop_event = threading.Event()
_thumbnail_dir = build_runtime_paths().thumbnails


def start_worker(thumbnail_dir: str | Path | None = None):
    global _worker_thread, _thumbnail_dir
    with _worker_lock:
        if thumbnail_dir is not None:
            _thumbnail_dir = normalize_path(thumbnail_dir)
        if _worker_thread is None or not _worker_thread.is_alive():
            _stop_event.clear()
            _worker_thread = threading.Thread(
                target=_worker_loop, daemon=True, name="MediaStudioWorker"
            )
            _worker_thread.start()


def stop_worker(*, wait: bool = False, timeout: float = 10.0) -> bool:
    _stop_event.set()
    with _worker_lock:
        worker = _worker_thread
    if wait and worker is not None and worker is not threading.current_thread():
        worker.join(timeout)
    return worker is None or not worker.is_alive()


def _next_asset_batch(batch_size: int = 50) -> list[dict]:
    with closing(db.get_conn()) as conn:
        rows = conn.execute(
            """SELECT i.id, i.rel_path, i.file_name, i.media_type, i.mime_type,
                f.path AS folder_path, i.folder_id, f.source_status
            FROM images i
            JOIN folders f ON f.id = i.folder_id
            WHERE f.status = 'processing'
              AND f.enabled = 1
              AND f.source_status IN ('available', 'partially_available')
              AND i.metadata_json IS NULL
              AND i.error IS NULL
            ORDER BY i.id ASC
            LIMIT ?""",
            (batch_size,),
        ).fetchall()
        if rows:
            return [dict(row) for row in rows]

        conn.execute(
            """UPDATE folders
            SET status = 'completed'
            WHERE status = 'processing'
              AND NOT EXISTS (
                  SELECT 1 FROM images
                  WHERE images.folder_id = folders.id
                    AND images.metadata_json IS NULL
                    AND images.error IS NULL
              )"""
        )
        conn.commit()

    return []


def _next_asset() -> dict | None:
    batch = _next_asset_batch(batch_size=1)
    return batch[0] if batch else None


def _next_image() -> dict | None:
    """Compatibility wrapper for the former image-only worker queue."""
    return _next_asset()


def _record_error(image_id: int, message: str) -> None:
    with closing(db.get_conn()) as conn:
        conn.execute(
            "UPDATE images SET error = ?, preview_status = 'error' WHERE id = ?",
            (message, image_id),
        )
        conn.commit()


def _process_video(asset_id: int, abs_path: Path, *, thumbnail: bool = True,
                   thumbnail_dir: Path | None = None) -> None:
    format_name = abs_path.suffix.lower().lstrip(".") or None
    width = 0
    height = 0
    pixel_format = None
    duration = None
    frame_rate = None
    codec = None
    try:
        probe = probe_video(abs_path)
        format_name = probe.format or format_name
        width = probe.width
        height = probe.height
        pixel_format = probe.pixel_format
        duration = probe.duration
        frame_rate = probe.frame_rate
        codec = probe.codec
        technical_metadata = probe.metadata
    except MediaToolUnavailableError as exc:
        technical_metadata = {
            "source": "ffprobe",
            "status": "unavailable",
            "error": str(exc),
        }
    except VideoProcessingError as exc:
        technical_metadata = {
            "source": "ffprobe",
            "status": "error",
            "error": str(exc),
        }

    preview_status = "ready"
    preview_error = None
    if thumbnail:
        try:
            thumb_data = make_video_thumbnail(abs_path, duration=duration)
            cache_dir = thumbnail_dir if thumbnail_dir is not None else _thumbnail_dir
            cache_dir.mkdir(parents=True, exist_ok=True)
            target = cache_dir / f"{asset_id}.jpg"
            temporary = target.with_suffix('.jpg.tmp')
            temporary.write_bytes(thumb_data)
            temporary.replace(target)
        except MediaToolUnavailableError as exc:
            preview_status = "unavailable"
            preview_error = str(exc)
        except VideoProcessingError as exc:
            preview_status = "error"
            preview_error = str(exc)

    metadata = {
        "file": abs_path.name,
        "path": str(abs_path),
        "media_type": "video",
        "technical_metadata": technical_metadata,
    }
    with closing(db.get_conn()) as conn:
        conn.execute(
            """UPDATE images SET format = ?, width = ?, height = ?, mode = ?,
                duration = ?, frame_rate = ?, codec = ?, error = NULL,
                metadata_json = ?, preview_status = ?, preview_error = ?,
                created_at = datetime('now')
            WHERE id = ?""",
            (
                format_name,
                width,
                height,
                pixel_format,
                duration,
                frame_rate,
                codec,
                json.dumps(metadata, ensure_ascii=False),
                preview_status,
                preview_error,
                asset_id,
            ),
        )
        conn.execute(
            """UPDATE folders
            SET status = 'completed'
            WHERE status = 'processing'
              AND NOT EXISTS (
                  SELECT 1 FROM images
                  WHERE images.folder_id = folders.id
                    AND images.metadata_json IS NULL
                    AND images.error IS NULL
              )"""
        )
        conn.commit()


@serialized_asset_work
def process_asset(asset_id: int, thumbnail_dir: Path, *, metadata: bool = True,
                  thumbnail: bool = True, only_pending: bool = False) -> bool:
    """Re-read ownership under the index lock; publish metadata before decoding a thumbnail."""
    with closing(db.get_conn()) as conn:
        row = conn.execute(
            """SELECT i.*, f.path AS folder_path, f.enabled
               FROM images i JOIN folders f ON f.id = i.folder_id WHERE i.id = ?""",
            (asset_id,),
        ).fetchone()
    if row is None or not row["enabled"]:
        return False
    if only_pending and (row['metadata_json'] is not None or row['error'] is not None):
        metadata = False
    original = db.get_asset_original_data(asset_id) if row["has_original_data"] else None
    path = Path(row["folder_path"]) / row["rel_path"]
    try:
        if not row['has_original_data'] and not Path(row['folder_path']).is_dir():
            db.update_source_state(row['folder_id'], 'unavailable',
                f"Source directory is unavailable: {row['folder_path']}")
            return False
        if row["has_original_data"] and original is None:
            raise FileNotFoundError("Original asset data missing")
        if row['media_type'] == 'video':
            context = temporary_media_file(original, row['file_name']) if original is not None else nullcontext(path)
            with context as video_path:
                if metadata:
                    _process_video(asset_id, video_path, thumbnail=thumbnail, thumbnail_dir=thumbnail_dir)
                elif thumbnail:
                    data = make_video_thumbnail(video_path, duration=row['duration'])
                    thumbnail_dir.mkdir(parents=True, exist_ok=True)
                    target = thumbnail_dir / f'{asset_id}.jpg'
                    temporary = target.with_suffix('.jpg.tmp')
                    temporary.write_bytes(data)
                    temporary.replace(target)
                    db.update_asset_preview_status(asset_id, 'ready')
            with closing(db.get_conn()) as conn:
                result = conn.execute('SELECT preview_status FROM images WHERE id = ?', (asset_id,)).fetchone()
            if result is None or result['preview_status'] != 'ready':
                return False
            if metadata:
                with closing(db.get_conn()) as conn:
                    dimensions = conn.execute('SELECT width, height FROM images WHERE id = ?', (asset_id,)).fetchone()
                return bool(dimensions and dimensions['width'] and dimensions['height'])
            return True
        initial_stat = None if original is not None else path.stat()
        if initial_stat is not None and (initial_stat.st_size != row['file_size']
                or abs(initial_stat.st_mtime - row['file_mtime']) >= 0.001):
            raise OSError('File changed since indexing; check it again after the source settles')

        def check_source() -> None:
            if initial_stat is not None:
                current = path.stat()
                if (initial_stat.st_size, initial_stat.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                    raise OSError('File changed while processing; retry after the source settles')

        if metadata:
            meta = (extract_metadata_from_bytes(original, row["file_name"])
                    if original is not None else extract_metadata(path))
            check_source()
            with closing(db.get_conn()) as conn:
                db._update_image_metadata(conn, asset_id, meta)
                conn.commit()
        if thumbnail:
            data = (make_thumbnail_bytes_from_bytes(original)
                    if original is not None else make_thumbnail_bytes(path))
            check_source()
            if not data:
                db.update_asset_preview_status(asset_id, "error", "Image preview generation failed")
                return False
            thumbnail_dir.mkdir(parents=True, exist_ok=True)
            target = thumbnail_dir / f"{asset_id}.jpg"
            temporary = target.with_suffix(".jpg.tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
            db.update_asset_preview_status(asset_id, "ready")
        with closing(db.get_conn()) as conn:
            error = conn.execute("SELECT error FROM images WHERE id = ?", (asset_id,)).fetchone()
        return error is not None and not error["error"]
    except Exception as exc:
        if metadata:
            _record_error(asset_id, str(exc))
        else:
            db.update_asset_preview_status(asset_id, "error", str(exc))
        return False


def _worker_loop():
    print("[Worker] Background processing loop started", flush=True)
    while not _stop_event.is_set():
        try:
            batch = _next_asset_batch()
            if not batch:
                _stop_event.wait(0.5)
                continue
            for row in batch:
                if _stop_event.is_set():
                    break
                # The queued row may have been completed by foreground detail loading or repair.
                with closing(db.get_conn()) as conn:
                    pending = conn.execute(
                        "SELECT metadata_json, error FROM images WHERE id = ?", (row["id"],)
                    ).fetchone()
                if pending is not None and pending["metadata_json"] is None and pending["error"] is None:
                    process_asset(row["id"], _thumbnail_dir, only_pending=True)
        except Exception as exc:
            print(f"[Worker Error] Loop error: {exc}", file=sys.stderr, flush=True)
            traceback.print_exc()
            _stop_event.wait(2.0)
