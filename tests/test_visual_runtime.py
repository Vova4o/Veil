from __future__ import annotations

from pathlib import Path

import pytest

from app import database
from app.visual_runtime import backend as studio_backend
from app.visual_runtime.models import (
    EditPolicy,
    ExecutionOrigin,
    ExecutionPlan,
    FreeModeRequest,
    ReferenceRole,
    VisualOperation,
    VisualReference,
    VisualSession,
)
from app.visual_runtime import store as visual_store


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    old_db_path = database.get_db_path()
    db_path = tmp_path / "visual_runtime.sqlite3"
    database.set_db_path(db_path)
    database.init_db()
    try:
        yield db_path
    finally:
        database.set_db_path(old_db_path)


def _seed_image(folder_id: int = 1) -> int:
    conn = database.get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO folders (id, path, name) VALUES (?, ?, ?)",
            (folder_id, f"/tmp/visual-{folder_id}", f"visual-{folder_id}"),
        )
        cur = conn.execute(
            """INSERT INTO images (folder_id, rel_path, file_name, media_type, mime_type)
            VALUES (?, ?, ?, 'image', 'image/png')""",
            (folder_id, f"img-{uuid_suffix()}.png", "img.png"),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


_counter = 0


def uuid_suffix() -> str:
    global _counter
    _counter += 1
    return f"{_counter:06d}"


# ── Step 1: backend pin ──────────────────────────────────────────────


def test_studio_backend_pin():
    assert (
        studio_backend.STUDIO_BACKEND_MODEL_FILENAME == "moodyKrea2Mix_v40.safetensors"
    )
    assert studio_backend.STUDIO_BACKEND_ARCHITECTURE == "krea2"
    assert studio_backend.is_studio_backend_file("moodyKrea2Mix_v40.safetensors")
    assert studio_backend.is_studio_backend_file("MOODYKREA2MIX_V40.SAFETENSORS")
    # Migration bridge: current model_04 file still resolves.
    assert studio_backend.is_studio_backend_file(
        "Moody-Krea-Mix-v7_00002__clean_fp8.safetensors"
    )
    assert not studio_backend.is_studio_backend_file("flux1-dev.safetensors")


def test_studio_backend_health_unknown_and_ready():
    unknown = studio_backend.check_studio_backend_health(None)
    assert unknown["status"] == "unknown"
    assert unknown["ready"] is False

    class FakeInventory:
        models = {"diffusion_models": ["moodyKrea2Mix_v40.safetensors"]}

    ready = studio_backend.check_studio_backend_health(FakeInventory())
    assert ready["ready"] is True
    assert ready["status"] == "ready"

    class EmptyInventory:
        models = {"diffusion_models": ["other.safetensors"]}

    missing = studio_backend.check_studio_backend_health(EmptyInventory())
    assert missing["ready"] is False
    assert missing["status"] == "not_installed"


# ── Step 2: contracts ────────────────────────────────────────────────


def test_visual_session_contract_keeps_original_and_current_separate():
    session = VisualSession(
        id="abc123",
        original_asset_id=1,
        current_asset_id=2,
        references=(
            VisualReference(asset_id=1, role=ReferenceRole.CHARACTER),
            VisualReference(asset_id=3, role=ReferenceRole.ENVIRONMENT),
        ),
        instruction="Make us read books on the bed",
    )
    assert session.original_asset_id == 1
    assert session.current_asset_id == 2
    assert session.references[0].role == ReferenceRole.CHARACTER


def test_visual_operations_cover_plan_set():
    expected = {
        "generate_scene",
        "compose_scene",
        "edit_scene",
        "add_character",
        "replace_character",
        "change_pose",
        "change_wardrobe",
        "relocate",
        "replace_background",
        "adjust_lighting",
        "typography",
        "posterize",
        "finalize",
    }
    assert {op.value for op in VisualOperation} == expected


def test_edit_policy_defaults():
    policy = EditPolicy()
    assert policy.preserve_identity is True
    assert policy.preserve_pose is False
    assert policy.reference_strength == 1.0
    assert policy.grounding_resolution == 1024


def test_execution_plan_and_free_mode_request():
    plan = ExecutionPlan(
        session_id="s1",
        operation=VisualOperation.EDIT_SCENE,
        primitive="moody-edit",
        reason="pose edit is a policy over moody-edit",
    )
    assert plan.primitive.value == "moody-edit"

    free = FreeModeRequest(session_id="s1", instruction="try something wild")
    assert free.allow_install_nodes is False
    assert free.allow_download_models is False

    with pytest.raises(Exception):
        FreeModeRequest(session_id="s1", instruction="  ")


# ── Step 3: persistence ──────────────────────────────────────────────


def test_create_and_get_session(isolated_db):
    session_id = visual_store.create_session(instruction="base scene")
    fetched = visual_store.get_session(session_id)
    assert fetched is not None
    assert fetched["id"] == session_id
    assert fetched["original_asset_id"] is None
    assert fetched["current_asset_id"] is None
    assert fetched["references"] == []


def test_session_lineage_original_stays_stable(isolated_db):
    original = _seed_image()
    updated = _seed_image()
    session_id = visual_store.create_session(original_asset_id=original)
    visual_store.update_session_current(session_id, updated)
    fetched = visual_store.get_session(session_id)
    assert fetched is not None
    assert fetched["original_asset_id"] == original
    assert fetched["current_asset_id"] == updated


def test_references_crud(isolated_db):
    asset_a = _seed_image()
    asset_b = _seed_image()
    session_id = visual_store.create_session()
    ref_id = visual_store.add_reference(
        session_id, asset_a, ReferenceRole.CHARACTER.value, label="hero"
    )
    # Duplicate (session, asset, role) reuses the same row.
    ref_id_again = visual_store.add_reference(session_id, asset_a, "character")
    assert ref_id_again == ref_id

    visual_store.add_reference(session_id, asset_b, "environment")
    refs = visual_store.list_references(session_id)
    assert len(refs) == 2
    assert {r["role"] for r in refs} == {"character", "environment"}

    fetched = visual_store.get_session(session_id)
    assert fetched is not None
    assert len(fetched["references"]) == 2

    visual_store.remove_reference(ref_id)
    assert len(visual_store.list_references(session_id)) == 1


def test_actions_append_only_history(isolated_db):
    session_id = visual_store.create_session(instruction="edit chain")
    first = visual_store.append_action(
        session_id, VisualOperation.EDIT_SCENE, "change background"
    )
    second = visual_store.append_action(
        session_id,
        "change_pose",
        "change pose",
        origin=ExecutionOrigin.MCP_FREE.value,
        input_asset_ids=[1],
        output_asset_ids=[2],
    )
    assert second > first
    history = visual_store.list_actions(session_id)
    assert [h["operation"] for h in history] == ["edit_scene", "change_pose"]
    assert history[1]["origin"] == "mcp_free"
    assert history[1]["input_asset_ids"] == [1]
    assert history[1]["output_asset_ids"] == [2]
    # No update/delete API: append-only surface only.
    assert not hasattr(visual_store, "update_action")
    assert not hasattr(visual_store, "delete_action")


def test_append_action_rejects_unknown_operation(isolated_db):
    session_id = visual_store.create_session()
    with pytest.raises(Exception):
        visual_store.append_action(session_id, "not_a_real_op", "x")


def test_cascade_delete_removes_session_graph(isolated_db):
    asset_id = _seed_image()
    session_id = visual_store.create_session()
    visual_store.add_reference(session_id, asset_id, "character")
    visual_store.append_action(session_id, "edit_scene", "edit")
    conn = database.get_conn()
    try:
        conn.execute("DELETE FROM visual_sessions WHERE id = ?", (session_id,))
        conn.commit()
        remaining_refs = conn.execute(
            "SELECT COUNT(*) AS c FROM visual_references WHERE session_id = ?",
            (session_id,),
        ).fetchone()["c"]
        remaining_actions = conn.execute(
            "SELECT COUNT(*) AS c FROM visual_actions WHERE session_id = ?",
            (session_id,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert remaining_refs == 0
    assert remaining_actions == 0
