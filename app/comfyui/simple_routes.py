from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import random
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Literal

from flask import Blueprint, current_app, jsonify, render_template, request
from pydantic import BaseModel

from app import database as db
from app import library as media_library
from app.ai.enhancement import PromptEnhancementService
from app.ai.profiles import AIProfileStore
from app.ai.prompting import PromptOperation, PromptScenario, PromptTask
from app.ai.reconstruction import SceneAnalysisService
from app.ai.translation import PromptText
from app.config_store import ConfigStore

from .client import ComfyUIClient, ComfyUIClientError
from .detector import detect_comfyui
from .node_packs import (
    NodePackInstallError,
    custom_nodes_root,
    install_node_pack,
    installed_node_packs,
)
from .simple_downloader import SimpleModelDownloaderError, SimpleModelDownloaderService
from .simple_profiles import (
    APPROVED_PROFILES,
    ApprovedProfile,
    QualityPresetLevel,
    check_profile_health,
    compile_edit_workflow,
    compile_simple_workflow,
    resolve_dimensions,
    resolve_edit_profile,
    serialize_approved_profile,
)
from .workflow_execution import WorkflowExecutionService
from .workflow_inventory import cached_runtime_inventory, client_from_store
from .workflow_store import WorkflowStore

logger = logging.getLogger(__name__)
simple_blueprint = Blueprint("simple_mode", __name__)


def _config_store() -> ConfigStore:
    return current_app.config["CONFIG_STORE"]


def _ai_profile_store() -> AIProfileStore:
    return AIProfileStore(
        Path(current_app.config["CONFIG_FILE"]),
        secret_store=current_app.config.get("AI_SECRET_STORE"),
    )


def _list_ai_profiles() -> list[dict[str, Any]]:
    ai_data = _ai_profile_store().list()
    if isinstance(ai_data, dict):
        return ai_data.get("profiles", [])
    if isinstance(ai_data, list):
        return ai_data
    return []


def _client() -> ComfyUIClient:
    return client_from_store(_config_store())


def _workflow_store() -> WorkflowStore:
    return WorkflowStore()


def _download_service() -> SimpleModelDownloaderService:
    return SimpleModelDownloaderService(_config_store())


def _catalog_payload() -> list[dict[str, Any]]:
    return [
        serialize_approved_profile(profile, include_health=False)
        for profile in APPROVED_PROFILES.values()
    ]


def _ai_status_payload() -> dict[str, Any]:
    ai_profiles = _list_ai_profiles()
    has_text = any(
        "text" in profile.get("roles", []) or "translator" in profile.get("roles", [])
        for profile in ai_profiles
    )
    has_vision = any(
        "vision" in profile.get("roles", []) or profile.get("multimodal") is True
        for profile in ai_profiles
    )
    return {
        "available": bool(ai_profiles),
        "has_text": has_text,
        "has_vision": has_vision,
        "profile_count": len(ai_profiles),
    }


def _query_ambient_candidates(*, current_id: int | None = None) -> list[dict[str, Any]]:
    # The registered source path is authoritative; no runtime/model directory is used.
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT i.id, i.folder_id, i.rel_path, i.file_name, i.width, i.height,
                       i.has_original_data,
                       f.path AS folder_path,
                       ROW_NUMBER() OVER (
                           PARTITION BY i.folder_id ORDER BY (i.id = ?) DESC, RANDOM()
                       ) AS source_position
                FROM images i
                LEFT JOIN folders f ON f.id = i.folder_id
                WHERE i.media_type = 'image'
                  AND (f.enabled = 1 OR f.id IS NULL)
                  AND (f.source_status IN ('available', 'partially_available') OR f.id IS NULL)
            )
            ORDER BY (id = ?) DESC, source_position, RANDOM()
            LIMIT 360
            """,
            (current_id, current_id),
        ).fetchall()
    finally:
        conn.close()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not row["has_original_data"]:
            folder_path = row["folder_path"]
            if not folder_path or not (Path(folder_path) / row["rel_path"]).is_file():
                continue
        asset_id = int(row["id"])
        candidates.append({
            "id": asset_id,
            "_folder_id": row["folder_id"],
            "file_name": row["file_name"] or "",
            "original_url": f"/api/original/{asset_id}",
            "preview_url": f"/api/preview/{asset_id}",
            "thumbnail_url": f"/api/thumbnail/{asset_id}",
            "width": int(row["width"] or 0),
            "height": int(row["height"] or 0),
        })
    return candidates


def _ambient_payload(limit: int = 72, *, current_id: int | None = None) -> list[dict[str, Any]]:
    try:
        candidates = _query_ambient_candidates(current_id=current_id)
    except Exception as exc:
        logger.debug("Failed to load ambient library assets: %s", exc)
        return []

    # Shuffle within sources and between sources, then take one image per source
    # each round. A large folder must not crowd a small folder out of the pool.
    by_source: dict[int | None, list[dict[str, Any]]] = {}
    for candidate in candidates:
        item = dict(candidate)
        source_id = item.pop("_folder_id")
        by_source.setdefault(source_id, []).append(item)
    groups = list(by_source.values())
    for group in groups:
        random.shuffle(group)
    random.shuffle(groups)
    # Keep the currently displayed file in refreshes when its source is still
    # available. Absence from a random sample does not mean the file was removed.
    if current_id is not None:
        for index, group in enumerate(groups):
            current = next((item for item in group if item["id"] == current_id), None)
            if current is not None:
                group.remove(current)
                group.append(current)
                groups.insert(0, groups.pop(index))
                break
    pool: list[dict[str, Any]] = []
    while groups and len(pool) < limit:
        remaining = []
        for group in groups:
            pool.append(group.pop())
            if group:
                remaining.append(group)
            if len(pool) >= limit:
                break
        groups = remaining
    return pool


def _inventory_or_none():
    try:
        return cached_runtime_inventory(_config_store())
    except Exception:
        return None


def _custom_nodes_root() -> Path | None:
    from .manager import resolve_comfyui_installation

    try:
        detection = resolve_comfyui_installation(_config_store())
    except Exception:
        return None
    return custom_nodes_root(detection.comfy_dir) if detection else None


def _profile_health(profile: ApprovedProfile, inventory: Any | None = None) -> dict[str, Any]:
    installed = (
        installed_node_packs(profile.custom_nodes, _custom_nodes_root())
        if profile.custom_nodes
        else None
    )
    return check_profile_health(
        profile,
        inventory if inventory is not None else _inventory_or_none(),
        installed_node_packs=installed,
    )


def _manual_node_install(pack) -> dict[str, str]:
    return {
        "id": pack.id,
        "display_name": pack.display_name,
        "repository": pack.repository,
        "commit": pack.commit,
        "folder": pack.folder,
        "command": (
            f"git clone {pack.repository} custom_nodes/{pack.folder} && "
            f"git -C custom_nodes/{pack.folder} checkout {pack.commit}"
        ),
    }


@simple_blueprint.route("/create")
@simple_blueprint.route("/editor")
def simple_mode_page():
    initial_models = _catalog_payload()
    initial_ambient = _ambient_payload(16)
    return render_template(
        "create.html",
        initial_models=initial_models,
        initial_ambient=initial_ambient,
        default_model_id=initial_models[0]["id"] if initial_models else "",
    )


@simple_blueprint.route("/api/simple/models", methods=["GET"])
def simple_models():
    return jsonify({
        "models": _catalog_payload(),
        "default_model_id": next(iter(APPROVED_PROFILES), None),
    })


@simple_blueprint.route("/api/simple/ambient", methods=["GET"])
def simple_ambient():
    limit = request.args.get("limit", default=36, type=int) or 36
    current_id = request.args.get("current_id", type=int)
    return jsonify({"items": _ambient_payload(max(1, min(limit, 72)), current_id=current_id)})


@simple_blueprint.route("/api/simple/ai-status", methods=["GET"])
def simple_ai_status():
    return jsonify(_ai_status_payload())


@simple_blueprint.route("/api/simple/bootstrap", methods=["GET"])
def simple_bootstrap():
    return jsonify({
        "profiles": _catalog_payload(),
        "default_profile_id": next(iter(APPROVED_PROFILES), None),
        "ambient_candidates": _ambient_payload(),
        "ai_status": _ai_status_payload(),
    })


@simple_blueprint.route("/api/simple/models/<profile_id>/status", methods=["GET"])
@simple_blueprint.route("/api/simple/profiles/<profile_id>/status", methods=["GET"])
def simple_profile_status(profile_id: str):
    profile = APPROVED_PROFILES.get(profile_id)
    if not profile:
        return jsonify({"error": "Model not found"}), 404
    if request.args.get("refresh") == "1":
        from .workflow_inventory import invalidate_inventory_cache
        invalidate_inventory_cache()
    return jsonify({
        "profile_id": profile_id,
        "health": _profile_health(profile),
    })


@simple_blueprint.route("/api/simple/models/<profile_id>/install", methods=["POST"])
def simple_install_profile(profile_id: str):
    profile = APPROVED_PROFILES.get(profile_id)
    if not profile:
        return jsonify({"error": "Model not found"}), 404

    service = _download_service()
    try:
        model_root = service.resolve_model_root()
    except SimpleModelDownloaderError as exc:
        return jsonify({
            "error": str(exc),
            "code": "comfyui_path_required",
            "open_settings": True,
        }), 409

    health = _profile_health(profile)
    missing_names = {
        str(item.get("filename") or "").casefold()
        for item in health.get("missing_resources", [])
    }
    queued: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for dependency in profile.required_resources:
        if dependency.filename.casefold() not in missing_names:
            continue
        if not dependency.download_url:
            unavailable.append(dependency.display_name)
            continue
        try:
            queued.append(service.queue(
                profile_id=profile.id,
                display_name=dependency.display_name,
                folder=dependency.folder,
                filename=dependency.filename,
                source_url=dependency.download_url,
            ))
        except SimpleModelDownloaderError as exc:
            unavailable.append(f"{dependency.display_name}: {exc}")

    node_results: list[dict[str, Any]] = []
    manual_install: list[dict[str, str]] = []
    restart_required = bool(health.get("restart_required"))
    if profile.custom_nodes:
        nodes_root = _custom_nodes_root()
        on_disk = installed_node_packs(profile.custom_nodes, nodes_root)
        for pack in profile.custom_nodes:
            if pack.id in on_disk:
                continue
            if nodes_root is None:
                unavailable.append(f"{pack.display_name}: ComfyUI custom_nodes folder was not found")
                manual_install.append(_manual_node_install(pack))
                continue
            try:
                node_results.append(install_node_pack(pack, nodes_root))
                restart_required = True
            except NodePackInstallError as exc:
                logger.warning("Node pack %s install failed: %s", pack.id, exc)
                unavailable.append(f"{pack.display_name}: {exc}")
                node_results.append({"id": pack.id, "status": "failed", "code": exc.code, "error": str(exc)})
                manual_install.append(_manual_node_install(pack))
        if node_results:
            from .workflow_inventory import invalidate_runtime_inventory
            invalidate_runtime_inventory()

    return jsonify({
        "ok": not unavailable,
        "profile_id": profile.id,
        "model_root": str(model_root),
        "downloads": queued,
        "node_packs": node_results,
        "manual_install": manual_install,
        "restart_required": restart_required,
        "unavailable": unavailable,
    }), 202 if queued else 200


@simple_blueprint.route("/api/simple/downloads", methods=["GET"])
def simple_downloads():
    profile_id = str(request.args.get("profile_id") or "").strip() or None
    return jsonify({"items": _download_service().list(profile_id=profile_id)})


@simple_blueprint.route("/api/simple/downloads/<int:download_id>/pause", methods=["POST"])
def simple_pause_download(download_id: int):
    try:
        return jsonify({"item": _download_service().pause(download_id)})
    except SimpleModelDownloaderError as exc:
        return jsonify({"error": str(exc)}), 404


@simple_blueprint.route("/api/simple/downloads/<int:download_id>/resume", methods=["POST"])
@simple_blueprint.route("/api/simple/downloads/<int:download_id>/retry", methods=["POST"])
def simple_resume_download(download_id: int):
    try:
        return jsonify({"item": _download_service().resume(download_id)})
    except SimpleModelDownloaderError as exc:
        return jsonify({"error": str(exc)}), 404


@simple_blueprint.route("/api/simple/downloads/<int:download_id>/cancel", methods=["POST"])
def simple_cancel_download(download_id: int):
    try:
        return jsonify({"item": _download_service().cancel(download_id)})
    except SimpleModelDownloaderError as exc:
        return jsonify({"error": str(exc)}), 404


def _pick_windows_directory(initial_path: str) -> str | None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise RuntimeError("PowerShell не найден, поэтому системный выбор папки недоступен.")
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Выберите папку ComfyUI'
$dialog.ShowNewFolderButton = $false
$initial = $env:CMV_INITIAL_DIRECTORY
if ($initial -and (Test-Path -LiteralPath $initial)) { $dialog.SelectedPath = $initial }
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.SelectedPath
}
"""
    env = os.environ.copy()
    env["CMV_INITIAL_DIRECTORY"] = initial_path
    result = subprocess.run(
        [powershell, "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Не удалось открыть выбор папки.").strip()
        raise RuntimeError(message)
    selected = (result.stdout or "").strip().splitlines()
    return selected[-1].strip() if selected else None


def _pick_tk_directory(initial_path: str) -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - platform dependency
        raise RuntimeError("Системный выбор папки недоступен в этой сборке Python.") from exc
    root = tk.Tk()
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        selected = filedialog.askdirectory(
            title="Выберите папку ComfyUI",
            initialdir=initial_path or None,
            mustexist=True,
        )
        return str(selected).strip() or None
    finally:
        root.destroy()


@simple_blueprint.route("/api/simple/pick-comfyui-directory", methods=["POST"])
def simple_pick_comfyui_directory():
    remote = str(request.remote_addr or "")
    if remote not in {"127.0.0.1", "::1", "localhost"}:
        return jsonify({
            "error": "Выбор локальной папки доступен только при открытии приложения на этом компьютере."
        }), 403
    payload = request.get_json(silent=True) or {}
    initial_path = str(payload.get("initial_path") or "").strip()
    try:
        selected = (
            _pick_windows_directory(initial_path)
            if os.name == "nt"
            else _pick_tk_directory(initial_path)
        )
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return jsonify({"error": str(exc), "code": "directory_picker_unavailable"}), 503
    if not selected:
        return jsonify({"cancelled": True})
    detection = detect_comfyui(selected)
    return jsonify({
        "cancelled": False,
        "path": selected,
        "detection": detection.to_dict(),
    })


class SimpleGenerateRequest(BaseModel):
    profile_id: str = "model_01"
    prompt: str = ""
    improve_with_ai: bool = True
    aspect_ratio: str = "1:1"
    quality: str = "standard"
    batch_size: int = 1
    seed: int = -1
    reference_image: str | None = None
    negative_prompt: str | None = None
    # text: description only; reference: the image guides the prompt; edit: the image is changed.
    mode: Literal["text", "reference", "edit"] = "text"
    # edit only: a calibrated edit profile id and an optional donor photo (second reference).
    edit_profile: str | None = None
    subject_image: str | None = None


def _quality_level(value: str) -> QualityPresetLevel:
    normalized = (value or "standard").casefold()
    if normalized in {"high", "maximum"}:
        normalized = "detailed"
    try:
        return QualityPresetLevel(normalized)
    except ValueError:
        return QualityPresetLevel.STANDARD


def _first_profile_for_role(role: str) -> dict[str, Any] | None:
    for profile in _list_ai_profiles():
        roles = profile.get("roles", [])
        if role in roles:
            return profile
        if role == "vision" and profile.get("multimodal") is True:
            return profile
        if role == "text" and "translator" in roles:
            return profile
    return None


def _prepare_prompt(
    profile: ApprovedProfile,
    req: SimpleGenerateRequest,
) -> tuple[str, str, bool, str | None]:
    positive_prompt = req.prompt.strip()
    negative_prompt = (
        req.negative_prompt
        if req.negative_prompt is not None
        else profile.default_negative_prompt
    )
    ai_improved = False
    explanation = None

    if req.reference_image and req.reference_image.startswith("data:image/"):
        vision_profile = _first_profile_for_role("vision")
        if vision_profile:
            try:
                ai_store = _ai_profile_store()
                scene_service = SceneAnalysisService()
                task = PromptTask(
                    operation=PromptOperation.RECONSTRUCT,
                    family=profile.prompt_family,
                    scenario=PromptScenario.ILLUSTRATION_ART,
                    model_style=profile.prompt_style,
                )
                outcome = scene_service.analyze(
                    profile=vision_profile,
                    task=task,
                    image_data_url=req.reference_image,
                    api_key=ai_store.resolve_api_key(vision_profile),
                )
                subjects = ", ".join(
                    subject.kind for subject in outcome.scene_spec.subjects if subject.kind
                )
                background = outcome.scene_spec.composition.background or ""
                reconstructed = f"{subjects}, {background}".strip(", ")
                if reconstructed:
                    positive_prompt = (
                        f"{positive_prompt}, {reconstructed}"
                        if positive_prompt
                        else reconstructed
                    )
                    ai_improved = True
                    explanation = "Reconstructed from reference image"
            except Exception as exc:
                logger.warning("Vision reconstruction fallback: %s", exc)

    elif req.improve_with_ai and positive_prompt:
        text_profile = _first_profile_for_role("text")
        if text_profile:
            try:
                ai_store = _ai_profile_store()
                outcome = PromptEnhancementService().enhance(
                    profile=text_profile,
                    task=PromptTask(
                        operation=PromptOperation.ENHANCE,
                        family=profile.prompt_family,
                        scenario=PromptScenario.ILLUSTRATION_ART,
                        model_style=profile.prompt_style,
                    ),
                    source=PromptText(positive_prompt=positive_prompt),
                    api_key=ai_store.resolve_api_key(text_profile),
                )
                enhanced = outcome.enhancement.enhanced
                if enhanced.positive_prompt:
                    positive_prompt = enhanced.positive_prompt
                    if enhanced.negative_prompt:
                        negative_prompt = enhanced.negative_prompt
                    ai_improved = True
                    explanation = "Enhanced with AI prompt compiler"
            except Exception as exc:
                logger.warning("AI prompt improvement fallback: %s", exc)

    if not positive_prompt:
        positive_prompt = "Highly detailed image matching the provided visual direction"
    return positive_prompt, negative_prompt or "", ai_improved, explanation


MAX_SOURCE_IMAGE_BYTES = 40 * 1024 * 1024


def _decode_image_data_url(value: str | None) -> tuple[bytes, tuple[int, int]] | None:
    """Decode a ``data:image/...;base64`` URL and return its bytes and pixel size."""
    if not value or not value.startswith("data:image/") or "," not in value:
        return None
    header, payload = value.split(",", 1)
    if ";base64" not in header:
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > MAX_SOURCE_IMAGE_BYTES:
        return None
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            return data, (int(image.width), int(image.height))
    except (UnidentifiedImageError, OSError):
        return None


def _comfyui_offline_response():
    return jsonify({
        "error": "Could not connect to or automatically start ComfyUI.",
        "code": "comfyui_connection_failed",
        "suggestion": "ComfyUI is not running and automatic startup failed. Check the ComfyUI path in settings using the gear icon in the header.",
    }), 503


def _upload_input_image(client: ComfyUIClient, prefix: str, data: bytes) -> str:
    """Upload a photo to ComfyUI input/cmv and return the name a LoadImage node expects."""
    uploaded = client.upload_image(f"{prefix}_{uuid.uuid4().hex}.png", data, subfolder="cmv")
    subfolder = str(uploaded.get("subfolder") or "")
    return f"{subfolder}/{uploaded['name']}" if subfolder else str(uploaded["name"])


def _generate_fixed(profile: ApprovedProfile, req: SimpleGenerateRequest):
    """Queue a fixed-recipe profile run: one ComfyUI prompt per image with consecutive seeds."""
    if req.mode not in profile.modes:
        return jsonify({"error": f"Mode '{req.mode}' is not supported by {profile.name}.", "code": "mode_unsupported"}), 400

    image = _decode_image_data_url(req.reference_image) if req.mode in {"reference", "edit"} else None
    if req.mode in {"reference", "edit"} and image is None:
        return jsonify({"error": "Attach a valid image for this mode.", "code": "image_required"}), 400
    if req.mode == "edit" and not req.prompt.strip():
        return jsonify({"error": "Describe what to change in the photo.", "code": "instruction_required"}), 400
    subject = None
    edit_profile = None
    if req.mode == "edit":
        if req.subject_image:
            subject = _decode_image_data_url(req.subject_image)
            if subject is None:
                return jsonify({"error": "The second photo is not a valid image.", "code": "subject_invalid"}), 400
        try:
            edit_profile = resolve_edit_profile(profile, req.edit_profile, references=2 if subject else 1)
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": "edit_profile_invalid"}), 400
    elif req.subject_image or req.edit_profile:
        return jsonify({"error": "A second photo and edit profiles are only used in edit mode.", "code": "edit_only"}), 400
    width, height = resolve_dimensions(profile, req.aspect_ratio, image[1] if image else None)

    from .manager import ensure_comfyui_online
    from .workflow_inventory import invalidate_runtime_inventory

    if not ensure_comfyui_online(_config_store(), timeout=60.0):
        return _comfyui_offline_response()
    inventory = _inventory_or_none()
    if inventory is not None and not getattr(inventory, "online", False):
        # The cache may still hold the offline snapshot taken before ComfyUI came up.
        invalidate_runtime_inventory()
        inventory = _inventory_or_none()

    health = _profile_health(profile, inventory)
    if health["status"] == "not_installed":
        return jsonify({
            "error": "Required model components are missing.",
            "code": "model_not_installed",
            "missing_resources": health["missing_resources"],
        }), 409
    if health["modes_ready"].get(req.mode) is False:
        return jsonify({
            "error": health["message"],
            "code": "restart_required" if health["restart_required"] else "nodes_missing",
            "missing_nodes": health["missing_nodes"],
        }), 409

    # Edit negatives name what must disappear; they only act on cfg > 1 edit profiles.
    edit_negative = (req.negative_prompt or "").strip() if req.mode == "edit" else ""
    if req.mode == "edit":
        positive_prompt, ai_improved, ai_explanation = req.prompt.strip(), False, None
    else:
        positive_prompt, _negative, ai_improved, ai_explanation = _prepare_prompt(profile, req)

    client = _client()
    source_name = subject_name = None
    if req.mode == "edit":
        try:
            source_name = _upload_input_image(client, "cmv_source", image[0])
            if subject:
                subject_name = _upload_input_image(client, "cmv_subject", subject[0])
        except ComfyUIClientError as exc:
            return jsonify({"error": f"Could not send the photo to ComfyUI: {exc}", "code": "comfyui_rejected"}), 502

    batch = max(1, min(int(req.batch_size), profile.max_batch))
    base_seed = req.seed if req.seed >= 0 else random.randint(0, 1125899906842624 - batch)
    seeds = [base_seed + index for index in range(batch)]

    wf_store = _workflow_store()
    draft = wf_store.create_draft(
        template_id=f"simple-{profile.id}-{req.mode}",
        template_version="3.0.0",
        values={
            "mode": req.mode,
            "user_request": req.prompt,
            "positive_prompt": positive_prompt,
            "aspect_ratio": req.aspect_ratio,
            "width": width,
            "height": height,
            "batch_size": batch,
            "seeds": seeds,
            "source_image": source_name,
            "subject_image": subject_name,
            "edit_profile": edit_profile.id if edit_profile else None,
            "negative_prompt": edit_negative,
        },
        resource_selections={},
    )

    client_id = str(uuid.uuid4())
    runs: list[dict[str, Any]] = []
    for index, seed in enumerate(seeds):
        try:
            if req.mode == "edit":
                workflow = compile_edit_workflow(
                    profile,
                    instruction=positive_prompt,
                    source_image=source_name,
                    dimensions=(width, height),
                    seed=seed,
                    edit_profile=edit_profile.id if edit_profile else None,
                    subject_image=subject_name,
                    negative_instruction=edit_negative,
                )
            else:
                workflow = compile_simple_workflow(
                    profile,
                    positive_prompt=positive_prompt,
                    seed=seed,
                    mode=req.mode,
                    dimensions=(width, height),
                )
        except Exception as exc:
            return jsonify({"error": f"Failed to compile workflow: {exc}"}), 500
        try:
            result = client.queue_prompt(
                workflow,
                client_id=client_id,
                extra_data={
                    "comfy_meta_viewer": {
                        "simple_mode": True,
                        "profile_id": profile.id,
                        "mode": req.mode,
                        "draft_id": draft.id,
                        "batch_index": index,
                    },
                    "extra_pnginfo": {
                        "cmv_simple_profile": profile.id,
                        "cmv_create_mode": req.mode,
                        "cmv_user_request": req.prompt,
                        "cmv_prompt": positive_prompt,
                        **({"cmv_edit_profile": edit_profile.id} if edit_profile else {}),
                        **({"cmv_negative_prompt": edit_negative} if edit_negative else {}),
                    },
                },
            )
        except ComfyUIClientError as exc:
            if not runs:
                return jsonify({
                    "error": f"ComfyUI rejected prompt: {exc}",
                    "code": "comfyui_rejected",
                    "suggestion": "Make sure ComfyUI is running and the selected model components are installed.",
                }), 502
            logger.warning("Create batch stopped after %s of %s images: %s", len(runs), batch, exc)
            break
        except Exception as exc:
            if not runs:
                return jsonify({"error": f"Connection error: {exc}", "code": "comfyui_connection_failed"}), 503
            logger.warning("Create batch stopped after %s of %s images: %s", len(runs), batch, exc)
            break
        prompt_id = str(result["prompt_id"])
        run = wf_store.create_run(draft_id=draft.id, prompt_id=prompt_id, client_id=client_id)
        runs.append({"run_id": run.id, "prompt_id": prompt_id, "seed": seed})

    return jsonify({
        "ok": True,
        "run_id": runs[0]["run_id"],
        "run_ids": [item["run_id"] for item in runs],
        "prompt_id": runs[0]["prompt_id"],
        "runs": runs,
        "requested": batch,
        "profile_id": profile.id,
        "mode": req.mode,
        "edit_profile": edit_profile.id if edit_profile else None,
        "seconds_per_image": edit_profile.seconds if edit_profile else None,
        "width": width,
        "height": height,
        "positive_prompt": positive_prompt,
        "negative_prompt": edit_negative,
        "ai_improved": ai_improved,
        "ai_explanation": ai_explanation,
    })


@simple_blueprint.route("/api/simple/generate", methods=["POST"])
def simple_generate():
    try:
        req = SimpleGenerateRequest.model_validate(request.get_json(silent=True) or {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    profile = APPROVED_PROFILES.get(req.profile_id)
    if not profile:
        return jsonify({"error": f"Unknown model '{req.profile_id}'"}), 400

    if not profile.workflow_ready:
        return jsonify({
            "error": "Workflow for this model is still being calibrated.",
            "code": "workflow_pending",
        }), 409

    if profile.fixed_sampling:
        return _generate_fixed(profile, req)

    health = check_profile_health(profile, _inventory_or_none())
    if health["status"] == "not_installed":
        return jsonify({
            "error": "Required model components are missing.",
            "code": "model_not_installed",
            "missing_resources": health["missing_resources"],
        }), 409

    positive_prompt, negative_prompt, ai_improved, ai_explanation = _prepare_prompt(profile, req)
    quality_level = _quality_level(req.quality)

    try:
        compiled_workflow = compile_simple_workflow(
            profile,
            positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
            aspect_ratio=req.aspect_ratio,
            quality=quality_level,
            batch_size=req.batch_size,
            seed=req.seed,
        )
    except Exception as exc:
        return jsonify({"error": f"Failed to compile workflow: {exc}"}), 500

    wf_store = _workflow_store()
    draft = wf_store.create_draft(
        template_id=f"simple-{profile.id}",
        template_version="2.0.0",
        values={
            "positive_prompt": positive_prompt,
            "negative_prompt": negative_prompt,
            "aspect_ratio": req.aspect_ratio,
            "quality": quality_level.value,
            "batch_size": req.batch_size,
            "seed": req.seed,
        },
        resource_selections={},
    )

    from .manager import ensure_comfyui_online
    online = ensure_comfyui_online(_config_store(), timeout=60.0)
    if not online:
        return jsonify({
            "error": "Could not connect to or automatically start ComfyUI.",
            "code": "comfyui_connection_failed",
            "suggestion": "ComfyUI is not running and automatic startup failed. Check the ComfyUI path in settings using the gear icon in the header.",
        }), 503

    client_id = str(uuid.uuid4())
    try:
        result = _client().queue_prompt(
            compiled_workflow,
            client_id=client_id,
            extra_data={
                "comfy_meta_viewer": {
                    "simple_mode": True,
                    "profile_id": profile.id,
                    "quality": quality_level.value,
                    "draft_id": draft.id,
                },
                "extra_pnginfo": {
                    "cmv_simple_profile": profile.id,
                    "cmv_prompt": positive_prompt,
                },
            },
        )
        prompt_id = str(result["prompt_id"])
    except ComfyUIClientError as exc:
        return jsonify({
            "error": f"ComfyUI rejected prompt: {exc}",
            "code": "comfyui_rejected",
            "suggestion": "Make sure ComfyUI is running and the selected model components are installed.",
        }), 502
    except Exception as exc:
        return jsonify({
            "error": f"Connection error: {exc}",
            "code": "comfyui_connection_failed",
            "suggestion": "Could not reach ComfyUI. Check the connection and try again.",
        }), 503

    run = wf_store.create_run(
        draft_id=draft.id,
        prompt_id=prompt_id,
        client_id=client_id,
    )
    return jsonify({
        "ok": True,
        "run_id": run.id,
        "prompt_id": prompt_id,
        "profile_id": profile.id,
        "positive_prompt": positive_prompt,
        "negative_prompt": negative_prompt,
        "ai_improved": ai_improved,
        "ai_explanation": ai_explanation,
    })


def _output_asset_payload(asset_id: int) -> dict[str, Any] | None:
    try:
        result = media_library.get_assets(
            collection="all",
            asset_id=int(asset_id),
            page=1,
            per_page=1,
        )
    except Exception:
        return None
    assets = result.get("assets", [])
    if not assets:
        return None
    asset = assets[0]
    return {
        "id": int(asset["id"]),
        "filename": asset.get("file_name") or "",
        "preview_url": f"/api/preview/{int(asset['id'])}",
        "thumbnail_url": asset.get("thumbnail_url") or f"/api/thumbnail/{int(asset['id'])}",
        "width": int(asset.get("width") or 0),
        "height": int(asset.get("height") or 0),
    }


@simple_blueprint.route("/api/simple/runs/<int:run_id>", methods=["GET"])
def simple_get_run(run_id: int):
    wf_store = _workflow_store()
    try:
        run = WorkflowExecutionService(store=wf_store, client=_client()).refresh(run_id)
    except Exception as exc:
        import logging
        logging.getLogger("comfy-meta-viewer.comfyui").warning(
            "[CMV] refresh() failed for run %s: %s", run_id, exc,
        )
        run = wf_store.get_run(run_id)

    if run.status == "failed" and run.error:
        import logging
        log = logging.getLogger("comfy-meta-viewer.comfyui")
        log.error("[CMV] Run %s failed. Error: %s", run_id, run.error)

    outputs = [
        payload
        for asset_id in run.output_asset_ids
        if (payload := _output_asset_payload(int(asset_id))) is not None
    ]
    return jsonify({
        "run": run.model_dump(mode="json"),
        "status": run.status,
        "outputs": outputs,
        "is_complete": run.status in {"completed", "failed", "cancelled"},
    })


@simple_blueprint.route("/api/simple/runs/<int:run_id>/cancel", methods=["POST"])
def simple_cancel_run(run_id: int):
    wf_store = _workflow_store()
    try:
        run = WorkflowExecutionService(store=wf_store, client=_client()).cancel(run_id)
        return jsonify({"ok": True, "run": run.model_dump(mode="json")})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@simple_blueprint.route("/api/simple/assistant/chat", methods=["POST"])
def simple_assistant_chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    profile_id = str(data.get("profile_id", "model_01"))
    history = data.get("history", [])
    current_prompt = str(data.get("current_prompt", "")).strip()
    if not message:
        return jsonify({"error": "Message is required"}), 400

    profile = APPROVED_PROFILES.get(profile_id) or next(iter(APPROVED_PROFILES.values()), None)
    if profile is None:
        return jsonify({"error": "No Simple Mode models configured"}), 500

    text_profile = _first_profile_for_role("text")
    if not text_profile:
        return jsonify({
            "error": "No AI text profile configured. Add an AI provider in Integrations.",
            "code": "no_ai_profile",
        }), 400

    try:
        from app.ai.transport import run_openai_compatible_chat

        system_prompt = (
            "You are a concise prompt assistant for ComfyUI image generation.\n"
            f"Selected user model: {profile.name} ({profile.technical_name}).\n"
            f"Prompt family: {profile.prompt_family.value}.\n"
            "Help refine composition, lighting, subject details and clarity. "
            "Do not add generic quality buzzwords unless they are technically required by the model. "
            "Return only the final positive image prompt, without explanations, labels, markdown, or a negative prompt."
        )
        messages = [{"role": "system", "content": system_prompt}]
        if current_prompt:
            messages.append({"role": "system", "content": f"Current prompt: {current_prompt}"})
        for item in history[-8:]:
            messages.append({
                "role": item.get("role", "user"),
                "content": item.get("content", ""),
            })
        messages.append({"role": "user", "content": message})

        ai_store = _ai_profile_store()
        response = run_openai_compatible_chat(
            profile=text_profile,
            messages=messages,
            api_key=ai_store.resolve_api_key(text_profile),
        )
        return jsonify({"reply": response.text, "profile_id": profile.id})
    except Exception as exc:
        return jsonify({"error": f"AI Assistant error: {exc}"}), 500
