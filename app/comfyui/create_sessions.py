"""Create sessions: named threads of Create tasks, like chats.

A session row only holds the name. Its runs are the Create tasks (workflow_drafts with
template 'create-task') whose resource selections carry ``session_id``, so the log, the
prompts, the errors and the output images all come from the task records themselves.

A session gets its name from the first request: a cut of the request right away, then a
short title from the free OpenCode model when one is installed. Earlier runs of the same
session are handed to the agent as context (see ``agent_history``).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app import database as db

logger = logging.getLogger(__name__)

SESSION_ID_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"
MAX_TITLE_CHARS = 60
FALLBACK_TITLE_CHARS = 48
# The agent sees at most this many earlier runs; the UI suggests a new session from here on.
HISTORY_RUNS = 20
HISTORY_REQUEST_CHARS = 400
HISTORY_PROMPT_CHARS = 900
TITLE_TIMEOUT_SECONDS = 90

MODE_TITLES = {
    "text": "Prompt",
    "variation": "Prompt from a photo",
    "reference": "Recreate a photo",
    "edit": "Photo edit",
}

_TASK_FIELDS = """
    d.id AS draft_id,
    json_extract(d.resource_selections_json, '$.create_task_id') AS task_id,
    json_extract(d.resource_selections_json, '$.backend') AS backend,
    json_extract(d.resource_selections_json, '$.request.mode') AS mode,
    json_extract(d.resource_selections_json, '$.request.request') AS request,
    json_extract(d.resource_selections_json, '$.request.prompt') AS own_prompt,
    json_extract(d.resource_selections_json, '$.request.use_agent') AS use_agent,
    json_extract(d.resource_selections_json, '$.request.batch_size') AS batch_size,
    json_extract(d.resource_selections_json, '$.request.aspect_ratio') AS aspect_ratio,
    json_extract(d.resource_selections_json, '$.request.denoise') AS denoise,
    json_extract(d.resource_selections_json, '$.request.subject_image') IS NOT NULL AS with_reference,
    d.values_json, d.status, d.created_at, d.updated_at
"""


def _iso(value: str | None) -> str | None:
    """SQLite's UTC ``datetime('now')`` text as ISO 8601 with a zone."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return value


def _session_payload(row: Any) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "title": row["title"],
        "title_state": row["title_state"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "runs": int(row["runs"]) if "runs" in keys and row["runs"] is not None else 0,
    }


def get_session(session_id: str) -> dict[str, Any] | None:
    conn = db.get_conn()
    try:
        row = conn.execute(
            """SELECT s.*, (SELECT COUNT(*) FROM workflow_drafts d WHERE d.template_id='create-task'
                AND json_extract(d.resource_selections_json, '$.session_id')=s.id) AS runs
            FROM create_sessions s WHERE s.id=?""", (session_id,),
        ).fetchone()
    finally:
        conn.close()
    return _session_payload(row) if row else None


def list_sessions(limit: int = 200) -> list[dict[str, Any]]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT s.*, COUNT(d.id) AS runs FROM create_sessions s
            LEFT JOIN workflow_drafts d ON d.template_id='create-task'
                AND json_extract(d.resource_selections_json, '$.session_id')=s.id
            GROUP BY s.id ORDER BY s.updated_at DESC, s.created_at DESC LIMIT ?""", (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [_session_payload(row) for row in rows]


def rename_session(session_id: str, title: str) -> dict[str, Any] | None:
    clean = _clean_title(title)
    if not clean:
        raise ValueError("The session name is empty.")
    conn = db.get_conn()
    try:
        cursor = conn.execute(
            "UPDATE create_sessions SET title=?, title_state='manual' WHERE id=?", (clean, session_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_session(session_id) if cursor.rowcount else None


def delete_session(session_id: str) -> bool:
    """Forget the session; its tasks and images stay (the images live in the library)."""
    conn = db.get_conn()
    try:
        cursor = conn.execute("DELETE FROM create_sessions WHERE id=?", (session_id,))
        conn.commit()
    finally:
        conn.close()
    return bool(cursor.rowcount)


# --- runs ----------------------------------------------------------------------------


def _task_rows(session_id: str) -> list[Any]:
    conn = db.get_conn()
    try:
        return conn.execute(
            f"""SELECT {_TASK_FIELDS} FROM workflow_drafts d WHERE d.template_id='create-task'
            AND json_extract(d.resource_selections_json, '$.session_id')=? ORDER BY d.id""", (session_id,),
        ).fetchall()
    finally:
        conn.close()


def _output_ids(values: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    for run in values.get("runs") or []:
        for output in run.get("outputs") or []:
            if isinstance(output, dict) and isinstance(output.get("id"), int):
                ids.append(output["id"])
        for asset_id in (run.get("run") or {}).get("output_asset_ids") or []:
            if isinstance(asset_id, int):
                ids.append(asset_id)
    return list(dict.fromkeys(ids))


def _existing_assets(ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    conn = db.get_conn()
    try:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, file_name, media_type, width, height FROM images WHERE id IN ({placeholders})", ids,
        ).fetchall()
    finally:
        conn.close()
    return {int(row["id"]): dict(row) for row in rows}


def _status(row: Any, values: dict[str, Any]) -> str:
    """queued tasks are split by phase: the agent writes, the prompt waits, or it renders."""
    if row["status"] != "queued":
        return row["status"]
    return {"agent": "agent", "running": "running"}.get(values.get("phase"), "queued")


def session_runs(session_id: str) -> list[dict[str, Any]]:
    """The session's runs, oldest first, with their prompts, errors and images."""
    rows = _task_rows(session_id)
    parsed = []
    all_ids: list[int] = []
    for row in rows:
        try:
            values = json.loads(row["values_json"] or "{}")
        except json.JSONDecodeError:
            values = {}
        ids = _output_ids(values)
        all_ids.extend(ids)
        parsed.append((row, values, ids))
    assets = _existing_assets(list(dict.fromkeys(all_ids)))
    runs = []
    for row, values, ids in parsed:
        plan = values.get("plan") or {}
        error = values.get("error") or None
        agent = plan.get("agent") or {}
        runs.append({
            "task_id": row["task_id"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "status": _status(row, values),
            "backend": row["backend"],
            "mode": row["mode"],
            "request": row["request"] or "",
            "use_agent": bool(row["use_agent"]),
            "with_reference": bool(row["with_reference"]),
            "batch_size": row["batch_size"],
            "aspect_ratio": row["aspect_ratio"],
            "denoise": row["denoise"],
            "prompt": plan.get("positive_prompt") or (row["own_prompt"] if not row["use_agent"] else "") or "",
            "negative_prompt": plan.get("negative_prompt") or "",
            "note": plan.get("note") or "",
            "agent_name": agent.get("name") if isinstance(agent, dict) else None,
            "edit_profile_label": plan.get("edit_profile_label") or "",
            "error": {
                "error": error.get("error") or "The run failed.",
                "code": error.get("code"),
                "stage": error.get("stage"),
                "technical_error": error.get("technical_error"),
            } if isinstance(error, dict) else None,
            "outputs": [
                {"id": asset_id, "deleted": False, **{key: assets[asset_id][key] for key in
                                                       ("file_name", "media_type", "width", "height")}}
                if asset_id in assets else {"id": asset_id, "deleted": True}
                for asset_id in ids
            ],
        })
    return runs


def agent_history(session_id: str | None, *, before_task: str | None = None) -> list[dict[str, str]]:
    """Earlier runs of the session that reached a prompt, oldest first, for the agent."""
    if not session_id:
        return []
    history = []
    for run in session_runs(session_id):
        if run["task_id"] == before_task:
            break
        if not run["prompt"]:
            continue
        history.append({
            "mode": run["mode"] or "text",
            "request": run["request"][:HISTORY_REQUEST_CHARS],
            "prompt": run["prompt"][:HISTORY_PROMPT_CHARS],
        })
    return history[-HISTORY_RUNS:]


# --- naming --------------------------------------------------------------------------


def _clean_title(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'«»“”`").strip()
    text = text.rstrip(".").strip()
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(",;:-") or text[:MAX_TITLE_CHARS]
    return text


def fallback_title(request: str, mode: str | None) -> str:
    """The first words of the request, or the kind of run when there are none."""
    line = next((part.strip() for part in str(request or "").splitlines() if part.strip()), "")
    if not line:
        return MODE_TITLES.get(mode or "", "New session")
    if len(line) > FALLBACK_TITLE_CHARS:
        line = line[:FALLBACK_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return line[:1].upper() + line[1:]


TITLE_TASK = """You name the sessions of an image generation app, like a chat app names its chats.
Write a short title, 2 to 5 words, that says what the user is making. Use the language of the
request. No quotes, no emoji, no final period, no words like "image", "photo of" or "request".

Return only this JSON object: {"title": "<the title>"}

FIRST REQUEST OF THE SESSION:
"""


def _parse_title(text: str) -> str:
    text = (text or "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict) and isinstance(data.get("title"), str):
                return _clean_title(data["title"])
        except json.JSONDecodeError:
            pass
    first = next((line for line in text.splitlines() if line.strip()), "")
    return _clean_title(first) if len(first) <= MAX_TITLE_CHARS * 2 else ""


def opencode_title(request: str) -> str | None:
    """A short title from the free OpenCode model, or None when there is no such model."""
    from app.ai.agents import implicit_opencode_profile
    from app.ai.execution.opencode import OpenCodePromptExecutor

    profile = implicit_opencode_profile()
    if profile is None:
        return None
    profile = {**profile, "timeout_seconds": TITLE_TIMEOUT_SECONDS}
    result = OpenCodePromptExecutor().execute_raw(
        profile=profile,
        task_package=TITLE_TASK + request.strip()[:2_000],
        title="MediaStudio session title",
    )
    return _parse_title(result.text) or None


# Swappable in tests; called on a worker thread with the first request.
title_namer: Callable[[str], str | None] = opencode_title


def _set_title(session_id: str, title: str, *, state: str, only_from: tuple[str, ...]) -> None:
    conn = db.get_conn()
    try:
        placeholders = ",".join("?" * len(only_from))
        conn.execute(
            f"UPDATE create_sessions SET title=?, title_state=? WHERE id=? AND title_state IN ({placeholders})",
            (title, state, session_id, *only_from),
        )
        conn.commit()
    finally:
        conn.close()


def _name_with_agent(session_id: str, request: str) -> None:
    try:
        title = title_namer(request)
    except Exception as exc:
        logger.info("Session %s keeps its first-words name: %s", session_id, exc)
        return
    if title:
        _set_title(session_id, title, state="agent", only_from=("fallback",))


def touch_session(session_id: str, *, request: str, mode: str | None, name_async: bool = True) -> None:
    """Record a run in the session: create it on its first run and give it a name."""
    conn = db.get_conn()
    try:
        conn.execute("INSERT OR IGNORE INTO create_sessions (id) VALUES (?)", (session_id,))
        conn.execute("UPDATE create_sessions SET updated_at=datetime('now') WHERE id=?", (session_id,))
        state = conn.execute("SELECT title_state FROM create_sessions WHERE id=?", (session_id,)).fetchone()
        conn.commit()
    finally:
        conn.close()
    if state is None or state["title_state"] != "pending":
        return
    _set_title(session_id, fallback_title(request, mode), state="fallback", only_from=("pending",))
    if not (request or "").strip():
        return
    if name_async:
        threading.Thread(
            target=_name_with_agent, args=(session_id, request), name=f"mediastudio-session-title-{session_id}",
            daemon=True,
        ).start()
    else:
        _name_with_agent(session_id, request)


__all__ = [
    "HISTORY_RUNS",
    "SESSION_ID_PATTERN",
    "agent_history",
    "delete_session",
    "fallback_title",
    "get_session",
    "list_sessions",
    "rename_session",
    "session_runs",
    "touch_session",
]
