"""Read-through access to matching legacy render caches."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

_sources: dict[int, tuple] = {}
_cache: Path | None = None


def _signature(source: dict[str, Any]) -> tuple:
    return (source.get("folder_path"), source.get("rel_path"),
            int(source.get("file_size") or 0), float(source.get("file_mtime") or 0),
            source.get("media_type"), bool(source.get("has_original_data")),
            source.get("content_fingerprint") or "")


def configure_legacy_cache(data_dir: Path, legacy_cache: Path | None = None) -> int:
    global _cache, _sources
    _cache, _sources = None, {}
    provenance = data_dir / "desktop-import.json"
    if not provenance.is_file():
        return 0
    try:
        legacy_data = Path(json.loads(provenance.read_text(encoding="utf-8"))["source"]).resolve()
        candidate = (legacy_cache or legacy_data.parent / "cache").resolve()
        if not candidate.is_dir():
            return 0
        with closing(sqlite3.connect((data_dir / "meta.db").resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("ATTACH DATABASE ? AS legacy", ((legacy_data / "meta.db").as_uri() + "?mode=ro",))
            rows = connection.execute("""
                SELECT current.id, cf.path AS folder_path, current.rel_path,
                    current.file_mtime, current.file_size, current.media_type,
                    current.has_original_data, current.content_fingerprint
                FROM main.images current JOIN legacy.images old ON old.id = current.id
                JOIN main.folders cf ON cf.id = current.folder_id
                JOIN legacy.folders lf ON lf.id = old.folder_id
                WHERE cf.path = lf.path AND current.rel_path = old.rel_path
                    AND current.file_size = old.file_size AND current.file_mtime = old.file_mtime
                    AND current.media_type = old.media_type AND current.has_original_data = old.has_original_data
                    AND COALESCE(current.content_fingerprint, '') = COALESCE(old.content_fingerprint, '')
            """).fetchall()
        _sources = {row["id"]: _signature(dict(row)) for row in rows}
        _cache = candidate
    except (OSError, sqlite3.Error, ValueError, KeyError) as error:
        # Optional acceleration must never prevent opening the actual library.
        print(f"[Desktop] Legacy cache reuse skipped: {error}", flush=True)
    return len(_sources)


def _matches(image_id: int, source: dict[str, Any]) -> bool:
    # Recheck current source identity after rescans, replacements and edits.
    return _cache is not None and _sources.get(image_id) == _signature(source)


def legacy_thumbnail(image_id: int, source: dict[str, Any]) -> Path | None:
    if not _matches(image_id, source) or _cache is None:
        return None
    candidate = _cache / "thumbnails" / f"{image_id}.jpg"
    return candidate if candidate.is_file() else None


def legacy_preview(image_id: int, source: dict[str, Any], max_size: int) -> Path | None:
    if not _matches(image_id, source) or _cache is None:
        return None
    stem = f"{image_id}-{int(float(source.get('file_mtime') or 0) * 1000)}-{int(source.get('file_size') or 0)}-{max_size}"
    for extension in ("jpg", "webp"):
        candidate = _cache / "previews" / f"{stem}.{extension}"
        if candidate.is_file():
            return candidate
    return None
