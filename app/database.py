from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .asset_work import serialized_asset_work
from .paths import build_runtime_paths, normalize_path, portable_filename
from .schemas import (
    AssetInsertRow,
    FolderInfo,
    ImageDetail,
    ImageInsertRow,
    ImageListItem,
    ImageMetadata,
    ImagesResponse,
)

_DB_PATH: str | None = None
_connection_condition = threading.Condition()
_active_connections = 0
_maintenance_owner: int | None = None


class DatabaseMaintenanceError(RuntimeError):
    """Raised when the index is temporarily unavailable for maintenance."""


class _TrackedConnection(sqlite3.Connection):
    _mediastudio_counted = False

    def close(self) -> None:
        counted = self._mediastudio_counted
        try:
            super().close()
        finally:
            if counted:
                self._mediastudio_counted = False
                _release_connection()


def _release_connection() -> None:
    global _active_connections
    with _connection_condition:
        _active_connections -= 1
        _connection_condition.notify_all()


@contextmanager
def database_maintenance(timeout: float = 10.0):
    """Block new connections and wait until all current connections are closed."""
    global _maintenance_owner
    owner = threading.get_ident()
    deadline = time.monotonic() + timeout
    with _connection_condition:
        if _maintenance_owner is not None:
            raise DatabaseMaintenanceError("Database maintenance is already running")
        _maintenance_owner = owner
        while _active_connections:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _maintenance_owner = None
                _connection_condition.notify_all()
                raise DatabaseMaintenanceError(
                    f"Timed out waiting for {_active_connections} database connection(s)"
                )
            _connection_condition.wait(remaining)
    try:
        yield
    finally:
        with _connection_condition:
            _maintenance_owner = None
            _connection_condition.notify_all()


def get_db_path() -> str:
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = str(build_runtime_paths().database)
    return _DB_PATH


def set_db_path(path: str | Path) -> None:
    global _DB_PATH
    _DB_PATH = str(normalize_path(path))


def get_conn() -> sqlite3.Connection:
    global _active_connections
    with _connection_condition:
        if (
            _maintenance_owner is not None
            and _maintenance_owner != threading.get_ident()
        ):
            raise DatabaseMaintenanceError("Database reset is in progress")
        _active_connections += 1

    try:
        conn = sqlite3.connect(get_db_path(), timeout=30.0, factory=_TrackedConnection)
        conn._mediastudio_counted = True
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA mmap_size=268435456")
        return conn
    except Exception:
        if "conn" in locals():
            conn.close()
        else:
            _release_connection()
        raise


_INITIAL_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'idle',
        enabled INTEGER NOT NULL DEFAULT 1,
        recursive INTEGER NOT NULL DEFAULT 0,
        source_status TEXT NOT NULL DEFAULT 'available',
        last_error TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        scanned_at TEXT DEFAULT (datetime('now')),
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
        rel_path TEXT NOT NULL,
        file_name TEXT NOT NULL,
        file_size INTEGER DEFAULT 0,
        file_mtime REAL DEFAULT 0,
        media_type TEXT NOT NULL DEFAULT 'image',
        mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
        format TEXT,
        width INTEGER DEFAULT 0,
        height INTEGER DEFAULT 0,
        mode TEXT,
        duration REAL,
        frame_rate REAL,
        codec TEXT,
        error TEXT,
        metadata_json TEXT,
        ai_annotations_json TEXT,
        preview_status TEXT NOT NULL DEFAULT 'pending',
        preview_error TEXT,
        has_original_data INTEGER NOT NULL DEFAULT 0,
        content_fingerprint TEXT,
        is_favorite INTEGER NOT NULL DEFAULT 0,
        rating INTEGER,
        note TEXT NOT NULL DEFAULT '',
        indexed_at TEXT DEFAULT (datetime('now')),
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(folder_id, rel_path)
    );

    CREATE TABLE IF NOT EXISTS asset_blobs (
        image_id INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
        data BLOB NOT NULL
    );

    CREATE TABLE IF NOT EXISTS albums (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        cover_image_id INTEGER REFERENCES images(id) ON DELETE SET NULL,
        custom_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS album_images (
        album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
        image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        position INTEGER NOT NULL DEFAULT 0,
        added_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (album_id, image_id)
    );

    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS image_tags (
        image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (image_id, tag_id)
    );

    CREATE TABLE IF NOT EXISTS image_nodes (
        image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        node_type TEXT NOT NULL,
        PRIMARY KEY (image_id, node_type)
    );

    CREATE TABLE IF NOT EXISTS image_model_families (
        image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        family TEXT NOT NULL,
        PRIMARY KEY (image_id, family)
    );

    -- Server-side custom (drag-reordered) positions, one scope per view:
    -- 'folder:<id>' (Viewer folder / library source), 'media:all' (whole
    -- library), 'collection:<id>' (system collections). Albums keep their own
    -- album_images.position mechanism and never use this table.
    -- Smaller position = earlier (top). Images without a row are unpositioned
    -- ("new") and always sort before positioned ones (NULLS-first), newest
    -- first — this preserves the "new items on top" behavior for free.
    CREATE TABLE IF NOT EXISTS item_positions (
        scope TEXT NOT NULL,
        image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        position REAL NOT NULL,
        PRIMARY KEY (scope, image_id)
    );

    -- Durable background job queue (no broker: single local instance).
    -- A crashed worker never corrupts anything: rows stay 'pending' (or are
    -- reset from 'running' on startup) and are picked up again. Jobs must be
    -- idempotent — a retry after a crash replays file operations whose
    -- effects (trashed/missing files, unlinked caches) are all safe to repeat.
    CREATE TABLE IF NOT EXISTS job_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        op TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        run_after TEXT NOT NULL DEFAULT (datetime('now')),
        last_error TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );

    -- Tombstones for assets whose DB rows are already gone while their slow
    -- file operations (OS trash, cache purge) are still queued. They serve
    -- two purposes: the worker locates files without the images row, and
    -- reconciliation skips these paths instead of "resurrecting" them.
    -- image_id is deliberately NOT a foreign key: the row is deleted first.
    CREATE TABLE IF NOT EXISTS trash_tombstones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        image_id INTEGER NOT NULL,
        folder_id INTEGER NOT NULL,
        folder_path TEXT NOT NULL DEFAULT '',
        rel_path TEXT NOT NULL,
        file_name TEXT NOT NULL DEFAULT '',
        has_local_file INTEGER NOT NULL DEFAULT 1,
        trashed_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS ai_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER REFERENCES images(id) ON DELETE SET NULL,
        family TEXT NOT NULL,
        operation TEXT NOT NULL,
        scenario TEXT NOT NULL,
        modifiers_json TEXT NOT NULL DEFAULT '[]',
        checkpoint_profile TEXT,
        output_contract TEXT NOT NULL,
        execution_backend TEXT NOT NULL,
        provider_profile_id TEXT,
        model_id TEXT,
        user_input TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN (
                'queued', 'running', 'waiting_for_review',
                'completed', 'failed', 'cancelled'
            )),
        bundle_metadata_json TEXT,
        technical_error TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        started_at TEXT,
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS ai_scene_specs (
        job_id INTEGER PRIMARY KEY REFERENCES ai_jobs(id) ON DELETE CASCADE,
        schema_version TEXT NOT NULL,
        scene_spec_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS ai_prompt_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES ai_jobs(id) ON DELETE CASCADE,
        parent_draft_id INTEGER REFERENCES ai_prompt_drafts(id) ON DELETE SET NULL,
        schema_version TEXT NOT NULL,
        positive_prompt TEXT NOT NULL DEFAULT '',
        negative_prompt TEXT NOT NULL DEFAULT '',
        source_kind TEXT NOT NULL DEFAULT 'user_text',
        source_payload_json TEXT NOT NULL DEFAULT '{}',
        versions_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS ai_results (
        job_id INTEGER PRIMARY KEY REFERENCES ai_jobs(id) ON DELETE CASCADE,
        schema_version TEXT NOT NULL,
        positive_prompt TEXT NOT NULL,
        negative_prompt TEXT NOT NULL DEFAULT '',
        execution_metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS ai_prompt_translations (
        job_id INTEGER PRIMARY KEY REFERENCES ai_jobs(id) ON DELETE CASCADE,
        schema_version TEXT NOT NULL DEFAULT '1',
        source_language TEXT,
        target_language TEXT NOT NULL,
        source_positive_prompt TEXT NOT NULL,
        source_negative_prompt TEXT NOT NULL DEFAULT '',
        translated_positive_prompt TEXT NOT NULL,
        translated_negative_prompt TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS ai_prompt_adaptations (
        job_id INTEGER PRIMARY KEY REFERENCES ai_jobs(id) ON DELETE CASCADE,
        schema_version TEXT NOT NULL DEFAULT '2',
        target_family TEXT NOT NULL,
        checkpoint_profile TEXT,
        protected_triggers_json TEXT NOT NULL DEFAULT '[]',
        source_positive_prompt TEXT NOT NULL,
        source_negative_prompt TEXT NOT NULL DEFAULT '',
        adapted_positive_prompt TEXT NOT NULL,
        adapted_negative_prompt TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS ai_prompt_enhancements (
        job_id INTEGER PRIMARY KEY REFERENCES ai_jobs(id) ON DELETE CASCADE,
        schema_version TEXT NOT NULL DEFAULT '1',
        family TEXT NOT NULL,
        checkpoint_profile TEXT,
        wishes TEXT NOT NULL DEFAULT '',
        source_positive_prompt TEXT NOT NULL,
        source_negative_prompt TEXT NOT NULL DEFAULT '',
        enhanced_positive_prompt TEXT NOT NULL,
        enhanced_negative_prompt TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS model_resources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content_hash TEXT UNIQUE NOT NULL,
        file_path TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        architecture TEXT NOT NULL,
        prompt_family TEXT NOT NULL,
        display_name TEXT NOT NULL,
        version TEXT NOT NULL DEFAULT '',
        preview_url TEXT,
        metadata_source TEXT NOT NULL DEFAULT 'local',
        trigger_words_json TEXT NOT NULL DEFAULT '[]',
        default_strength REAL NOT NULL DEFAULT 1.0,
        min_strength REAL NOT NULL DEFAULT 0.0,
        max_strength REAL NOT NULL DEFAULT 2.0,
        technical_status TEXT NOT NULL DEFAULT 'supported',
        restriction_reason TEXT,
        is_available INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS model_file_hashes (
        file_path TEXT PRIMARY KEY,
        file_size INTEGER NOT NULL,
        file_mtime REAL NOT NULL,
        sha256 TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS model_recommendations (
        sha256 TEXT PRIMARY KEY,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS model_downloads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        civitai_model_id INTEGER NOT NULL DEFAULT 0,
        civitai_model_name TEXT NOT NULL DEFAULT '',
        civitai_version_id INTEGER NOT NULL,
        version_name TEXT NOT NULL DEFAULT '',
        folder TEXT NOT NULL,
        filename TEXT NOT NULL,
        file_size_bytes INTEGER NOT NULL DEFAULT 0,
        downloaded_bytes INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'queued',
        error TEXT,
        source_url TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS workflow_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        template_id TEXT NOT NULL,
        template_version TEXT NOT NULL,
        values_json TEXT NOT NULL DEFAULT '{}',
        resource_selections_json TEXT NOT NULL DEFAULT '{}',
        source_asset_id INTEGER REFERENCES images(id) ON DELETE SET NULL,
        ai_prompt_draft_id INTEGER REFERENCES ai_prompt_drafts(id) ON DELETE SET NULL,
        auto_rate INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'editing'
            CHECK (status IN ('editing', 'queued', 'completed', 'failed', 'cancelled')),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS workflow_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id INTEGER NOT NULL REFERENCES workflow_drafts(id) ON DELETE CASCADE,
        prompt_id TEXT NOT NULL UNIQUE,
        client_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
        auto_rate INTEGER NOT NULL DEFAULT 0,
        progress REAL,
        queue_position INTEGER,
        current_node TEXT,
        error_json TEXT,
        output_refs_json TEXT NOT NULL DEFAULT '[]',
        output_asset_ids_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        started_at TEXT,
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS ai_ratings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        job_id INTEGER REFERENCES ai_jobs(id) ON DELETE SET NULL,
        rank TEXT,
        rank_override TEXT,
        status TEXT NOT NULL DEFAULT 'rated',
        technical_quality REAL,
        composition REAL,
        prompt_adherence REAL,
        defects_json TEXT NOT NULL DEFAULT '[]',
        explanation TEXT NOT NULL DEFAULT '',
        execution_backend TEXT NOT NULL DEFAULT '',
        provider_profile_id TEXT,
        model_id TEXT,
        evaluation_version TEXT NOT NULL DEFAULT '1',
        schema_version TEXT NOT NULL DEFAULT '1',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(image_id)
    );

    -- Studio VisualSession layer (EDITOR-STUDIO-RUNTIME-PLAN §4, §6).
    -- Scene state lives here; WorkflowDraft/WorkflowRun stay the execution
    -- layer underneath. original_asset_id is the stable anchor, while
    -- current_asset_id tracks the latest working state.
    CREATE TABLE IF NOT EXISTS visual_sessions (
        id TEXT PRIMARY KEY,
        original_asset_id INTEGER REFERENCES images(id) ON DELETE SET NULL,
        current_asset_id INTEGER REFERENCES images(id) ON DELETE SET NULL,
        instruction TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS visual_references (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES visual_sessions(id) ON DELETE CASCADE,
        asset_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(session_id, asset_id, role)
    );

    -- Append-only history: no UPDATE/DELETE API is exposed for this table.
    CREATE TABLE IF NOT EXISTS visual_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES visual_sessions(id) ON DELETE CASCADE,
        operation TEXT NOT NULL,
        instruction TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT 'runtime',
        input_asset_ids_json TEXT NOT NULL DEFAULT '[]',
        output_asset_ids_json TEXT NOT NULL DEFAULT '[]',
        workflow_run_id INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_images_folder ON images(folder_id);
    CREATE INDEX IF NOT EXISTS idx_images_folder_mtime ON images(folder_id, file_mtime);
    CREATE INDEX IF NOT EXISTS idx_images_mtime_id ON images(file_mtime DESC, id DESC);
    CREATE INDEX IF NOT EXISTS idx_album_images_image ON album_images(image_id);
    CREATE INDEX IF NOT EXISTS idx_album_images_album_image ON album_images(album_id, image_id);
    CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags(tag_id);
    CREATE INDEX IF NOT EXISTS idx_ai_jobs_asset ON ai_jobs(asset_id);
    CREATE INDEX IF NOT EXISTS idx_ai_jobs_status ON ai_jobs(status);
    CREATE INDEX IF NOT EXISTS idx_ai_prompt_drafts_job ON ai_prompt_drafts(job_id, id);
    CREATE INDEX IF NOT EXISTS idx_ai_prompt_translations_language
        ON ai_prompt_translations(target_language);
    CREATE INDEX IF NOT EXISTS idx_ai_prompt_adaptations_family
        ON ai_prompt_adaptations(target_family);
    CREATE INDEX IF NOT EXISTS idx_model_resources_hash ON model_resources(content_hash);
    CREATE INDEX IF NOT EXISTS idx_model_resources_arch ON model_resources(architecture);
    CREATE INDEX IF NOT EXISTS idx_model_file_hashes_sha256 ON model_file_hashes(sha256);
    CREATE INDEX IF NOT EXISTS idx_model_downloads_status ON model_downloads(status);
    CREATE INDEX IF NOT EXISTS idx_workflow_drafts_template ON workflow_drafts(template_id, id);
    CREATE INDEX IF NOT EXISTS idx_workflow_drafts_source ON workflow_drafts(source_asset_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_create_task_id
        ON workflow_drafts(json_extract(resource_selections_json, '$.create_task_id'))
        WHERE template_id = 'create-task';
    CREATE INDEX IF NOT EXISTS idx_workflow_runs_draft ON workflow_runs(draft_id, id);
    CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status);
    CREATE INDEX IF NOT EXISTS idx_ai_ratings_image ON ai_ratings(image_id);
    CREATE INDEX IF NOT EXISTS idx_ai_ratings_rank ON ai_ratings(rank);
"""

_COLUMN_MIGRATIONS = (
    "ALTER TABLE images ADD COLUMN has_original_data INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE folders ADD COLUMN status TEXT NOT NULL DEFAULT 'idle'",
    "ALTER TABLE folders ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE folders ADD COLUMN recursive INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE folders ADD COLUMN source_status TEXT NOT NULL DEFAULT 'available'",
    "ALTER TABLE folders ADD COLUMN last_error TEXT",
    "ALTER TABLE folders ADD COLUMN revision INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE images ADD COLUMN content_fingerprint TEXT",
    "ALTER TABLE images ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE images ADD COLUMN rating INTEGER",
    "ALTER TABLE images ADD COLUMN note TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE images ADD COLUMN indexed_at TEXT",
    "ALTER TABLE images ADD COLUMN media_type TEXT NOT NULL DEFAULT 'image'",
    "ALTER TABLE images ADD COLUMN mime_type TEXT NOT NULL DEFAULT 'application/octet-stream'",
    "ALTER TABLE images ADD COLUMN duration REAL",
    "ALTER TABLE images ADD COLUMN frame_rate REAL",
    "ALTER TABLE images ADD COLUMN codec TEXT",
    "ALTER TABLE images ADD COLUMN ai_annotations_json TEXT",
    "ALTER TABLE images ADD COLUMN preview_status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE images ADD COLUMN preview_error TEXT",
    "ALTER TABLE ai_prompt_drafts ADD COLUMN parent_draft_id INTEGER REFERENCES ai_prompt_drafts(id) ON DELETE SET NULL",
    "ALTER TABLE ai_prompt_drafts ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'user_text'",
    "ALTER TABLE ai_prompt_drafts ADD COLUMN source_payload_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE ai_prompt_drafts ADD COLUMN updated_at TEXT",
    "ALTER TABLE images ADD COLUMN derived_from_asset_id INTEGER REFERENCES images(id) ON DELETE SET NULL",
    "ALTER TABLE ai_prompt_adaptations ADD COLUMN protected_triggers_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE workflow_drafts ADD COLUMN auto_rate INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE workflow_runs ADD COLUMN auto_rate INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE albums ADD COLUMN custom_order INTEGER NOT NULL DEFAULT 0",
)

_LATE_INDEXES_SQL = """
    CREATE INDEX IF NOT EXISTS idx_images_favorite ON images(is_favorite);
    CREATE INDEX IF NOT EXISTS idx_images_fingerprint ON images(content_fingerprint);
    CREATE INDEX IF NOT EXISTS idx_images_media_type ON images(media_type);
    CREATE INDEX IF NOT EXISTS idx_images_derived_from ON images(derived_from_asset_id);
    CREATE INDEX IF NOT EXISTS idx_images_mtime_id ON images(file_mtime DESC, id DESC);
    CREATE INDEX IF NOT EXISTS idx_images_media_mtime_id ON images(media_type, file_mtime DESC, id DESC);
    CREATE INDEX IF NOT EXISTS idx_images_folder_media_mtime_id ON images(folder_id, media_type, file_mtime DESC, id DESC);
    CREATE INDEX IF NOT EXISTS idx_images_rating_media_mtime ON images(COALESCE(rating, 0), media_type, file_mtime DESC, id DESC);
    CREATE INDEX IF NOT EXISTS idx_images_folder_rating_media_mtime ON images(folder_id, COALESCE(rating, 0), media_type, file_mtime DESC, id DESC);
    CREATE INDEX IF NOT EXISTS idx_images_indexed_at ON images(indexed_at DESC);
    CREATE INDEX IF NOT EXISTS idx_images_has_original_data ON images(has_original_data) WHERE has_original_data = 1;
    CREATE INDEX IF NOT EXISTS idx_images_pending_processing ON images(folder_id, id) WHERE metadata_json IS NULL AND error IS NULL;
    CREATE INDEX IF NOT EXISTS idx_album_images_album_pos ON album_images(album_id, position ASC, image_id ASC);
    CREATE INDEX IF NOT EXISTS idx_item_positions_scope ON item_positions(scope, position ASC, image_id ASC);
    CREATE INDEX IF NOT EXISTS idx_job_queue_status ON job_queue(status, run_after, id);
    CREATE INDEX IF NOT EXISTS idx_trash_tombstones_folder ON trash_tombstones(folder_id, rel_path);
    CREATE INDEX IF NOT EXISTS idx_trash_tombstones_image ON trash_tombstones(image_id);
    CREATE INDEX IF NOT EXISTS idx_ai_jobs_asset ON ai_jobs(asset_id);
    CREATE INDEX IF NOT EXISTS idx_ai_jobs_status ON ai_jobs(status);
    CREATE INDEX IF NOT EXISTS idx_visual_references_session ON visual_references(session_id, id);
    CREATE INDEX IF NOT EXISTS idx_visual_references_asset ON visual_references(asset_id);
    CREATE INDEX IF NOT EXISTS idx_visual_actions_session ON visual_actions(session_id, id);
    CREATE INDEX IF NOT EXISTS idx_visual_sessions_original ON visual_sessions(original_asset_id);
    CREATE INDEX IF NOT EXISTS idx_visual_sessions_current ON visual_sessions(current_asset_id);
    CREATE INDEX IF NOT EXISTS idx_image_nodes_node_type ON image_nodes(node_type COLLATE NOCASE, image_id);
    CREATE INDEX IF NOT EXISTS idx_image_model_families_family ON image_model_families(family, image_id);
"""


def _create_initial_tables_and_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(_INITIAL_SCHEMA_SQL)


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for migration in _COLUMN_MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _apply_data_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE images SET indexed_at = COALESCE(indexed_at, created_at, datetime('now'))"
    )
    conn.execute(
        """UPDATE ai_prompt_drafts SET
            updated_at = COALESCE(updated_at, created_at, datetime('now'))"""
    )
    conn.execute(
        """UPDATE images SET
            media_type = COALESCE(NULLIF(media_type, ''), 'image'),
            mime_type = CASE
                WHEN LOWER(file_name) LIKE '%.png' THEN 'image/png'
                WHEN LOWER(file_name) LIKE '%.jpg' THEN 'image/jpeg'
                WHEN LOWER(file_name) LIKE '%.jpeg' THEN 'image/jpeg'
                WHEN LOWER(file_name) LIKE '%.webp' THEN 'image/webp'
                WHEN LOWER(file_name) LIKE '%.bmp' THEN 'image/bmp'
                WHEN LOWER(file_name) LIKE '%.tiff' THEN 'image/tiff'
                ELSE COALESCE(NULLIF(mime_type, ''), 'application/octet-stream')
            END,
            preview_status = CASE
                WHEN preview_status = 'pending' AND (metadata_json IS NOT NULL OR error IS NOT NULL)
                THEN 'ready'
                ELSE COALESCE(NULLIF(preview_status, ''), 'pending')
            END"""
    )


def _create_late_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(_LATE_INDEXES_SQL)


def _migrate_rating_indexes(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_images_rating_media_mtime'"
    ).fetchone()
    if row and "COALESCE" not in str(row["sql"] or "").upper():
        conn.execute("DROP INDEX IF EXISTS idx_images_rating_media_mtime")


_METADATA_TRIGGERS_SQL = """
    CREATE TRIGGER IF NOT EXISTS trg_images_metadata_ai AFTER INSERT ON images
    WHEN NEW.metadata_json IS NOT NULL AND json_valid(NEW.metadata_json) = 1
    BEGIN
        INSERT OR IGNORE INTO image_nodes (image_id, node_type)
        SELECT DISTINCT NEW.id, CAST(value AS TEXT)
        FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
        WHERE key = 'class_type' AND type = 'text' AND TRIM(CAST(value AS TEXT)) != '';

        INSERT OR IGNORE INTO image_nodes (image_id, node_type)
        SELECT DISTINCT NEW.id, CAST(value AS TEXT)
        FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
        WHERE json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
          AND key = 'class_type' AND type = 'text' AND TRIM(CAST(value AS TEXT)) != '';

        INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT NEW.id, 'flux'
        WHERE (
            LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                  COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                  COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                  COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%flux%'
            OR EXISTS (
                SELECT 1 FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
                WHERE (key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
                   OR (key = 'class_type' AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
            )
            OR (
                json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
                AND EXISTS (
                    SELECT 1 FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
                    WHERE (key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
                       OR (key = 'class_type' AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
                )
            )
        );

        INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT NEW.id, 'pony'
        WHERE (
            LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                  COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                  COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                  COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%pony%'
            OR EXISTS (
                SELECT 1 FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
                WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
            )
            OR (
                json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
                AND EXISTS (
                    SELECT 1 FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
                    WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
                )
            )
        );

        INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT NEW.id, 'sdxl'
        WHERE (
            (
                LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                      COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                      COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                      COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sdxl%'
                OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd_xl%'
                OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd-xl%'
                OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable diffusion xl%'
                OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable-diffusion-xl%'
                OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                         COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%xl%'
                OR EXISTS (
                    SELECT 1 FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
                    WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                      AND (
                          LOWER(CAST(value AS TEXT)) LIKE '%sdxl%' OR
                          LOWER(CAST(value AS TEXT)) LIKE '%sd_xl%' OR
                          LOWER(CAST(value AS TEXT)) LIKE '%sd-xl%' OR
                          LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion xl%' OR
                          LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-xl%' OR
                          LOWER(CAST(value AS TEXT)) LIKE '%xl%'
                      )
                )
                OR (
                    json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
                    AND EXISTS (
                        SELECT 1 FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
                        WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                          AND (
                              LOWER(CAST(value AS TEXT)) LIKE '%sdxl%' OR
                              LOWER(CAST(value AS TEXT)) LIKE '%sd_xl%' OR
                              LOWER(CAST(value AS TEXT)) LIKE '%sd-xl%' OR
                              LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion xl%' OR
                              LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-xl%' OR
                              LOWER(CAST(value AS TEXT)) LIKE '%xl%'
                          )
                    )
                )
            )
            AND NOT (
                LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                      COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                      COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                      COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%pony%'
                OR EXISTS (
                    SELECT 1 FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
                    WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
                )
                OR (
                    json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
                    AND EXISTS (
                        SELECT 1 FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
                        WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
                    )
                )
            )
        );

        INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT NEW.id, 'sd15'
        WHERE (
            LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                  COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                  COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                  COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd15%'
            OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd1.5%'
            OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%v1-5%'
            OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%v1.5%'
            OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable diffusion 1.5%'
            OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                     COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable-diffusion-1.5%'
            OR EXISTS (
                SELECT 1 FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
                WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                  AND (
                      LOWER(CAST(value AS TEXT)) LIKE '%sd15%' OR
                      LOWER(CAST(value AS TEXT)) LIKE '%sd1.5%' OR
                      LOWER(CAST(value AS TEXT)) LIKE '%v1-5%' OR
                      LOWER(CAST(value AS TEXT)) LIKE '%v1.5%' OR
                      LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion 1.5%' OR
                      LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-1.5%'
                  )
            )
            OR (
                json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
                AND EXISTS (
                    SELECT 1 FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
                    WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                      AND (
                          LOWER(CAST(value AS TEXT)) LIKE '%sd15%' OR
                          LOWER(CAST(value AS TEXT)) LIKE '%sd1.5%' OR
                          LOWER(CAST(value AS TEXT)) LIKE '%v1-5%' OR
                          LOWER(CAST(value AS TEXT)) LIKE '%v1.5%' OR
                          LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion 1.5%' OR
                          LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-1.5%'
                      )
                )
            )
        );
    END;

    CREATE TRIGGER IF NOT EXISTS trg_images_metadata_au AFTER UPDATE OF metadata_json ON images
    BEGIN
        DELETE FROM image_nodes WHERE image_id = OLD.id;
        DELETE FROM image_model_families WHERE image_id = OLD.id;

        INSERT OR IGNORE INTO image_nodes (image_id, node_type)
        SELECT DISTINCT NEW.id, CAST(value AS TEXT)
        FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
        WHERE NEW.metadata_json IS NOT NULL AND json_valid(NEW.metadata_json) = 1
          AND key = 'class_type' AND type = 'text' AND TRIM(CAST(value AS TEXT)) != '';

        INSERT OR IGNORE INTO image_nodes (image_id, node_type)
        SELECT DISTINCT NEW.id, CAST(value AS TEXT)
        FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
        WHERE NEW.metadata_json IS NOT NULL AND json_valid(NEW.metadata_json) = 1
          AND json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
          AND key = 'class_type' AND type = 'text' AND TRIM(CAST(value AS TEXT)) != '';

        INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT NEW.id, 'flux'
        WHERE NEW.metadata_json IS NOT NULL AND json_valid(NEW.metadata_json) = 1
          AND (
              LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                    COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                    COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                    COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%flux%'
              OR EXISTS (
                  SELECT 1 FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
                  WHERE (key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
                     OR (key = 'class_type' AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
              )
              OR (
                  json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
                      WHERE (key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
                         OR (key = 'class_type' AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
                  )
              )
          );

        INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT NEW.id, 'pony'
        WHERE NEW.metadata_json IS NOT NULL AND json_valid(NEW.metadata_json) = 1
          AND (
              LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                    COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                    COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                    COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%pony%'
              OR EXISTS (
                  SELECT 1 FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
                  WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
              )
              OR (
                  json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
                      WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
                  )
              )
          );

        INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT NEW.id, 'sdxl'
        WHERE NEW.metadata_json IS NOT NULL AND json_valid(NEW.metadata_json) = 1
          AND (
              (
                  LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                        COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                        COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                        COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sdxl%'
                  OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd_xl%'
                  OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd-xl%'
                  OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable diffusion xl%'
                  OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable-diffusion-xl%'
                  OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                           COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%xl%'
                  OR EXISTS (
                      SELECT 1 FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
                      WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                        AND (
                            LOWER(CAST(value AS TEXT)) LIKE '%sdxl%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%sd_xl%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%sd-xl%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion xl%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-xl%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%xl%'
                        )
                  )
                  OR (
                      json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
                      AND EXISTS (
                          SELECT 1 FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
                          WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                            AND (
                                LOWER(CAST(value AS TEXT)) LIKE '%sdxl%' OR
                                LOWER(CAST(value AS TEXT)) LIKE '%sd_xl%' OR
                                LOWER(CAST(value AS TEXT)) LIKE '%sd-xl%' OR
                                LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion xl%' OR
                                LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-xl%' OR
                                LOWER(CAST(value AS TEXT)) LIKE '%xl%'
                            )
                      )
                  )
              )
              AND NOT (
                  LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                        COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                        COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                        COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%pony%'
                  OR EXISTS (
                      SELECT 1 FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
                      WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
                  )
                  OR (
                      json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
                      AND EXISTS (
                          SELECT 1 FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
                          WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
                      )
                  )
              )
          );

        INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT NEW.id, 'sd15'
        WHERE NEW.metadata_json IS NOT NULL AND json_valid(NEW.metadata_json) = 1
          AND (
              LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                    COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                    COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                    COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd15%'
              OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd1.5%'
              OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%v1-5%'
              OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%v1.5%'
              OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable diffusion 1.5%'
              OR LOWER(COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                       COALESCE(json_extract(NEW.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable-diffusion-1.5%'
              OR EXISTS (
                  SELECT 1 FROM json_tree(NEW.metadata_json, '$.workflow.workflow_nodes')
                  WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                    AND (
                        LOWER(CAST(value AS TEXT)) LIKE '%sd15%' OR
                        LOWER(CAST(value AS TEXT)) LIKE '%sd1.5%' OR
                        LOWER(CAST(value AS TEXT)) LIKE '%v1-5%' OR
                        LOWER(CAST(value AS TEXT)) LIKE '%v1.5%' OR
                        LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion 1.5%' OR
                        LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-1.5%'
                    )
              )
              OR (
                  json_extract(NEW.metadata_json, '$.workflow.workflow_nodes') IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_tree(NEW.metadata_json, '$.prompt_api_json')
                      WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                        AND (
                            LOWER(CAST(value AS TEXT)) LIKE '%sd15%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%sd1.5%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%v1-5%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%v1.5%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion 1.5%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-1.5%'
                        )
                  )
              )
          );
    END;

    CREATE TRIGGER IF NOT EXISTS trg_images_metadata_ad AFTER DELETE ON images
    BEGIN
        DELETE FROM image_nodes WHERE image_id = OLD.id;
        DELETE FROM image_model_families WHERE image_id = OLD.id;
    END;
"""


def _backfill_metadata_indices(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """SELECT 1 FROM images i
        WHERE i.metadata_json IS NOT NULL
          AND json_valid(i.metadata_json) = 1
          AND NOT EXISTS (SELECT 1 FROM image_nodes WHERE image_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM image_model_families WHERE image_id = i.id)
        LIMIT 1"""
    ).fetchone()
    if not row:
        return

    conn.execute(
        """INSERT OR IGNORE INTO image_nodes (image_id, node_type)
        SELECT DISTINCT i.id, CAST(value AS TEXT)
        FROM images i, json_tree(i.metadata_json, '$.workflow.workflow_nodes')
        WHERE i.metadata_json IS NOT NULL AND json_valid(i.metadata_json) = 1
          AND key = 'class_type' AND type = 'text' AND TRIM(CAST(value AS TEXT)) != ''"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO image_nodes (image_id, node_type)
        SELECT DISTINCT i.id, CAST(value AS TEXT)
        FROM images i, json_tree(i.metadata_json, '$.prompt_api_json')
        WHERE i.metadata_json IS NOT NULL AND json_valid(i.metadata_json) = 1
          AND json_extract(i.metadata_json, '$.workflow.workflow_nodes') IS NULL
          AND key = 'class_type' AND type = 'text' AND TRIM(CAST(value AS TEXT)) != ''"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT i.id, 'flux'
        FROM images i
        WHERE i.metadata_json IS NOT NULL AND json_valid(i.metadata_json) = 1
          AND (
              LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                    COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                    COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                    COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%flux%'
              OR EXISTS (
                  SELECT 1 FROM json_tree(i.metadata_json, '$.workflow.workflow_nodes')
                  WHERE (key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
                     OR (key = 'class_type' AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
              )
              OR (
                  json_extract(i.metadata_json, '$.workflow.workflow_nodes') IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_tree(i.metadata_json, '$.prompt_api_json')
                      WHERE (key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
                         OR (key = 'class_type' AND LOWER(CAST(value AS TEXT)) LIKE '%flux%')
                  )
              )
          )"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT i.id, 'pony'
        FROM images i
        WHERE i.metadata_json IS NOT NULL AND json_valid(i.metadata_json) = 1
          AND (
              LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                    COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                    COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                    COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%pony%'
              OR EXISTS (
                  SELECT 1 FROM json_tree(i.metadata_json, '$.workflow.workflow_nodes')
                  WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
              )
              OR (
                  json_extract(i.metadata_json, '$.workflow.workflow_nodes') IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_tree(i.metadata_json, '$.prompt_api_json')
                      WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
                  )
              )
          )"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT i.id, 'sdxl'
        FROM images i
        WHERE i.metadata_json IS NOT NULL AND json_valid(i.metadata_json) = 1
          AND (
              (
                  LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                        COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                        COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                        COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sdxl%'
                  OR LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd_xl%'
                  OR LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd-xl%'
                  OR LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable diffusion xl%'
                  OR LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable-diffusion-xl%'
                  OR LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                           COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%xl%'
                  OR EXISTS (
                      SELECT 1 FROM json_tree(i.metadata_json, '$.workflow.workflow_nodes')
                      WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                        AND (
                            LOWER(CAST(value AS TEXT)) LIKE '%sdxl%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%sd_xl%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%sd-xl%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion xl%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-xl%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%xl%'
                        )
                  )
                  OR (
                      json_extract(i.metadata_json, '$.workflow.workflow_nodes') IS NULL
                      AND EXISTS (
                          SELECT 1 FROM json_tree(i.metadata_json, '$.prompt_api_json')
                          WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                            AND (
                                LOWER(CAST(value AS TEXT)) LIKE '%sdxl%' OR
                                LOWER(CAST(value AS TEXT)) LIKE '%sd_xl%' OR
                                LOWER(CAST(value AS TEXT)) LIKE '%sd-xl%' OR
                                LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion xl%' OR
                                LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-xl%' OR
                                LOWER(CAST(value AS TEXT)) LIKE '%xl%'
                            )
                      )
                  )
              )
              AND NOT (
                  LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                        COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                        COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                        COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%pony%'
                  OR EXISTS (
                      SELECT 1 FROM json_tree(i.metadata_json, '$.workflow.workflow_nodes')
                      WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
                  )
                  OR (
                      json_extract(i.metadata_json, '$.workflow.workflow_nodes') IS NULL
                      AND EXISTS (
                          SELECT 1 FROM json_tree(i.metadata_json, '$.prompt_api_json')
                          WHERE key IN ('ckpt_name', 'unet_name', 'model_name') AND LOWER(CAST(value AS TEXT)) LIKE '%pony%'
                      )
                  )
              )
          )"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO image_model_families (image_id, family)
        SELECT i.id, 'sd15'
        FROM images i
        WHERE i.metadata_json IS NOT NULL AND json_valid(i.metadata_json) = 1
          AND (
              LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                    COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                    COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                    COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd15%'
              OR LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%sd1.5%'
              OR LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%v1-5%'
              OR LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%v1.5%'
              OR LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable diffusion 1.5%'
              OR LOWER(COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.Model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.model'), '') || ' ' ||
                       COALESCE(json_extract(i.metadata_json, '$.prompt_parameters.generation_settings.Model'), '')) LIKE '%stable-diffusion-1.5%'
              OR EXISTS (
                  SELECT 1 FROM json_tree(i.metadata_json, '$.workflow.workflow_nodes')
                  WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                    AND (
                        LOWER(CAST(value AS TEXT)) LIKE '%sd15%' OR
                        LOWER(CAST(value AS TEXT)) LIKE '%sd1.5%' OR
                        LOWER(CAST(value AS TEXT)) LIKE '%v1-5%' OR
                        LOWER(CAST(value AS TEXT)) LIKE '%v1.5%' OR
                        LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion 1.5%' OR
                        LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-1.5%'
                    )
              )
              OR (
                  json_extract(i.metadata_json, '$.workflow.workflow_nodes') IS NULL
                  AND EXISTS (
                      SELECT 1 FROM json_tree(i.metadata_json, '$.prompt_api_json')
                      WHERE key IN ('ckpt_name', 'unet_name', 'model_name')
                        AND (
                            LOWER(CAST(value AS TEXT)) LIKE '%sd15%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%sd1.5%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%v1-5%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%v1.5%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%stable diffusion 1.5%' OR
                            LOWER(CAST(value AS TEXT)) LIKE '%stable-diffusion-1.5%'
                        )
                  )
              )
          )"""
    )


def _migrate_metadata_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS image_nodes (
            image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
            node_type TEXT NOT NULL,
            PRIMARY KEY (image_id, node_type)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS image_model_families (
            image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
            family TEXT NOT NULL,
            PRIMARY KEY (image_id, family)
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_image_nodes_node_type
        ON image_nodes(node_type COLLATE NOCASE, image_id)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_image_model_families_family
        ON image_model_families(family, image_id)"""
    )
    conn.executescript(_METADATA_TRIGGERS_SQL)
    _backfill_metadata_indices(conn)


def _migrate_asset_blobs_and_rudiments(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS asset_blobs (
            image_id INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
            data BLOB NOT NULL
        )"""
    )
    col_rows = conn.execute("PRAGMA table_info(images)").fetchall()
    columns = {r["name"] for r in col_rows}
    if not columns:
        return

    if "has_original_data" not in columns:
        conn.execute("ALTER TABLE images ADD COLUMN has_original_data INTEGER NOT NULL DEFAULT 0")

    if "original_data" in columns:
        conn.execute(
            """INSERT OR IGNORE INTO asset_blobs (image_id, data)
               SELECT id, original_data FROM images WHERE original_data IS NOT NULL"""
        )
        conn.execute(
            """UPDATE images SET has_original_data = 1
               WHERE id IN (SELECT image_id FROM asset_blobs)"""
        )
        try:
            conn.execute("ALTER TABLE images DROP COLUMN original_data")
        except sqlite3.OperationalError:
            pass

    if "thumbnail_b64" in columns:
        try:
            conn.execute("ALTER TABLE images DROP COLUMN thumbnail_b64")
        except sqlite3.OperationalError:
            pass

    conn.execute(
        """UPDATE images SET has_original_data = 1
           WHERE id IN (SELECT image_id FROM asset_blobs) AND has_original_data = 0"""
    )


def init_db() -> None:
    Path(get_db_path()).parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        _create_initial_tables_and_indexes(conn)
        _migrate_ai_job_review_status(conn)
        _migrate_ai_rating_nullable_rank(conn)
        _normalize_ai_rating_statuses(conn)
        _apply_column_migrations(conn)
        _migrate_asset_blobs_and_rudiments(conn)
        _apply_data_migrations(conn)
        _migrate_rating_indexes(conn)
        _create_late_indexes(conn)
        _migrate_metadata_indexes(conn)
        from .library_repair import initialize
        initialize(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_ai_job_review_status(conn: sqlite3.Connection) -> None:
    """Extend the original SQLite CHECK constraint without losing job artifacts."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_jobs'"
    ).fetchone()
    if row is None or "waiting_for_review" in str(row["sql"]):
        return

    columns = (
        "id, asset_id, family, operation, scenario, modifiers_json, "
        "checkpoint_profile, output_contract, execution_backend, "
        "provider_profile_id, model_id, user_input, status, "
        "bundle_metadata_json, technical_error, created_at, updated_at, "
        "started_at, completed_at"
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(f"""
            BEGIN IMMEDIATE;
            CREATE TABLE ai_jobs__review_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER REFERENCES images(id) ON DELETE SET NULL,
                family TEXT NOT NULL,
                operation TEXT NOT NULL,
                scenario TEXT NOT NULL,
                modifiers_json TEXT NOT NULL DEFAULT '[]',
                checkpoint_profile TEXT,
                output_contract TEXT NOT NULL,
                execution_backend TEXT NOT NULL,
                provider_profile_id TEXT,
                model_id TEXT,
                user_input TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN (
                        'queued', 'running', 'waiting_for_review',
                        'completed', 'failed', 'cancelled'
                    )),
                bundle_metadata_json TEXT,
                technical_error TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                started_at TEXT,
                completed_at TEXT
            );
            INSERT INTO ai_jobs__review_migration ({columns})
                SELECT {columns} FROM ai_jobs;
            DROP TABLE ai_jobs;
            ALTER TABLE ai_jobs__review_migration RENAME TO ai_jobs;
            COMMIT;
        """)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            "AI job status migration produced foreign key violations."
        )


def _migrate_ai_rating_nullable_rank(conn: sqlite3.Connection) -> None:
    """Allow technical rating states to exist without a fabricated artistic rank."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_ratings'"
    ).fetchone()
    if row is None or "rank TEXT NOT NULL" not in str(row["sql"]):
        return

    columns = (
        "id, image_id, job_id, rank, rank_override, status, technical_quality, "
        "composition, prompt_adherence, defects_json, explanation, "
        "execution_backend, provider_profile_id, model_id, evaluation_version, "
        "schema_version, created_at, updated_at"
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(f"""
            BEGIN IMMEDIATE;
            CREATE TABLE ai_ratings__nullable_rank_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                job_id INTEGER REFERENCES ai_jobs(id) ON DELETE SET NULL,
                rank TEXT,
                rank_override TEXT,
                status TEXT NOT NULL DEFAULT 'rated',
                technical_quality REAL,
                composition REAL,
                prompt_adherence REAL,
                defects_json TEXT NOT NULL DEFAULT '[]',
                explanation TEXT NOT NULL DEFAULT '',
                execution_backend TEXT NOT NULL DEFAULT '',
                provider_profile_id TEXT,
                model_id TEXT,
                evaluation_version TEXT NOT NULL DEFAULT '1',
                schema_version TEXT NOT NULL DEFAULT '1',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(image_id)
            );
            INSERT INTO ai_ratings__nullable_rank_migration ({columns})
                SELECT {columns} FROM ai_ratings;
            DROP TABLE ai_ratings;
            ALTER TABLE ai_ratings__nullable_rank_migration RENAME TO ai_ratings;
            CREATE INDEX idx_ai_ratings_image ON ai_ratings(image_id);
            CREATE INDEX idx_ai_ratings_rank ON ai_ratings(rank);
            COMMIT;
        """)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            "AI rating rank migration produced foreign key violations."
        )


def _normalize_ai_rating_statuses(conn: sqlite3.Connection) -> None:
    """Remove legacy low ranks that were fabricated for technical states."""
    conn.execute(
        """UPDATE ai_ratings
        SET rank=NULL,
            technical_quality=NULL,
            composition=NULL,
            prompt_adherence=NULL,
            updated_at=datetime('now')
        WHERE status <> 'rated'
          AND (rank IS NOT NULL OR technical_quality IS NOT NULL
               OR composition IS NOT NULL OR prompt_adherence IS NOT NULL)"""
    )
    conn.commit()


def upsert_folder(path: str) -> int:
    conn = get_conn()
    try:
        name = Path(path).name or path
        cur = conn.execute(
            "INSERT INTO folders (path, name) VALUES (?, ?) ON CONFLICT(path) DO UPDATE SET scanned_at=datetime('now') RETURNING id",
            (path, name),
        )
        row = cur.fetchone()
        folder_id = row["id"]
        conn.commit()
        return folder_id
    finally:
        conn.close()


def upsert_source(
    path: str,
    *,
    name: str | None = None,
    enabled: bool = True,
    recursive: bool = False,
    source_status: str | None = None,
) -> int:
    """Create or update a physical source without discarding its indexed assets."""
    conn = get_conn()
    try:
        source_name = (name or "").strip() or Path(path).name or path
        state = source_status or ("available" if enabled else "disabled")
        cur = conn.execute(
            """INSERT INTO folders
                (path, name, enabled, recursive, source_status)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name=excluded.name,
                enabled=excluded.enabled,
                recursive=excluded.recursive,
                source_status=CASE
                    WHEN excluded.enabled = 0 THEN 'disabled'
                    WHEN folders.source_status = 'disabled' THEN 'reconnecting'
                    ELSE folders.source_status
                END
            RETURNING id""",
            (path, source_name, int(enabled), int(recursive), state),
        )
        folder_id = int(cur.fetchone()["id"])
        conn.commit()
        return folder_id
    finally:
        conn.close()


def get_folder_mtimes(folder_id: int) -> dict[str, float]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT rel_path, file_mtime FROM images WHERE folder_id = ?", (folder_id,)
        ).fetchall()
        return {r["rel_path"]: r["file_mtime"] for r in rows}
    finally:
        conn.close()


def get_folder_file_stats(folder_id: int) -> dict[str, tuple[int, float]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT rel_path, file_size, file_mtime FROM images WHERE folder_id = ?",
            (folder_id,),
        ).fetchall()
        return {
            str(row["rel_path"]): (int(row["file_size"]), float(row["file_mtime"]))
            for row in rows
        }
    finally:
        conn.close()


def get_folder_file_records(folder_id: int) -> dict[str, dict[str, Any]]:
    """Return the identity fields needed to reconcile a physical source."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, rel_path, file_size, file_mtime, content_fingerprint,
                media_type, mime_type
            FROM images WHERE folder_id = ?""",
            (folder_id,),
        ).fetchall()
        return {str(row["rel_path"]): dict(row) for row in rows}
    finally:
        conn.close()


def update_image_fingerprints(fingerprints: list[tuple[int, str]]) -> None:
    if not fingerprints:
        return
    conn = get_conn()
    try:
        conn.executemany(
            "UPDATE images SET content_fingerprint = ? WHERE id = ?",
            [(fingerprint, image_id) for image_id, fingerprint in fingerprints],
        )
        conn.commit()
    finally:
        conn.close()


def batch_rename_assets(
    renames: list[tuple[str, str, int, float, str, str, str, int]],
) -> None:
    """Move indexed identities to new relative paths in a single batch using executemany."""
    if not renames:
        return
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        conn.executemany(
            """UPDATE images SET rel_path = ?, file_name = ?, file_size = ?,
                file_mtime = ?, content_fingerprint = ?, media_type = ?, mime_type = ?
            WHERE id = ?""",
            renames,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rename_asset_record(
    asset_id: int,
    *,
    rel_path: str,
    file_name: str,
    file_size: int,
    file_mtime: float,
    content_fingerprint: str,
    media_type: str,
    mime_type: str,
) -> None:
    """Move an indexed identity to a new relative path without losing virtual links."""
    batch_rename_assets(
        [
            (
                rel_path,
                file_name,
                file_size,
                file_mtime,
                content_fingerprint,
                media_type,
                mime_type,
                asset_id,
            )
        ]
    )


def rename_image_record(image_id: int, **fields: Any) -> None:
    """Compatibility wrapper for the former image-only reconciliation API."""
    rename_asset_record(image_id, **fields)


def insert_assets(folder_id: int, assets: list[AssetInsertRow]) -> None:
    if not assets:
        return
    conn = get_conn()
    try:
        params = [
            (
                folder_id,
                asset.rel_path,
                asset.file_name,
                asset.file_size,
                asset.file_mtime,
                asset.media_type,
                asset.mime_type,
                asset.format,
                asset.width,
                asset.height,
                asset.mode,
                asset.duration,
                asset.frame_rate,
                asset.codec,
                asset.error,
                asset.metadata_json,
                asset.content_fingerprint,
                asset.preview_status,
                asset.preview_error,
            )
            for asset in assets
        ]
        conn.execute("BEGIN")
        conn.executemany(
            """INSERT INTO images (folder_id, rel_path, file_name, file_size, file_mtime,
                media_type, mime_type, format, width, height, mode, duration,
                frame_rate, codec, error, metadata_json,
                content_fingerprint, preview_status, preview_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(folder_id, rel_path) DO UPDATE SET
                file_name=excluded.file_name,
                file_size=excluded.file_size, file_mtime=excluded.file_mtime,
                media_type=excluded.media_type, mime_type=excluded.mime_type,
                format=excluded.format, width=excluded.width, height=excluded.height,
                mode=excluded.mode, duration=excluded.duration,
                frame_rate=excluded.frame_rate, codec=excluded.codec,
                error=excluded.error,
                metadata_json=excluded.metadata_json,
                ai_annotations_json=NULL,
                content_fingerprint=excluded.content_fingerprint,
                preview_status=excluded.preview_status,
                preview_error=excluded.preview_error,
                created_at=datetime('now')""",
            params,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_images(folder_id: int, images: list[ImageInsertRow]) -> None:
    """Compatibility wrapper for the former image-only index API."""
    insert_assets(folder_id, list(images))


def get_folders() -> list[FolderInfo]:
    conn = get_conn()
    try:
        # Keep image_count for the image viewer while exposing unified asset counts.
        rows = conn.execute(
            """SELECT f.id, f.path, f.name, f.scanned_at, f.created_at, f.status,
               f.enabled, f.recursive, f.source_status, f.last_error, f.revision,
               COUNT(CASE WHEN i.media_type = 'image' THEN 1 END) AS image_count,
               COUNT(i.id) AS asset_count,
               COUNT(CASE WHEN i.media_type = 'video' THEN 1 END) AS video_count,
               COUNT(CASE WHEN i.media_type = 'image' AND (i.metadata_json IS NOT NULL OR i.error IS NOT NULL) THEN 1 END) AS processed_count,
               COUNT(CASE WHEN i.id IS NOT NULL AND (i.metadata_json IS NOT NULL OR i.error IS NOT NULL) THEN 1 END) AS processed_asset_count
            FROM folders f LEFT JOIN images i ON i.folder_id = f.id
            GROUP BY f.id ORDER BY f.scanned_at DESC"""
        ).fetchall()
        folders_list = [FolderInfo.model_validate(dict(r)) for r in rows]
        return folders_list
    finally:
        conn.close()


def get_indexed_folder_paths() -> list[str]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT path FROM folders ORDER BY id").fetchall()
        return [str(row["path"]) for row in rows]
    finally:
        conn.close()


def get_source_records() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, path, name, status, enabled, recursive, source_status,
                last_error, revision, scanned_at
            FROM folders
            WHERE path NOT LIKE '__uploads%'
            ORDER BY id"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_folder_record(folder_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT id, path, name, status, enabled, recursive, source_status,
                last_error, revision, scanned_at
            FROM folders WHERE id = ?""",
            (folder_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_folder_path(folder_id: int) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT path FROM folders WHERE id = ?",
            (folder_id,),
        ).fetchone()
        return str(row["path"]) if row else None
    finally:
        conn.close()


def update_folder_status(folder_id: int, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE folders SET status = ? WHERE id = ?",
            (status, folder_id),
        )
        conn.commit()
    finally:
        conn.close()


@serialized_asset_work
def update_source_settings(
    folder_id: int,
    *,
    name: str | None = None,
    enabled: bool | None = None,
    recursive: bool | None = None,
) -> None:
    assignments: list[str] = []
    values: list[Any] = []
    if name is not None:
        assignments.append("name = ?")
        values.append(name)
    if enabled is not None:
        assignments.append("enabled = ?")
        values.append(int(enabled))
        assignments.append("source_status = ?")
        values.append("reconnecting" if enabled else "disabled")
        assignments.append("status = ?")
        values.append("processing" if enabled else "paused")
    if recursive is not None:
        assignments.append("recursive = ?")
        values.append(int(recursive))
    if not assignments:
        return

    conn = get_conn()
    try:
        values.append(folder_id)
        conn.execute(
            f"UPDATE folders SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def update_source_state(
    folder_id: int,
    source_status: str,
    error: str | None = None,
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE folders SET source_status = ?, last_error = ? WHERE id = ?",
            (source_status, error, folder_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_folder_scanned(folder_id: int, *, changed: bool) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE folders
            SET scanned_at = datetime('now'),
                revision = revision + ?
            WHERE id = ?""",
            (int(changed), folder_id),
        )
        conn.commit()
    finally:
        conn.close()


def folder_has_unprocessed_images(folder_id: int) -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT 1 FROM images
            WHERE folder_id = ? AND metadata_json IS NULL AND error IS NULL
            LIMIT 1""",
            (folder_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def consolidate_legacy_source(path: str) -> None:
    """Merge the former '<source> (no metadata)' pseudo-folder into its source."""
    derived_path = f"{path} (no metadata)"
    conn = get_conn()
    try:
        source = conn.execute(
            "SELECT id FROM folders WHERE path = ?",
            (path,),
        ).fetchone()
        derived = conn.execute(
            "SELECT id FROM folders WHERE path = ?",
            (derived_path,),
        ).fetchone()
        if not source or not derived or source["id"] == derived["id"]:
            return

        source_id = int(source["id"])
        derived_id = int(derived["id"])
        conn.execute("BEGIN")
        conn.execute(
            """DELETE FROM images
            WHERE folder_id = ? AND rel_path IN (
                SELECT rel_path FROM images WHERE folder_id = ?
            )""",
            (derived_id, source_id),
        )
        conn.execute(
            "UPDATE images SET folder_id = ? WHERE folder_id = ?",
            (source_id, derived_id),
        )
        conn.execute("DELETE FROM folders WHERE id = ?", (derived_id,))
        # The derived folder view is gone: drop its whole custom-order scope.
        # (Deleted duplicate images clean up via ON DELETE CASCADE; moved
        # images re-enter the source folder view as "new".)
        conn.execute(
            "DELETE FROM item_positions WHERE scope = ?",
            (f"folder:{derived_id}",),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def split_folder_by_metadata(folder_id: int) -> None:
    """After worker finishes a folder, split images into metadata / no-metadata sibling folders.

    Rules:
    - If the folder contains both metadata and no-metadata images, create a sibling
      '<name> (no metadata)' folder and move the no-metadata images there.
    - If the folder contains ONLY no-metadata images, rename it to '<name> (no metadata)'.
    - If the folder contains ONLY metadata images, do nothing.
    - Skip uploads folders.
    """
    conn = get_conn()
    try:
        folder_row = conn.execute(
            "SELECT id, path, name FROM folders WHERE id = ?", (folder_id,)
        ).fetchone()
        if not folder_row:
            return

        folder_path = folder_row["path"]
        folder_name = folder_row["name"]

        # Skip uploads folders and already-split '(no metadata)' folders
        if folder_path in ("__uploads__", "__uploads_no_metadata__"):
            return
        if folder_name.endswith("(no metadata)"):
            return

        no_meta_cond = (
            "(error IS NOT NULL OR "
            "(metadata_json IS NOT NULL "
            "AND json_valid(metadata_json) = 1 "
            "AND json_extract(metadata_json, '$.prompt_parameters') IS NULL "
            "AND json_extract(metadata_json, '$.workflow') IS NULL))"
        )
        has_meta_cond = (
            "(error IS NULL AND "
            "(metadata_json IS NULL "
            "OR (json_valid(metadata_json) = 1 AND ("
            "json_extract(metadata_json, '$.prompt_parameters') IS NOT NULL "
            "OR json_extract(metadata_json, '$.workflow') IS NOT NULL))))"
        )

        count_no_meta = conn.execute(
            f"SELECT COUNT(*) AS c FROM images WHERE folder_id = ? AND {no_meta_cond}",
            (folder_id,),
        ).fetchone()["c"]

        if count_no_meta == 0:
            # All images have metadata — nothing to do
            return

        count_has_meta = conn.execute(
            f"SELECT COUNT(*) AS c FROM images WHERE folder_id = ? AND {has_meta_cond}",
            (folder_id,),
        ).fetchone()["c"]

        if count_has_meta == 0:
            # ALL images lack metadata — rename the folder itself
            new_name = f"{folder_name} (no metadata)"
            new_path = f"{folder_path} (no metadata)"
            conn.execute(
                "UPDATE folders SET name = ?, path = ? WHERE id = ?",
                (new_name, new_path, folder_id),
            )
            conn.commit()
            return

        # Mixed: create sibling folder and move no-metadata images there
        no_meta_path = f"{folder_path} (no metadata)"
        no_meta_name = f"{folder_name} (no metadata)"

        # Upsert the sibling folder
        cur = conn.execute(
            "INSERT INTO folders (path, name, status, scanned_at) "
            "VALUES (?, ?, 'completed', datetime('now')) "
            "ON CONFLICT(path) DO UPDATE SET scanned_at=datetime('now'), status='completed' "
            "RETURNING id",
            (no_meta_path, no_meta_name),
        )
        no_meta_folder_id = cur.fetchone()["id"]

        # Move no-metadata images to the sibling folder
        moved_rows = conn.execute(
            f"SELECT id FROM images WHERE folder_id = ? AND {no_meta_cond}",
            (folder_id,),
        ).fetchall()
        moved_ids = [int(row["id"]) for row in moved_rows]
        conn.execute(
            f"UPDATE images SET folder_id = ? WHERE folder_id = ? AND {no_meta_cond}",
            (no_meta_folder_id, folder_id),
        )
        if moved_ids:
            # Folder-scoped custom positions belong to the old folder view;
            # drop them so the moved images re-enter as "new" (top).
            # 'media:all' / collection scopes are membership-based and stay.
            placeholders = ",".join("?" for _ in moved_ids)
            conn.execute(
                "DELETE FROM item_positions "
                f"WHERE scope LIKE 'folder:%' AND image_id IN ({placeholders})",
                moved_ids,
            )
        conn.commit()
    finally:
        conn.close()


def get_image_ids_by_rel_paths(folder_id: int, rel_paths: list[str]) -> list[int]:
    if not rel_paths:
        return []
    conn = get_conn()
    try:
        ids = []
        for i in range(0, len(rel_paths), 500):
            chunk = rel_paths[i : i + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT id FROM images WHERE folder_id = ? AND rel_path IN ({placeholders})",
                [folder_id] + chunk,
            ).fetchall()
            ids.extend(r["id"] for r in rows)
        return ids
    finally:
        conn.close()


def get_folder_asset_ids(folder_id: int) -> list[int]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM images WHERE folder_id = ?",
            (folder_id,),
        ).fetchall()
        return [int(row["id"]) for row in rows]
    finally:
        conn.close()


def get_folder_image_ids(folder_id: int) -> list[int]:
    """Compatibility wrapper returning all indexed asset IDs for a source."""
    return get_folder_asset_ids(folder_id)


def delete_images_by_ids(image_ids: list[int]) -> None:
    if not image_ids:
        return
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        for i in range(0, len(image_ids), 500):
            chunk = image_ids[i : i + 500]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM images WHERE id IN ({placeholders})",
                chunk,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _update_image_metadata(
    conn: sqlite3.Connection,
    image_id: int,
    metadata: ImageMetadata,
) -> None:
    conn.execute(
        """UPDATE images SET
            format = ?, width = ?, height = ?, mode = ?, error = ?, metadata_json = ?,
            created_at = datetime('now')
        WHERE id = ?""",
        (
            metadata.format,
            metadata.size[0] if metadata.size else 0,
            metadata.size[1] if metadata.size else 0,
            metadata.mode,
            metadata.error,
            metadata.model_dump_json(),
            image_id,
        ),
    )


def update_video_metadata(
    asset_id: int,
    *,
    format: str | None,
    width: int,
    height: int,
    mode: str | None,
    duration: float | None,
    frame_rate: float | None,
    codec: str | None,
    metadata: dict[str, Any],
) -> None:
    """Persist technical metadata that came from the original video."""
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE images SET format = ?, width = ?, height = ?, mode = ?,
                duration = ?, frame_rate = ?, codec = ?, metadata_json = ?,
                created_at = datetime('now')
            WHERE id = ? AND media_type = 'video'""",
            (
                format,
                width,
                height,
                mode,
                duration,
                frame_rate,
                codec,
                json.dumps(metadata, ensure_ascii=False),
                asset_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_asset_preview_status(
    asset_id: int,
    status: str,
    error: str | None = None,
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE images SET preview_status = ?, preview_error = ? WHERE id = ?",
            (status, error, asset_id),
        )
        conn.commit()
    finally:
        conn.close()


def _fallback_commit_worker_items(
    conn: sqlite3.Connection,
    *,
    image_updates: list[tuple] | None = None,
    video_updates: list[tuple] | None = None,
    error_updates: list[tuple[str, int]] | None = None,
) -> None:
    for u in (image_updates or []):
        try:
            conn.execute("BEGIN")
            conn.execute(
                """UPDATE images SET format = ?, width = ?, height = ?,
                    mode = ?, error = ?, metadata_json = ?,
                    preview_status = ?, preview_error = ?,
                    created_at = datetime('now')
                WHERE id = ?""",
                u,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            try:
                conn.execute(
                    "UPDATE images SET error = 'Batch commit failed', preview_status = 'error' WHERE id = ?",
                    (u[-1],),
                )
                conn.commit()
            except Exception:
                pass
    for v in (video_updates or []):
        try:
            conn.execute("BEGIN")
            conn.execute(
                """UPDATE images SET format = ?, width = ?, height = ?, mode = ?,
                    duration = ?, frame_rate = ?, codec = ?, error = NULL,
                    metadata_json = ?, preview_status = ?, preview_error = ?,
                    created_at = datetime('now')
                WHERE id = ?""",
                v,
            )
            conn.commit()
        except Exception:
            conn.rollback()
    for e in (error_updates or []):
        try:
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE images SET error = ?, preview_status = 'error' WHERE id = ?",
                e,
            )
            conn.commit()
        except Exception:
            conn.rollback()


def commit_worker_batch(
    *,
    image_updates: list[tuple] | None = None,
    video_updates: list[tuple] | None = None,
    error_updates: list[tuple[str, int]] | None = None,
) -> None:
    """Batch-commit processed assets and errors from the background worker in a single transaction."""
    if not image_updates and not video_updates and not error_updates:
        return
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        if image_updates:
            conn.executemany(
                """UPDATE images SET format = ?, width = ?, height = ?,
                    mode = ?, error = ?, metadata_json = ?,
                    preview_status = ?, preview_error = ?,
                    created_at = datetime('now')
                WHERE id = ?""",
                image_updates,
            )
        if video_updates:
            conn.executemany(
                """UPDATE images SET format = ?, width = ?, height = ?, mode = ?,
                    duration = ?, frame_rate = ?, codec = ?, error = NULL,
                    metadata_json = ?, preview_status = ?, preview_error = ?,
                    created_at = datetime('now')
                WHERE id = ?""",
                video_updates,
            )
        if error_updates:
            conn.executemany(
                "UPDATE images SET error = ?, preview_status = 'error' WHERE id = ?",
                error_updates,
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
    except Exception:
        conn.rollback()
        _fallback_commit_worker_items(
            conn,
            image_updates=image_updates,
            video_updates=video_updates,
            error_updates=error_updates,
        )
    finally:
        conn.close()


@serialized_asset_work
def ensure_image_processed(image_id: int, img_path: str) -> None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT metadata_json, error FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        if row and row["metadata_json"] is None and row["error"] is None:
            # Process it
            from .extractor import extract_metadata
            abs_path = Path(img_path)
            if abs_path.is_file():
                try:
                    meta = extract_metadata(abs_path)
                    conn.execute(
                        """UPDATE images SET
                            format = ?, width = ?, height = ?, mode = ?, error = ?, metadata_json = ?,
                            preview_status = 'ready', preview_error = NULL,
                            created_at = datetime('now')
                        WHERE id = ?""",
                        (
                            meta.format,
                            meta.size[0] if meta.size else 0,
                            meta.size[1] if meta.size else 0,
                            meta.mode,
                            meta.error,
                            meta.model_dump_json(),
                            image_id,
                        ),
                    )
                    conn.commit()
                except Exception as e:
                    import traceback
                    err_str = str(e) + "\n" + traceback.format_exc()
                    conn.execute(
                        "UPDATE images SET error = ? WHERE id = ?",
                        (err_str, image_id),
                    )
                    conn.commit()
            else:
                err_str = f"File not found: {abs_path}"
                conn.execute(
                    "UPDATE images SET error = ? WHERE id = ?",
                    (err_str, image_id),
                )
                conn.commit()
    finally:
        conn.close()


@serialized_asset_work
def ensure_upload_image_processed(image_id: int, original_data: bytes) -> None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT width, height, metadata_json, error, file_name FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        if row and (row["width"] == 0 or row["height"] == 0 or row["metadata_json"] is None) and row["error"] is None:
            from .extractor import extract_metadata_from_bytes
            try:
                meta = extract_metadata_from_bytes(original_data, row["file_name"])
                conn.execute(
                    """UPDATE images SET
                        format = ?, width = ?, height = ?, mode = ?, error = ?, metadata_json = ?,
                        preview_status = 'ready', preview_error = NULL,
                        created_at = datetime('now')
                    WHERE id = ?""",
                    (
                        meta.format,
                        meta.size[0] if meta.size else 0,
                        meta.size[1] if meta.size else 0,
                        meta.mode,
                        meta.error,
                        meta.model_dump_json(),
                        image_id,
                    ),
                )
                conn.commit()
            except Exception as e:
                import traceback
                err_str = str(e) + "\n" + traceback.format_exc()
                conn.execute(
                    "UPDATE images SET error = ? WHERE id = ?",
                    (err_str, image_id),
                )
                conn.commit()
    finally:
        conn.close()


def get_images_page(
    folder_id: int | None,
    page: int = 1,
    per_page: int = 50,
    sort_by: str = "date",
    sort_dir: str = "desc",
    album_id: int | None = None,
    rating: int | None = None,
    media_types: tuple[str, ...] = ("image",),
    cursor_mtime: float | None = None,
    cursor_id: int | None = None,
) -> ImagesResponse:
    if rating is not None and rating not in range(6):
        raise ValueError("rating must be between 0 and 5")
    normalized_media_types = tuple(dict.fromkeys(media_types))
    if not normalized_media_types or any(
        media_type not in ("image", "video")
        for media_type in normalized_media_types
    ):
        raise ValueError("media_types must contain image and/or video")

    conn = get_conn()
    try:
        rating_clause = "" if rating is None else " AND COALESCE(i.rating, 0) = ?"
        rating_params: tuple[int, ...] = () if rating is None else (rating,)
        media_placeholders = ", ".join("?" for _ in normalized_media_types)
        media_clause = f" AND i.media_type IN ({media_placeholders})"

        if album_id is not None:
            total_row = conn.execute(
                f"""SELECT COUNT(*) AS c FROM images i
                JOIN folders f ON f.id = i.folder_id
                JOIN album_images ai ON ai.image_id = i.id
                WHERE ai.album_id = ? AND f.enabled = 1
                  {media_clause}{rating_clause}""",
                (album_id, *normalized_media_types, *rating_params),
            ).fetchone()
        elif folder_id is not None:
            total_row = conn.execute(
                f"""SELECT COUNT(*) AS c FROM images i
                JOIN folders f ON f.id = i.folder_id
                WHERE i.folder_id = ? AND f.enabled = 1
                  {media_clause}{rating_clause}""",
                (folder_id, *normalized_media_types, *rating_params),
            ).fetchone()
        else:
            total_row = conn.execute(
                f"""SELECT COUNT(*) AS c FROM images i
                JOIN folders f ON f.id = i.folder_id
                WHERE f.enabled = 1{media_clause}{rating_clause}""",
                (*normalized_media_types, *rating_params),
            ).fetchone()

        total = total_row["c"] if total_row else 0

        # Map sorting key to column names safely.
        # "custom" (server-side drag order from item_positions) is resolved
        # after the album check below; sort_dir is ignored for it — a custom
        # order has exactly one direction.
        sort_by_map = {
            "name": "i.file_name COLLATE NOCASE",
            "date": "i.file_mtime",
            "size": "i.file_size",
            "type": "i.mime_type",
        }
        if sort_by != "custom" and sort_by not in sort_by_map:
            raise ValueError(f"Invalid sort_by parameter: {sort_by}")
        if sort_dir.lower() not in {"asc", "desc"}:
            raise ValueError(f"Invalid sort_dir parameter: {sort_dir}")

        sort_column = sort_by_map.get(sort_by)
        direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

        # Enforce strict whitelist of columns and directions to prevent SQL Injection
        allowed_sort_columns = {
            "i.file_name COLLATE NOCASE",
            "i.file_mtime",
            "i.file_size",
            "i.mime_type",
        }
        allowed_directions = {"ASC", "DESC"}

        if sort_column is not None and sort_column not in allowed_sort_columns:
            raise ValueError(f"Invalid sort column: {sort_column}")
        if direction not in allowed_directions:
            raise ValueError(f"Invalid sort direction: {direction}")

        # Keyset / Cursor pagination
        cursor_clause = ""
        cursor_params: tuple[Any, ...] = ()
        if cursor_mtime is not None and cursor_id is not None and sort_by == "date":
            if sort_dir.lower() == "desc":
                cursor_clause = " AND (i.file_mtime < ? OR (i.file_mtime = ? AND i.id < ?))"
            else:
                cursor_clause = " AND (i.file_mtime > ? OR (i.file_mtime = ? AND i.id > ?))"
            cursor_params = (cursor_mtime, cursor_mtime, cursor_id)
            offset = 0
        else:
            offset = (page - 1) * per_page

        # Check if album has custom ordering
        album_custom_order = False
        if album_id is not None:
            row = conn.execute(
                "SELECT custom_order FROM albums WHERE id = ?", (album_id,)
            ).fetchone()
            album_custom_order = bool(row and row["custom_order"])

        # Resolve the server-side drag-order scope. Albums keep their own
        # album_images.position mechanism; an album without the custom_order
        # flag falls back to date (previous behavior when the route remapped
        # sort_by=custom to date).
        custom_scope: str | None = None
        if sort_by == "custom" and not album_custom_order:
            if album_id is not None:
                sort_column = sort_by_map["date"]
            elif folder_id is not None:
                custom_scope = f"folder:{folder_id}"
            else:
                custom_scope = "media:all"

        # Determine sort order
        if album_custom_order:
            subquery_order = "ORDER BY ai2.position ASC, i2.id ASC"
            main_order = "ORDER BY ai.position ASC, i.id ASC"
        elif custom_scope is not None:
            # Unpositioned ("new") rows sort first, newest first — this keeps
            # the "new items on top" behavior without any client overlay.
            # (SQLite sorts NULLS first in ASC order.)
            subquery_order = (
                "ORDER BY ip2.position IS NOT NULL, ip2.position ASC, COALESCE(i2.file_mtime, 0) DESC, i2.id DESC"
            )
            main_order = (
                "ORDER BY ip.position IS NOT NULL, ip.position ASC, COALESCE(i.file_mtime, 0) DESC, i.id DESC"
            )
        elif sort_by == "name":
            subquery_order = f"ORDER BY i2.file_name COLLATE NOCASE {direction}, i2.id {direction}"
            main_order = f"ORDER BY i.file_name COLLATE NOCASE {direction}, i.id {direction}"
        elif sort_by == "type":
            # MIME type is available at indexing, before metadata extraction finishes.
            subquery_order = f"ORDER BY i2.mime_type {direction}, i2.file_name COLLATE NOCASE {direction}, i2.id {direction}"
            main_order = f"ORDER BY i.mime_type {direction}, i.file_name COLLATE NOCASE {direction}, i.id {direction}"
        else:
            sort_col_sub = sort_column.replace("i.", "i2.")
            subquery_order = f"ORDER BY {sort_col_sub} {direction}, i2.id {direction}"
            main_order = f"ORDER BY {sort_column} {direction}, i.id {direction}"

        # Deferred Join: subquery uses index scan to get IDs, main query fetches row details
        media_clause_sub = media_clause.replace("i.", "i2.")
        rating_clause_sub = rating_clause.replace("i.", "i2.")
        cursor_clause_sub = cursor_clause.replace("i.", "i2.")

        custom_join_sub = ""
        custom_join_params: tuple[Any, ...] = ()
        if custom_scope is not None:
            custom_join_sub = (
                " LEFT JOIN item_positions ip2"
                " ON ip2.scope = ? AND ip2.image_id = i2.id"
            )
            custom_join_params = (custom_scope,)

        if album_id is not None:
            subquery = f"""SELECT i2.id FROM images i2
                JOIN folders f2 ON f2.id = i2.folder_id
                JOIN album_images ai2 ON ai2.image_id = i2.id
                WHERE ai2.album_id = ? AND f2.enabled = 1
                  {media_clause_sub}{rating_clause_sub}{cursor_clause_sub}
                {subquery_order} LIMIT ? OFFSET ?"""
            query_params = (
                album_id,
                *normalized_media_types,
                *rating_params,
                *cursor_params,
                per_page,
                offset,
            )
        elif folder_id is not None:
            subquery = f"""SELECT i2.id FROM images i2
                JOIN folders f2 ON f2.id = i2.folder_id{custom_join_sub}
                WHERE i2.folder_id = ? AND f2.enabled = 1
                  {media_clause_sub}{rating_clause_sub}{cursor_clause_sub}
                {subquery_order} LIMIT ? OFFSET ?"""
            # NOTE: the custom JOIN's placeholder comes textually before the
            # WHERE placeholder, so its param goes first.
            query_params = (
                *custom_join_params,
                folder_id,
                *normalized_media_types,
                *rating_params,
                *cursor_params,
                per_page,
                offset,
            )
        else:
            subquery = f"""SELECT i2.id FROM images i2
                JOIN folders f2 ON f2.id = i2.folder_id{custom_join_sub}
                WHERE f2.enabled = 1{media_clause_sub}{rating_clause_sub}{cursor_clause_sub}
                {subquery_order} LIMIT ? OFFSET ?"""
            query_params = (
                *custom_join_params,
                *normalized_media_types,
                *rating_params,
                *cursor_params,
                per_page,
                offset,
            )

        album_join = ""
        if album_custom_order:
            album_join = " JOIN album_images ai ON ai.image_id = i.id AND ai.album_id = ?"

        custom_join_main = ""
        if custom_scope is not None:
            custom_join_main = (
                " LEFT JOIN item_positions ip ON ip.scope = ? AND ip.image_id = i.id"
            )

        main_query = f"""SELECT i.id, i.file_name, i.media_type, i.mime_type,
            i.format, i.width, i.height, i.mode,
            i.duration, i.frame_rate, i.codec, i.preview_status,
            i.preview_error, i.error,
            CASE
                WHEN i.metadata_json IS NOT NULL AND json_valid(i.metadata_json) = 1
                THEN json_extract(i.metadata_json, '$.prompt_parameters')
                ELSE NULL
            END AS prompt_parameters_json,
            i.rating,
            (i.has_original_data = 0) AS has_local_file,
            i.file_mtime
        FROM ({subquery}) page_ids
        JOIN images i ON i.id = page_ids.id{album_join}{custom_join_main}
        {main_order}"""

        if album_custom_order:
            query_params = (*query_params, album_id)
        if custom_scope is not None:
            query_params = (*query_params, custom_scope)

        rows = conn.execute(main_query, query_params).fetchall()

        images = []
        for r in rows:
            d = dict(r)
            w, h = d.get("width"), d.get("height")
            raw_params = d.get("prompt_parameters_json")
            prompt_parameters = None
            if raw_params:
                try:
                    parsed = json.loads(raw_params)
                    if isinstance(parsed, dict):
                        prompt_parameters = parsed
                except (json.JSONDecodeError, TypeError):
                    pass
            images.append(ImageListItem(
                id=d.get("id"),
                file_name=d.get("file_name", ""),
                media_type=d.get("media_type") or "image",
                mime_type=d.get("mime_type") or "application/octet-stream",
                format=d.get("format"),
                size=[w, h] if w and h else None,
                mode=d.get("mode"),
                duration=d.get("duration"),
                frame_rate=d.get("frame_rate"),
                codec=d.get("codec"),
                preview_status=d.get("preview_status"),
                preview_error=d.get("preview_error"),
                error=d.get("error"),
                has_local_file=bool(d.get("has_local_file")),
                rating=d.get("rating"),
                prompt_parameters=prompt_parameters,
            ))
        return ImagesResponse(images=images, total=total, page=page, per_page=per_page)
    finally:
        conn.close()


def get_asset_path(asset_id: int) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT f.path, i.rel_path FROM images i
            JOIN folders f ON f.id = i.folder_id
            WHERE i.id = ?""",
            (asset_id,),
        ).fetchone()
        if row:
            return str(Path(row["path"]) / row["rel_path"])
        return None
    finally:
        conn.close()


def get_image_path(image_id: int) -> str | None:
    """Compatibility wrapper for callers using the image ID terminology."""
    return get_asset_path(image_id)


def get_asset_source_info(asset_id: int) -> dict[str, Any] | None:
    """Return source metadata without loading an uploaded BLOB into memory."""
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT i.id, i.rel_path, i.file_name, i.file_size, i.file_mtime,
                i.media_type, i.mime_type, i.format, i.duration, i.preview_status,
                i.preview_error, i.has_original_data, i.content_fingerprint,
                f.path AS folder_path
            FROM images i
            LEFT JOIN folders f ON f.id = i.folder_id
            WHERE i.id = ?""",
            (asset_id,),
        ).fetchone()
        if not row:
            return None

        source = dict(row)
        source["has_original_data"] = bool(source["has_original_data"])
        source["path"] = None
        if not source["has_original_data"]:
            source["path"] = str(
                Path(source["folder_path"]) / source["rel_path"]
            )
        return source
    finally:
        conn.close()


def get_image_source_info(image_id: int) -> dict[str, Any] | None:
    """Compatibility wrapper for the former image-only source lookup."""
    return get_asset_source_info(image_id)


def iter_asset_original_data(
    asset_id: int,
    chunk_size: int = 1024 * 1024,
    *,
    offset: int = 0,
    length: int | None = None,
):
    """Stream an SQLite BLOB without materializing the full value as bytes."""
    conn = get_conn()
    try:
        with conn.blobopen(
            "asset_blobs",
            "data",
            asset_id,
            readonly=True,
        ) as blob:
            blob.seek(offset)
            remaining = len(blob) - offset if length is None else length
            while remaining > 0:
                chunk = blob.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    finally:
        conn.close()


def iter_image_original_data(
    image_id: int,
    chunk_size: int = 1024 * 1024,
    *,
    offset: int = 0,
    length: int | None = None,
):
    """Compatibility wrapper for uploaded image BLOB streaming."""
    yield from iter_asset_original_data(image_id, chunk_size, offset=offset, length=length)


def get_asset_detail(asset_id: int) -> ImageDetail | None:
    # Ready details remain a cheap read even while the repair decodes another asset.
    with closing(get_conn()) as conn:
        row = conn.execute('SELECT metadata_json, error FROM images WHERE id = ?', (asset_id,)).fetchone()
    if row is not None and row['metadata_json'] is None and row['error'] is None:
        return _get_pending_asset_detail(asset_id)
    return _get_asset_detail(asset_id)


@serialized_asset_work
def _get_pending_asset_detail(asset_id: int) -> ImageDetail | None:
    return _get_asset_detail(asset_id)


def _get_asset_detail(asset_id: int) -> ImageDetail | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT i.id, i.folder_id, i.rel_path, i.file_name, i.media_type,
                i.mime_type, i.format, i.width, i.height, i.mode, i.duration,
                i.frame_rate, i.codec, i.error, i.metadata_json,
                i.ai_annotations_json, i.preview_status, i.preview_error,
                i.is_favorite, i.rating, i.note,
                i.has_original_data,
                (i.has_original_data = 0) AS has_local_file,
                f.path AS folder_path
            FROM images i
            LEFT JOIN folders f ON f.id = i.folder_id
            WHERE i.id = ?""",
            (asset_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["has_original_data"] = bool(d["has_original_data"])
        d["has_local_file"] = bool(d["has_local_file"])
        if (
            d.get("media_type") == "image"
            and d.get("metadata_json") is None
            and d.get("error") is None
        ):
            img_id = d["id"]
            try:
                if d.get("has_original_data"):
                    original_row = conn.execute(
                        "SELECT data FROM asset_blobs WHERE image_id = ?",
                        (img_id,),
                    ).fetchone()
                    if not original_row or original_row["data"] is None:
                        raise FileNotFoundError("Database original not found")
                    from .extractor import extract_metadata_from_bytes
                    meta = extract_metadata_from_bytes(
                        bytes(original_row["data"]),
                        d["file_name"],
                    )
                else:
                    abs_path = Path(d["folder_path"]) / d["rel_path"]
                    if not abs_path.is_file():
                        raise FileNotFoundError(f"File not found: {abs_path}")
                    from .extractor import extract_metadata
                    meta = extract_metadata(abs_path)

                _update_image_metadata(conn, img_id, meta)
                conn.commit()
                d["format"] = meta.format
                d["width"] = meta.size[0] if meta.size else 0
                d["height"] = meta.size[1] if meta.size else 0
                d["mode"] = meta.mode
                d["error"] = meta.error
                d["metadata_json"] = meta.model_dump_json()
            except Exception as e:
                import traceback
                err_str = str(e) + "\n" + traceback.format_exc()
                conn.execute(
                    "UPDATE images SET error = ? WHERE id = ?",
                    (err_str, img_id),
                )
                conn.commit()
                d["error"] = err_str

        meta_json = d.pop("metadata_json", None)
        merged: dict[str, Any] = {}
        if meta_json:
            try:
                merged = json.loads(meta_json)
            except (json.JSONDecodeError, TypeError):
                pass

        ai_json = d.pop("ai_annotations_json", None)
        ai_annotations: dict[str, Any] | None = None
        if ai_json:
            try:
                parsed_ai = json.loads(ai_json)
                if isinstance(parsed_ai, dict):
                    ai_annotations = parsed_ai
            except (json.JSONDecodeError, TypeError):
                pass

        tag_rows = conn.execute(
            """SELECT t.name FROM image_tags it
            JOIN tags t ON t.id = it.tag_id
            WHERE it.image_id = ? ORDER BY t.name COLLATE NOCASE""",
            (d["id"],),
        ).fetchall()
        user_metadata = {
            "favorite": bool(d.get("is_favorite")),
            "rating": d.get("rating"),
            "note": d.get("note") or "",
            "tags": [str(row["name"]) for row in tag_rows],
        }

        wf = merged.get("workflow")
        prompt_api_json = merged.get("prompt_api_json")
        workflow_ui_json = merged.get("workflow_ui_json")

        if not isinstance(wf, dict) or "workflow_nodes" not in wf:
            from .extractor import parse_workflow_json
            target_api_json = prompt_api_json or (wf if isinstance(wf, dict) and not wf.get("workflow_nodes") else None)
            parsed_wf = parse_workflow_json(target_api_json, workflow_ui_json)
            if parsed_wf and parsed_wf.get("workflow_nodes"):
                wf = parsed_wf

        w, h = d.get("width"), d.get("height")
        return ImageDetail(
            id=d.get("id"),
            file_name=d.get("file_name", ""),
            media_type=d.get("media_type") or "image",
            mime_type=d.get("mime_type") or "application/octet-stream",
            format=merged.get("format") or d.get("format"),
            size=[w, h] if w and h else merged.get("size"),
            mode=merged.get("mode") or d.get("mode"),
            duration=d.get("duration"),
            frame_rate=d.get("frame_rate"),
            codec=d.get("codec"),
            preview_status=d.get("preview_status"),
            preview_error=d.get("preview_error"),
            error=d.get("error"),
            prompt_parameters=merged.get("prompt_parameters"),
            workflow=wf,
            workflow_ui_json=workflow_ui_json,
            exif=merged.get("exif"),
            raw_chunks=merged.get("raw_chunks"),
            raw_parameters=merged.get("raw_parameters"),
            raw_params=merged.get("raw_parameters"),
            folder_id=d.get("folder_id"),
            has_local_file=bool(d.get("has_local_file")),
            rating=d.get("rating"),
            embedded_metadata=merged or None,
            user_metadata=user_metadata,
            ai_annotations=ai_annotations,
        )
    finally:
        conn.close()


def get_existing_asset_ids(asset_ids: Iterable[int]) -> set[int]:
    """Return the subset of asset IDs that still exists in the library."""
    normalized = sorted({int(asset_id) for asset_id in asset_ids if int(asset_id) > 0})
    if not normalized:
        return set()

    conn = get_conn()
    try:
        existing: set[int] = set()
        for start in range(0, len(normalized), 500):
            chunk = normalized[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT id FROM images WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            existing.update(int(row["id"]) for row in rows)
        return existing
    finally:
        conn.close()


def get_image_detail(image_id: int) -> ImageDetail | None:
    """Compatibility wrapper for the existing image detail endpoint."""
    return get_asset_detail(image_id)


@serialized_asset_work
def delete_folder(folder_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM images WHERE folder_id = ?", (folder_id,))
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        # Image rows clean up via ON DELETE CASCADE; drop the folder scope itself.
        conn.execute(
            "DELETE FROM item_positions WHERE scope = ?",
            (f"folder:{folder_id}",),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_image(image_id: int) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Durable background job queue ─────────────────────────────────────

JOB_MAX_ATTEMPTS = 5


def enqueue_job_conn(
    conn: sqlite3.Connection, op: str, payload: dict[str, Any]
) -> int:
    """Enqueue on an existing connection (joins the caller's transaction)."""
    cur = conn.execute(
        "INSERT INTO job_queue (op, payload_json, status)"
        " VALUES (?, ?, 'pending')",
        (op, json.dumps(payload)),
    )
    return int(cur.lastrowid)


def enqueue_job(op: str, payload: dict[str, Any]) -> int:
    conn = get_conn()
    try:
        job_id = enqueue_job_conn(conn, op, payload)
        conn.commit()
        return job_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_next_job() -> dict[str, Any] | None:
    """Atomically claim the oldest due pending job (single statement)."""
    conn = get_conn()
    try:
        row = conn.execute(
            """UPDATE job_queue SET status = 'running',
                attempts = attempts + 1, updated_at = datetime('now')
            WHERE id = (
                SELECT id FROM job_queue
                WHERE status = 'pending' AND run_after <= datetime('now')
                ORDER BY id LIMIT 1
            )
            RETURNING id, op, payload_json, attempts""",
        ).fetchone()
        conn.commit()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "op": str(row["op"]),
            "payload": json.loads(row["payload_json"] or "{}"),
            "attempts": int(row["attempts"]),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_job(job_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM job_queue WHERE id = ?", (job_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fail_job(job_id: int, error: str, *, max_attempts: int = JOB_MAX_ATTEMPTS) -> str:
    """Return a job for retry (exponential backoff) or park it as dead."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT attempts FROM job_queue WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return "gone"
        attempts = int(row["attempts"])
        if attempts >= max_attempts:
            conn.execute(
                "UPDATE job_queue SET status = 'dead', last_error = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (error[:2000], job_id),
            )
            status = "dead"
        else:
            backoff = min(300, 5 * (2**attempts))
            run_after_sql = (
                datetime.now(timezone.utc) + timedelta(seconds=backoff)
            ).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE job_queue SET status = 'pending', run_after = ?,"
                " last_error = ?, updated_at = datetime('now') WHERE id = ?",
                (run_after_sql, error[:2000], job_id),
            )
            status = "pending"
        conn.commit()
        return status
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_stuck_jobs() -> int:
    """Crash recovery: jobs left 'running' by a dead worker become pending."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE job_queue SET status = 'pending',"
            " run_after = datetime('now'), updated_at = datetime('now')"
            " WHERE status = 'running'"
        )
        count = cur.rowcount
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_tombstoned_rel_paths(folder_id: int) -> set[str]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT rel_path FROM trash_tombstones WHERE folder_id = ?",
            (folder_id,),
        ).fetchall()
        return {str(row["rel_path"]) for row in rows}
    finally:
        conn.close()


def get_tombstones_by_image_ids(image_ids: list[int]) -> list[dict[str, Any]]:
    if not image_ids:
        return []
    conn = get_conn()
    try:
        found: list[dict[str, Any]] = []
        for start in range(0, len(image_ids), 500):
            chunk = image_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                "SELECT id, image_id, folder_id, folder_path, rel_path,"
                " file_name, has_local_file FROM trash_tombstones"
                f" WHERE image_id IN ({placeholders})",
                chunk,
            ).fetchall()
            found.extend(dict(row) for row in rows)
        return found
    finally:
        conn.close()


def delete_tombstones(tombstone_ids: list[int]) -> int:
    if not tombstone_ids:
        return 0
    conn = get_conn()
    try:
        removed = 0
        for start in range(0, len(tombstone_ids), 500):
            chunk = tombstone_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            cur = conn.execute(
                f"DELETE FROM trash_tombstones WHERE id IN ({placeholders})",
                chunk,
            )
            removed += cur.rowcount
        conn.commit()
        return removed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_diagnostics() -> dict[str, Any]:
    conn = get_conn()
    try:
        folders_row = conn.execute("SELECT COUNT(*) AS c FROM folders").fetchone()
        assets_row = conn.execute(
            """SELECT COUNT(*) AS assets,
                SUM(CASE WHEN media_type = 'image' THEN 1 ELSE 0 END) AS images,
                SUM(CASE WHEN media_type = 'video' THEN 1 ELSE 0 END) AS videos
            FROM images"""
        ).fetchone()
        uploads_row = conn.execute(
            "SELECT COUNT(*) AS c FROM images WHERE has_original_data = 1"
        ).fetchone()
        return {
            "db_path": get_db_path(),
            "folders": folders_row["c"] if folders_row else 0,
            "assets": int(assets_row["assets"] or 0) if assets_row else 0,
            "images": int(assets_row["images"] or 0) if assets_row else 0,
            "videos": int(assets_row["videos"] or 0) if assets_row else 0,
            "uploads": uploads_row["c"] if uploads_row else 0,
        }
    finally:
        conn.close()


def insert_upload_asset(
    file_name: str,
    original_data: bytes,
    *,
    media_type: str,
    has_generation_metadata: bool = False,
    format_name: str | None = None,
    width: int = 0,
    height: int = 0,
    mode: str | None = None,
    duration: float | None = None,
    frame_rate: float | None = None,
    codec: str | None = None,
    embedded_metadata: dict[str, Any] | None = None,
    preview_status: str = "pending",
    preview_error: str | None = None,
) -> tuple[int, int]:
    if media_type not in ("image", "video"):
        raise ValueError(f"Unsupported media type: {media_type}")

    conn = get_conn()
    try:
        has_embedded_metadata = media_type == "video" or has_generation_metadata
        folder_path = "__uploads__" if has_embedded_metadata else "__uploads_no_metadata__"
        folder_name = "Uploads" if has_embedded_metadata else "Uploads (no metadata)"

        folder_row = conn.execute(
            "SELECT id FROM folders WHERE path = ?", (folder_path,)
        ).fetchone()
        if not folder_row:
            conn.execute(
                "INSERT INTO folders (path, name) VALUES (?, ?)", (folder_path, folder_name)
            )
            folder_row = conn.execute(
                "SELECT id FROM folders WHERE path = ?", (folder_path,)
            ).fetchone()
        folder_id = folder_row["id"]

        safe_name = portable_filename(file_name)
        from .media import mime_type_for_path

        mime_type = mime_type_for_path(safe_name)
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        counter = 0
        while True:
            rel_path = safe_name if counter == 0 else f"{stem}_{counter}{suffix}"
            try:
                cur = conn.execute(
                    """INSERT INTO images (
                        folder_id, rel_path, file_name, file_size, file_mtime,
                        media_type, mime_type, format, width, height, mode,
                        duration, frame_rate, codec, metadata_json,
                        preview_status, preview_error, has_original_data, content_fingerprint
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    RETURNING id""",
                    (
                        folder_id,
                        rel_path,
                        safe_name,
                        len(original_data),
                        time.time(),
                        media_type,
                        mime_type,
                        format_name,
                        width,
                        height,
                        mode,
                        duration,
                        frame_rate,
                        codec,
                        json.dumps(embedded_metadata, ensure_ascii=False)
                        if embedded_metadata is not None
                        else None,
                        preview_status,
                        preview_error,
                        hashlib.sha256(original_data).hexdigest(),
                    ),
                )
                break
            except sqlite3.IntegrityError as exc:
                if "images.folder_id, images.rel_path" not in str(exc):
                    raise
                counter += 1

        row = cur.fetchone()
        image_id = row["id"]
        conn.execute(
            "INSERT INTO asset_blobs (image_id, data) VALUES (?, ?)",
            (image_id, original_data),
        )
        conn.commit()
        return image_id, folder_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_upload_image(
    file_name: str,
    original_data: bytes,
    has_metadata: bool,
) -> tuple[int, int]:
    """Compatibility wrapper for the former image-only upload path."""
    return insert_upload_asset(
        file_name,
        original_data,
        media_type="image",
        has_generation_metadata=has_metadata,
    )


def get_asset_original_data(asset_id: int) -> bytes | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT data FROM asset_blobs WHERE image_id = ?", (asset_id,)
        ).fetchone()
        if row and row["data"]:
            return bytes(row["data"])
        return None
    finally:
        conn.close()


def get_image_original_data(image_id: int) -> bytes | None:
    """Compatibility wrapper for callers using image terminology."""
    return get_asset_original_data(image_id)


def get_image_format(image_id: int) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT format, file_name FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        if row:
            if row["format"]:
                return row["format"]
            suffix = Path(row["file_name"]).suffix.lower().lstrip(".")
            return suffix or None
        return None
    finally:
        conn.close()
