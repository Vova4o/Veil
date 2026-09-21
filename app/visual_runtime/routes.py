"""Studio HTTP API (EDITOR-STUDIO-RUNTIME-PLAN §5, plan migration step 5).

Endpoints revolve around VisualSession instead of Simple Mode model
profiles. Nothing here executes ComfyUI yet: ``resolve`` previews the typed
``ExecutionPlan`` and ``actions`` records history; graph compilation and
execution arrive with the first tested primitive graph.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from flask import Blueprint, current_app, jsonify, request
from pydantic import BaseModel, Field

from app.visual_runtime import store as visual_store
from app.visual_runtime.backend import (
    STUDIO_BACKEND_ARCHITECTURE,
    STUDIO_BACKEND_DISPLAY_NAME,
    STUDIO_BACKEND_MODEL_FILENAME,
    STUDIO_BACKEND_RESOURCE_FOLDER,
    check_studio_backend_health,
)
from app.visual_runtime.models import ExecutionOrigin, ReferenceRole, VisualOperation
from app.visual_runtime.primitives import (
    compile_plan,
    list_primitives,
    validate_primitive,
)
from app.visual_runtime.store import VisualSessionStoreError

logger = logging.getLogger(__name__)
studio_blueprint = Blueprint("studio", __name__)


def _inventory_or_none():
    try:
        from app.comfyui.workflow_inventory import cached_runtime_inventory

        return cached_runtime_inventory(current_app.config["CONFIG_STORE"])
    except Exception:
        return None


def _store_error_response(exc: Exception):
    message = str(exc)
    if "was not found" in message:
        return jsonify({"error": message}), 404
    return jsonify({"error": message}), 400


class SessionCreateRequest(BaseModel):
    instruction: str = Field(default="", max_length=20_000)
    original_asset_id: int | None = Field(default=None, ge=1)


class SessionPatchRequest(BaseModel):
    instruction: str | None = Field(default=None, max_length=20_000)
    current_asset_id: int | None = Field(default=None, ge=1)


class ReferenceAddRequest(BaseModel):
    asset_id: int = Field(ge=1)
    role: ReferenceRole
    label: str | None = Field(default=None, max_length=160)


class ActionAppendRequest(BaseModel):
    operation: VisualOperation
    instruction: str = Field(default="", max_length=20_000)
    origin: ExecutionOrigin = ExecutionOrigin.RUNTIME
    input_asset_ids: list[int] = Field(default_factory=list)
    output_asset_ids: list[int] = Field(default_factory=list)
    workflow_run_id: int | None = Field(default=None, ge=1)


class ResolveRequest(BaseModel):
    operation: VisualOperation
    instruction: str = Field(default="", max_length=20_000)


def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session["id"],
        "original_asset_id": session["original_asset_id"],
        "current_asset_id": session["current_asset_id"],
        "instruction": session["instruction"],
        "references": [
            {
                "id": ref["id"],
                "asset_id": ref["asset_id"],
                "role": ref["role"],
                "label": ref.get("label"),
            }
            for ref in session.get("references", [])
        ],
    }


@studio_blueprint.route("/api/studio/backend", methods=["GET"])
def studio_backend():
    """Pinned Studio v1 backend plus live health against the inventory."""
    return jsonify({
        "filename": STUDIO_BACKEND_MODEL_FILENAME,
        "display_name": STUDIO_BACKEND_DISPLAY_NAME,
        "architecture": STUDIO_BACKEND_ARCHITECTURE,
        "folder": STUDIO_BACKEND_RESOURCE_FOLDER,
        "health": check_studio_backend_health(_inventory_or_none()),
    })


@studio_blueprint.route("/api/studio/primitives", methods=["GET"])
def studio_primitives():
    """Registered workflow primitives with validation status."""
    inventory = _inventory_or_none()
    return jsonify({
        "primitives": [validate_primitive(item["id"], inventory) for item in list_primitives()],
    })


@studio_blueprint.route("/api/studio/sessions", methods=["POST"])
def studio_create_session():
    try:
        req = SessionCreateRequest.model_validate(request.get_json(silent=True) or {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        session_id = visual_store.create_session(
            original_asset_id=req.original_asset_id,
            instruction=req.instruction.strip(),
        )
    except VisualSessionStoreError as exc:
        return _store_error_response(exc)
    except sqlite3.IntegrityError as exc:
        return jsonify({"error": f"Unknown asset reference: {exc}"}), 400
    session = visual_store.get_session(session_id)
    assert session is not None
    return jsonify({"session": _session_payload(session)}), 201


@studio_blueprint.route("/api/studio/sessions/<session_id>", methods=["GET"])
def studio_get_session(session_id: str):
    session = visual_store.get_session(session_id)
    if session is None:
        return jsonify({"error": f"visual session '{session_id}' was not found"}), 404
    return jsonify({"session": _session_payload(session)})


@studio_blueprint.route("/api/studio/sessions/<session_id>", methods=["PATCH"])
def studio_patch_session(session_id: str):
    try:
        req = SessionPatchRequest.model_validate(request.get_json(silent=True) or {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        if req.instruction is not None:
            visual_store.set_session_instruction(session_id, req.instruction)
        if req.current_asset_id is not None:
            visual_store.update_session_current(session_id, req.current_asset_id)
    except VisualSessionStoreError as exc:
        return _store_error_response(exc)
    except sqlite3.IntegrityError as exc:
        return jsonify({"error": f"Unknown asset reference: {exc}"}), 400
    session = visual_store.get_session(session_id)
    assert session is not None
    return jsonify({"session": _session_payload(session)})


@studio_blueprint.route("/api/studio/sessions/<session_id>/references", methods=["POST"])
def studio_add_reference(session_id: str):
    try:
        req = ReferenceAddRequest.model_validate(request.get_json(silent=True) or {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        reference_id = visual_store.add_reference(
            session_id,
            req.asset_id,
            req.role.value,
            req.label,
        )
    except VisualSessionStoreError as exc:
        return _store_error_response(exc)
    except sqlite3.IntegrityError as exc:
        return jsonify({"error": f"Unknown session or asset reference: {exc}"}), 400
    references = visual_store.list_references(session_id)
    created = next((r for r in references if int(r["id"]) == reference_id), None)
    return jsonify({"reference": created}), 201


@studio_blueprint.route("/api/studio/references/<int:reference_id>", methods=["DELETE"])
def studio_delete_reference(reference_id: int):
    visual_store.remove_reference(reference_id)
    return jsonify({"ok": True})


@studio_blueprint.route("/api/studio/sessions/<session_id>/history", methods=["GET"])
def studio_session_history(session_id: str):
    session = visual_store.get_session(session_id)
    if session is None:
        return jsonify({"error": f"visual session '{session_id}' was not found"}), 404
    return jsonify({
        "session_id": session_id,
        "actions": visual_store.list_actions(session_id),
    })


@studio_blueprint.route("/api/studio/sessions/<session_id>/actions", methods=["POST"])
def studio_append_action(session_id: str):
    try:
        req = ActionAppendRequest.model_validate(request.get_json(silent=True) or {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    for asset_id in (*req.input_asset_ids, *req.output_asset_ids):
        if asset_id < 1:
            return jsonify({"error": "action asset IDs must be >= 1"}), 400
    try:
        action_id = visual_store.append_action(
            session_id,
            req.operation.value,
            req.instruction,
            origin=req.origin.value,
            input_asset_ids=tuple(req.input_asset_ids),
            output_asset_ids=tuple(req.output_asset_ids),
            workflow_run_id=req.workflow_run_id,
        )
        # Output Asset -> VisualSession.current_asset_id (§6 layer diagram).
        if req.output_asset_ids:
            visual_store.update_session_current(session_id, req.output_asset_ids[-1])
    except VisualSessionStoreError as exc:
        return _store_error_response(exc)
    except sqlite3.IntegrityError as exc:
        return jsonify({"error": f"Unknown session or asset reference: {exc}"}), 400
    actions = visual_store.list_actions(session_id)
    created = next((a for a in actions if int(a["id"]) == action_id), None)
    session = visual_store.get_session(session_id)
    assert session is not None
    return jsonify({
        "action": created,
        "session": _session_payload(session),
    }), 201


@studio_blueprint.route("/api/studio/sessions/<session_id>/resolve", methods=["POST"])
def studio_resolve(session_id: str):
    """Preview the typed ExecutionPlan for an operation (no execution)."""
    try:
        req = ResolveRequest.model_validate(request.get_json(silent=True) or {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    session = visual_store.get_session(session_id)
    if session is None:
        return jsonify({"error": f"visual session '{session_id}' was not found"}), 404
    try:
        plan = compile_plan(
            session_id,
            req.operation,
            req.instruction,
            has_current_image=session["current_asset_id"] is not None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    primitive_status = validate_primitive(plan.primitive, _inventory_or_none())
    return jsonify({
        "plan": plan.model_dump(mode="json"),
        "primitive": primitive_status,
    })


__all__ = ["studio_blueprint"]
