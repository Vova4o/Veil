"""SQLite persistence for VisualSession (§6 of EDITOR-STUDIO-RUNTIME-PLAN).

Layers stay separate::

    VisualSession -> VisualAction -> ExecutionPlan -> WorkflowDraft
        -> WorkflowRun -> Output Asset -> VisualSession.current_asset_id

``visual_actions`` is append-only: the store exposes insert + list but no
update/delete, so history, undo, comparison and provenance fall out of the
data model naturally.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from app.visual_runtime.models import ExecutionOrigin, VisualOperation


class VisualSessionStoreError(RuntimeError):
    """Raised when a visual session cannot be persisted or is misused."""


def _conn() -> sqlite3.Connection:
    from app import database as db

    return db.get_conn()


def _row_to_session(
    session_row: sqlite3.Row,
    references: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": str(session_row["id"]),
        "original_asset_id": session_row["original_asset_id"],
        "current_asset_id": session_row["current_asset_id"],
        "instruction": str(session_row["instruction"] or ""),
        "references": references,
        "created_at": str(session_row["created_at"]),
        "updated_at": str(session_row["updated_at"]),
    }


def create_session(
    *,
    session_id: str | None = None,
    original_asset_id: int | None = None,
    instruction: str = "",
) -> str:
    """Create a new visual session and return its ID."""
    resolved_id = (session_id or uuid.uuid4().hex[:32]).strip()
    if not resolved_id:
        raise VisualSessionStoreError("session_id cannot be empty")
    if original_asset_id is not None and original_asset_id < 1:
        raise VisualSessionStoreError("original_asset_id must be >= 1")
    conn = _conn()
    try:
        try:
            conn.execute(
                """INSERT INTO visual_sessions (id, original_asset_id, instruction)
                VALUES (?, ?, ?)""",
                (resolved_id, original_asset_id, instruction.strip()),
            )
        except sqlite3.IntegrityError as exc:
            raise VisualSessionStoreError(
                f"visual session '{resolved_id}' already exists"
            ) from exc
        conn.commit()
        return resolved_id
    finally:
        conn.close()


def get_session(session_id: str) -> dict[str, Any] | None:
    """Return a session with its references, or None when missing."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM visual_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        ref_rows = conn.execute(
            """SELECT id, asset_id, role, label, created_at
            FROM visual_references WHERE session_id = ? ORDER BY id ASC""",
            (session_id,),
        ).fetchall()
        references = [dict(r) for r in ref_rows]
        return _row_to_session(row, references)
    finally:
        conn.close()


def update_session_current(session_id: str, current_asset_id: int | None) -> None:
    """Point the session at its latest working state (original stays fixed)."""
    if current_asset_id is not None and current_asset_id < 1:
        raise VisualSessionStoreError("current_asset_id must be >= 1")
    conn = _conn()
    try:
        cur = conn.execute(
            """UPDATE visual_sessions
            SET current_asset_id = ?, updated_at = datetime('now')
            WHERE id = ?""",
            (current_asset_id, session_id),
        )
        if cur.rowcount == 0:
            raise VisualSessionStoreError(
                f"visual session '{session_id}' was not found"
            )
        conn.commit()
    finally:
        conn.close()


def set_session_instruction(session_id: str, instruction: str) -> None:
    conn = _conn()
    try:
        cur = conn.execute(
            """UPDATE visual_sessions
            SET instruction = ?, updated_at = datetime('now')
            WHERE id = ?""",
            (instruction.strip(), session_id),
        )
        if cur.rowcount == 0:
            raise VisualSessionStoreError(
                f"visual session '{session_id}' was not found"
            )
        conn.commit()
    finally:
        conn.close()


def add_reference(
    session_id: str,
    asset_id: int,
    role: str,
    label: str | None = None,
) -> int:
    """Attach an asset to a session. Duplicate (session, asset, role) reuses."""
    if asset_id < 1:
        raise VisualSessionStoreError("asset_id must be >= 1")
    conn = _conn()
    try:
        session = conn.execute(
            "SELECT id FROM visual_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise VisualSessionStoreError(
                f"visual session '{session_id}' was not found"
            )
        cleaned_label = label.strip() if label and label.strip() else None
        try:
            cur = conn.execute(
                """INSERT INTO visual_references (session_id, asset_id, role, label)
                VALUES (?, ?, ?, ?)""",
                (session_id, asset_id, str(role), cleaned_label),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "unique" in message:
                existing = conn.execute(
                    """SELECT id FROM visual_references
                    WHERE session_id = ? AND asset_id = ? AND role = ?""",
                    (session_id, asset_id, str(role)),
                ).fetchone()
                if existing is not None:
                    return int(existing["id"])
            raise VisualSessionStoreError(f"cannot add reference: {exc}") from exc
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_references(session_id: str) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT id, session_id, asset_id, role, label, created_at
            FROM visual_references WHERE session_id = ? ORDER BY id ASC""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def remove_reference(reference_id: int) -> None:
    conn = _conn()
    try:
        conn.execute(
            "DELETE FROM visual_references WHERE id = ?",
            (reference_id,),
        )
        conn.commit()
    finally:
        conn.close()


def append_action(
    session_id: str,
    operation: str | VisualOperation,
    instruction: str = "",
    *,
    origin: str | ExecutionOrigin = ExecutionOrigin.RUNTIME,
    input_asset_ids: tuple[int, ...] | list[int] = (),
    output_asset_ids: tuple[int, ...] | list[int] = (),
    workflow_run_id: int | None = None,
) -> int:
    """Append one history entry. There is intentionally no update/delete."""
    operation_value = (
        operation.value if isinstance(operation, VisualOperation) else str(operation)
    )
    try:
        VisualOperation(operation_value)
    except ValueError as exc:
        raise VisualSessionStoreError(
            f"unknown visual operation '{operation_value}'"
        ) from exc
    origin_value = origin.value if isinstance(origin, ExecutionOrigin) else str(origin)
    try:
        ExecutionOrigin(origin_value)
    except ValueError as exc:
        raise VisualSessionStoreError(
            f"unknown execution origin '{origin_value}'"
        ) from exc
    for asset_id in (*input_asset_ids, *output_asset_ids):
        if asset_id < 1:
            raise VisualSessionStoreError("action asset IDs must be >= 1")
    if workflow_run_id is not None and workflow_run_id < 1:
        raise VisualSessionStoreError("workflow_run_id must be >= 1")
    conn = _conn()
    try:
        session = conn.execute(
            "SELECT id FROM visual_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise VisualSessionStoreError(
                f"visual session '{session_id}' was not found"
            )
        cur = conn.execute(
            """INSERT INTO visual_actions
                (session_id, operation, instruction, origin,
                 input_asset_ids_json, output_asset_ids_json, workflow_run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                operation_value,
                instruction.strip(),
                origin_value,
                json.dumps(list(input_asset_ids)),
                json.dumps(list(output_asset_ids)),
                workflow_run_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_actions(session_id: str) -> list[dict[str, Any]]:
    """Return append-only history ordered oldest-first (natural undo order)."""
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT id, session_id, operation, instruction, origin,
                input_asset_ids_json, output_asset_ids_json,
                workflow_run_id, created_at
            FROM visual_actions WHERE session_id = ? ORDER BY id ASC""",
            (session_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["input_asset_ids"] = json.loads(
                    item.pop("input_asset_ids_json") or "[]"
                )
            except (json.JSONDecodeError, TypeError):
                item["input_asset_ids"] = []
            try:
                item["output_asset_ids"] = json.loads(
                    item.pop("output_asset_ids_json") or "[]"
                )
            except (json.JSONDecodeError, TypeError):
                item["output_asset_ids"] = []
            result.append(item)
        return result
    finally:
        conn.close()


__all__ = [
    "VisualSessionStoreError",
    "add_reference",
    "append_action",
    "create_session",
    "get_session",
    "list_actions",
    "list_references",
    "remove_reference",
    "set_session_instruction",
    "update_session_current",
]
