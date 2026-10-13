from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import os
import queue
import random
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from flask import Blueprint, Response, current_app, jsonify, render_template, request
from pydantic import BaseModel, Field

from app import database as db
from app import library as media_library
from app.ai import opencode_host
from app.ai.cli import CLIIntegrationError
from app.ai.execution import ExecutionRouter
from app.ai.model_catalog import native_cli_models
from app.ai.profiles import AIProfileStore
from app.config_store import ConfigStore

from .backends import BackendKind, ComfyUIJobClient, InferenceBackend
from .client import ComfyUIClient, ComfyUIClientError
from .create_agent import (
    CreateAgentError,
    CreateAgentResult,
    antigravity_models,
    instruction_paths,
    opencode_models,
    pick_agent_profile,
    run_create_agent,
    sees_images,
)
from .detector import detect_comfyui
from .node_packs import (
    NodePackInstallError,
    custom_nodes_root,
    install_node_pack,
    installed_node_packs,
)
from .progress_monitor import ProgressMonitor, monitor_for
from .simple_downloader import SimpleModelDownloaderError, SimpleModelDownloaderService
from .simple_profiles import (
    APPROVED_PROFILES,
    DEFAULT_VARIATION_DENOISE,
    ApprovedProfile,
    check_profile_health,
    compile_edit_workflow,
    compile_simple_workflow,
    compile_variation_workflow,
    resolve_dimensions,
    resolve_edit_profile,
    serialize_approved_profile,
    variation_dimensions,
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


def _client() -> ComfyUIClient:
    return client_from_store(_config_store())


def _progress_monitor(client: Any) -> ProgressMonitor | None:
    """The live-progress monitor of a real ComfyUI address (none for stand-in clients)."""
    host, port = getattr(client, "host", None), getattr(client, "port", None)
    if not isinstance(host, str) or not isinstance(port, int):
        return None
    return monitor_for(host, port)


def _workflow_store() -> WorkflowStore:
    return WorkflowStore()


def _download_service() -> SimpleModelDownloaderService:
    return SimpleModelDownloaderService(_config_store())


def _catalog_payload() -> list[dict[str, Any]]:
    return [
        serialize_approved_profile(profile, include_health=False)
        for profile in APPROVED_PROFILES.values()
    ]


def _agent_summary(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    try:
        capabilities = ExecutionRouter().capabilities_for(profile)
        accepts_images = bool(capabilities.image_inputs)
    except Exception:
        accepts_images = False
    return {
        "name": profile.get("name"),
        "kind": profile.get("kind"),
        "cli_type": profile.get("cli_type"),
        "model": profile.get("model"),
        "sees_images": accepts_images and sees_images(profile),
    }


def _agent_status_payload() -> dict[str, Any]:
    """Which agent Create will use for text and for photo modes (Integrations defaults)."""
    store = _ai_profile_store()
    try:
        text_agent = _agent_summary(pick_agent_profile(store, wants_image=False))
        photo_agent = _agent_summary(pick_agent_profile(store, wants_image=True))
    except Exception as exc:
        logger.debug("Create agent status failed: %s", exc)
        text_agent = photo_agent = None
    return {
        "available": text_agent is not None or photo_agent is not None,
        "text": text_agent,
        "photo": photo_agent,
        "instruction_files": instruction_paths(),
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
    nodes_root = _custom_nodes_root() if profile.custom_nodes else None
    installed = installed_node_packs(profile.custom_nodes, nodes_root) if nodes_root is not None else None
    return check_profile_health(
        profile,
        inventory if inventory is not None else _inventory_or_none(),
        installed_node_packs=installed,
    )


class _LocalBackend:
    """Create on the local ComfyUI, through this module's helpers (tests replace them)."""

    kind: BackendKind = "local"

    def client(self) -> ComfyUIClient:
        return _client()

    def monitor(self, client: Any) -> ProgressMonitor | None:
        return _progress_monitor(client)

    def ensure_ready(self, timeout: float) -> bool:
        from .manager import ensure_comfyui_online
        from .workflow_inventory import invalidate_runtime_inventory

        if not ensure_comfyui_online(_config_store(), timeout=timeout):
            return False
        inventory = _inventory_or_none()
        if inventory is not None and not getattr(inventory, "online", False):
            # The cache may still hold the offline snapshot taken before ComfyUI came up.
            invalidate_runtime_inventory()
        return True

    def not_ready_payload(self) -> dict[str, str]:
        return {
            "error": "Could not connect to or automatically start ComfyUI.",
            "code": "comfyui_connection_failed",
            "suggestion": "ComfyUI is not running and automatic startup failed. Check the ComfyUI path in settings using the gear icon in the header.",
        }

    def profile_health(self, profile: ApprovedProfile) -> dict[str, Any]:
        return _profile_health(profile)

    def run_details(self, prompt_id: str) -> dict[str, Any] | None:
        return None


def _backend(kind: BackendKind | None = None) -> InferenceBackend:
    """Where Create runs its graphs: the local ComfyUI, or a Colab GPU when chosen."""
    store = _config_store()
    if (kind or store.remote_settings()["backend"]) == "colab":
        from app.remote.colab_backend import colab_backend

        return colab_backend(store)
    return _LocalBackend()


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


@simple_blueprint.route("/api/simple/agent-status", methods=["GET"])
def simple_agent_status():
    return jsonify(_agent_status_payload())


@simple_blueprint.route("/api/simple/agents", methods=["GET"])
def simple_agents():
    """What Create's agent menu offers: OpenCode models by their cards, and the other agents.

    OpenCode models carry ``vision`` (sees photos), ``free`` and ``variants`` (reasoning
    levels) from OpenCode itself. Native Codex and Claude catalogs list their models;
    capability metadata takes precedence over explicitly labeled family fallbacks.
    """
    payload: dict[str, Any] = {"opencode": None, "antigravity": None, "codex": None, "claude": None, "agents": []}
    refresh = request.args.get("refresh") == "1"

    def opencode_section() -> dict[str, Any] | None:
        installations = opencode_host.find_installations()
        if not installations:
            return None
        installation = installations[0]
        models: list[dict[str, Any]] = []
        error = None
        try:
            models = [model.to_dict() for model in opencode_models(installation.executable, refresh=refresh)]
        except CLIIntegrationError as exc:
            error = str(exc)
        return {"label": installation.label, "variants": installation.generation >= 2, "models": models, "error": error}

    def antigravity_section() -> dict[str, Any] | None:
        try:
            executable, models = antigravity_models(refresh=refresh)
        except CLIIntegrationError as exc:
            return {"label": "Antigravity CLI", "models": [], "error": str(exc)}
        if executable is None:
            return None
        # Antigravity reports no input types; known families are an explicit fallback.
        return {"label": "Antigravity CLI", "models": models, "error": None, "vision_source": "family"}

    def native_section(host: str) -> dict[str, Any] | None:
        label = {"codex": "Codex", "claude": "Claude Code"}[host]
        try:
            executable, models = native_cli_models(host, refresh=refresh)
        except CLIIntegrationError as exc:
            return {"label": label, "models": [], "error": str(exc)}
        return {"label": label, "models": models, "error": None} if executable else None

    # Discovery is read-only and each host has its own timeout/error section.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {"opencode": pool.submit(opencode_section), "antigravity": pool.submit(antigravity_section),
                   "codex": pool.submit(native_section, "codex"), "claude": pool.submit(native_section, "claude")}
        for host, future in futures.items():
            payload[host] = future.result()
    seen: set[tuple[Any, Any, Any]] = set()
    for profile in _ai_profile_store().list().get("profiles", []):
        if not isinstance(profile, dict) or profile.get("cli_type") == "opencode":
            continue
        section = payload.get(profile.get("cli_type"))
        if section and any(profile.get("model") == model.get("model", model["id"])
                           or any(profile.get("model") == level["id"] for level in model.get("levels", []))
                           for model in section["models"]):
            continue
        roles = set(profile.get("roles") or ())
        if roles and roles <= {"translator"}:
            continue
        # Profiles that differ only by their role names run the same model.
        key = (profile.get("kind"), profile.get("cli_type") or profile.get("base_url"), profile.get("model"))
        if key in seen:
            continue
        seen.add(key)
        summary = _agent_summary(profile)
        if summary:
            payload["agents"].append({"id": profile.get("id"), **summary})
    return jsonify(payload)


@simple_blueprint.route("/api/simple/bootstrap", methods=["GET"])
def simple_bootstrap():
    return jsonify({
        "profiles": _catalog_payload(),
        "default_profile_id": next(iter(APPROVED_PROFILES), None),
        "ambient_candidates": _ambient_payload(),
        "agent_status": _agent_status_payload(),
    })


@simple_blueprint.route("/api/simple/models/<profile_id>/status", methods=["GET"])
@simple_blueprint.route("/api/simple/profiles/<profile_id>/status", methods=["GET"])
def simple_profile_status(profile_id: str):
    profile = APPROVED_PROFILES.get(profile_id)
    if not profile:
        return jsonify({"error": "Model not found"}), 404
    if request.args.get("refresh") == "1":
        from .workflow_inventory import invalidate_runtime_inventory
        invalidate_runtime_inventory()
    return jsonify({
        "profile_id": profile_id,
        "health": _backend().profile_health(profile),
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


def _pick_windows_directory(initial_path: str, title: str) -> str | None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise RuntimeError("PowerShell не найден, поэтому системный выбор папки недоступен.")
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $env:CMV_PICKER_TITLE
$dialog.ShowNewFolderButton = $true
$initial = $env:CMV_INITIAL_DIRECTORY
if ($initial -and (Test-Path -LiteralPath $initial)) { $dialog.SelectedPath = $initial }
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.SelectedPath
}
"""
    env = os.environ.copy()
    env["CMV_INITIAL_DIRECTORY"] = initial_path
    env["CMV_PICKER_TITLE"] = title
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


def _pick_tk_directory(initial_path: str, title: str) -> str | None:
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
            title=title,
            initialdir=initial_path or None,
            mustexist=False,
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
    title = str(payload.get("title") or "").strip()[:120] or "Choose a folder"
    try:
        selected = (
            _pick_windows_directory(initial_path, title)
            if os.name == "nt"
            else _pick_tk_directory(initial_path, title)
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


def _default_profile_id() -> str:
    return next(iter(APPROVED_PROFILES), "")


class SimpleGenerateRequest(BaseModel):
    profile_id: str = ""
    # The final model prompt (written by the agent, or edited by hand) or the edit command.
    prompt: str = ""
    # What the user typed; stored with the result next to the prompt.
    user_request: str | None = None
    aspect_ratio: str = "1:1"
    batch_size: int = 1
    seed: int = -1
    reference_image: str | None = None
    negative_prompt: str | None = None
    # text: description only; reference: the image guides the prompt; variation: the image is
    # also the starting point and is partly repainted; edit: the image is changed.
    mode: Literal["text", "reference", "variation", "edit"] = "text"
    # variation only: how much of the photo is repainted (0 keeps it, 1 ignores it).
    denoise: float | None = Field(default=None, ge=0.0, le=1.0)
    # edit only: a calibrated edit profile id and an optional donor photo (second reference).
    edit_profile: str | None = None
    subject_image: str | None = None
    # The agent that wrote the prompt, for metadata only.
    agent_name: str | None = None


class SimpleAgentRequest(BaseModel):
    profile_id: str = ""
    # variation uses the reference instruction: the agent describes the photo either way.
    mode: Literal["text", "reference", "variation", "edit"] = "text"
    request: str = ""
    previous_prompt: str | None = None
    aspect_ratio: str = "1:1"
    reference_image: str | None = None
    subject_image: str | None = None
    # An Integrations profile id that replaces the default agent for this run.
    agent_profile_id: str | None = None
    # Or a model and reasoning level picked from the host's native catalog in Create.
    agent_model: str | None = None
    agent_variant: str | None = None
    agent_host: Literal["opencode", "antigravity", "codex", "claude"] | None = None


class SimpleCreateRequest(SimpleAgentRequest):
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    use_agent: bool = True
    batch_size: int = Field(default=1, ge=1, le=4)
    denoise: float | None = Field(default=None, ge=0, le=1)
    prompt: str | None = None
    negative_prompt: str | None = None
    edit_profile: str | None = None
    agent_name: str | None = None
    seed: int = -1
    photo_name: str | None = None
    subject_name: str | None = None


def _create_task_service():
    from .create_tasks import CreateTaskService
    # Route dispatch is serialized only while creating the shared coordinator.
    with CREATE_TASK_LOCK:
        if "CREATE_TASK_SERVICE" not in current_app.config:
            current_app.config["CREATE_TASK_SERVICE"] = CreateTaskService(current_app._get_current_object())
        return current_app.config["CREATE_TASK_SERVICE"]


CREATE_TASK_LOCK = threading.Lock()


@simple_blueprint.route("/api/simple/tasks", methods=["POST"])
def simple_create_task():
    try:
        req = SimpleCreateRequest.model_validate(request.get_json(silent=True) or {})
        if req.mode != "edit" and req.subject_image:
            raise CreateAgentError("A second photo is only used in edit mode.", code="edit_only", status=400)
        _prepare_agent_request(req)
        if not req.use_agent and not (req.prompt or req.request).strip():
            raise CreateAgentError("The prompt is empty.", code="prompt_required", status=400)
    except CreateAgentError as exc:
        return jsonify(_agent_error_payload(exc)), exc.status
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        return jsonify(_create_task_service().start(req.task_id, req.model_dump(mode="json"))), 202
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "task_id_conflict"}), 409


@simple_blueprint.route("/api/simple/tasks/<task_id>", methods=["GET"])
def simple_get_task(task_id: str):
    try:
        return jsonify(_create_task_service().snapshot(task_id, include_request=request.args.get("include_request") == "1"))
    except KeyError:
        return jsonify({"error": "Create task was not found.", "code": "task_not_found"}), 404


@simple_blueprint.route("/api/simple/tasks/<task_id>/cancel", methods=["POST"])
def simple_cancel_task(task_id: str):
    try:
        return jsonify(_create_task_service().cancel(task_id))
    except KeyError:
        return jsonify({"error": "Create task was not found.", "code": "task_not_found"}), 404


MAX_SOURCE_IMAGE_BYTES = 40 * 1024 * 1024
PHOTO_MODES = {"reference", "variation", "edit"}


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


# Formats every browser shows; anything else Pillow reads is converted to PNG.
_BROWSER_IMAGE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}
REMOTE_IMAGE_TIMEOUT_SECONDS = 20


def fetch_remote_image(url: str) -> dict[str, Any]:
    """Download one image from a link for a Create reference.

    The browser cannot read most foreign images (CORS), so the server fetches them:
    only http(s), at most ``MAX_SOURCE_IMAGE_BYTES``, and the bytes must be an image.
    Raises ``ValueError`` with ``args = (message, code, status)``.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Paste a link that starts with http:// or https://.", "url_invalid", 400)
    request_obj = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (ComfyUI Meta Viewer)",
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.9,*/*;q=0.5",
    })
    try:
        with urllib.request.urlopen(request_obj, timeout=REMOTE_IMAGE_TIMEOUT_SECONDS) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_SOURCE_IMAGE_BYTES:
                raise ValueError("The image is larger than 20 MB.", "image_too_large", 413)
            data = response.read(MAX_SOURCE_IMAGE_BYTES + 1)
            final_url = response.geturl() or url
    except urllib.error.HTTPError as exc:
        raise ValueError(f"The site answered {exc.code} for this link.", "fetch_failed", 502) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ValueError(f"Could not download the image: {reason}", "fetch_failed", 502) from exc
    if len(data) > MAX_SOURCE_IMAGE_BYTES:
        raise ValueError("The image is larger than 20 MB.", "image_too_large", 413)
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = str(image.format or "")
            width, height = int(image.width), int(image.height)
            mime = _BROWSER_IMAGE_FORMATS.get(image_format)
            if mime is None:
                converted = io.BytesIO()
                image.convert("RGBA" if "A" in image.getbands() else "RGB").save(converted, format="PNG")
                data, mime = converted.getvalue(), "image/png"
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("The link does not point to an image.", "not_an_image", 415) from exc
    name = Path(urllib.parse.unquote(urllib.parse.urlparse(final_url).path)).name or "image"
    return {
        "data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
        "name": name[:120],
        "width": width,
        "height": height,
        "size": len(data),
        "url": final_url,
    }


@simple_blueprint.route("/api/simple/fetch-image", methods=["POST"])
def simple_fetch_image():
    """Fetch an image from a pasted or dropped link for a reference slot."""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(fetch_remote_image(str(payload.get("url") or "").strip()))
    except ValueError as exc:
        message, code, status = exc.args
        return jsonify({"error": message, "code": code}), status


def _backend_not_ready_response(backend: InferenceBackend):
    return jsonify(backend.not_ready_payload()), 503


def _upload_input_image(client: ComfyUIJobClient, prefix: str, data: bytes) -> str:
    """Upload a photo to ComfyUI input/cmv and return the name a LoadImage node expects."""
    uploaded = client.upload_image(f"{prefix}_{uuid.uuid4().hex}.png", data, subfolder="cmv")
    subfolder = str(uploaded.get("subfolder") or "")
    return f"{subfolder}/{uploaded['name']}" if subfolder else str(uploaded["name"])


def _generate_fixed(profile: ApprovedProfile, req: SimpleGenerateRequest, *, backend=None, on_queued=None, cancelled=None):
    """Queue a fixed-recipe profile run: one ComfyUI prompt per image with consecutive seeds."""
    if cancelled and cancelled():
        return jsonify({"error": "Generation was stopped.", "code": "cancelled"}), 409
    if req.mode not in profile.modes:
        return jsonify({"error": f"Mode '{req.mode}' is not supported by {profile.name}.", "code": "mode_unsupported"}), 400

    image = _decode_image_data_url(req.reference_image) if req.mode in PHOTO_MODES else None
    if req.mode in PHOTO_MODES and image is None:
        return jsonify({"error": "Attach a valid image for this mode.", "code": "image_required"}), 400
    if req.denoise is not None and req.mode != "variation":
        return jsonify({"error": "Denoise is only used in variation mode.", "code": "variation_only"}), 400
    denoise = req.denoise if req.denoise is not None else DEFAULT_VARIATION_DENOISE
    if req.mode == "edit" and not req.prompt.strip():
        return jsonify({"error": "Describe what to change in the photo.", "code": "instruction_required"}), 400
    if not req.prompt.strip():
        return jsonify({"error": "The prompt is empty.", "code": "prompt_required"}), 400
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
    if req.mode == "variation" and image:
        width, height = variation_dimensions(image[1])
    else:
        width, height = resolve_dimensions(profile, req.aspect_ratio, image[1] if image else None)

    backend = backend or _backend()
    # Matches the managed process readiness timeout: a cold start with custom nodes takes a while.
    if not backend.ensure_ready(timeout=120.0):
        return _backend_not_ready_response(backend)
    if cancelled and cancelled():
        return jsonify({"error": "Generation was stopped.", "code": "cancelled"}), 409

    health = backend.profile_health(profile)
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
    positive_prompt = req.prompt.strip()
    user_request = (req.user_request if req.user_request is not None else req.prompt).strip()

    client = backend.client()
    source_name = subject_name = None
    if req.mode in {"edit", "variation"}:
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
            "user_request": user_request,
            "agent": req.agent_name,
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
            "denoise": denoise if req.mode == "variation" else None,
        },
        resource_selections={},
    )

    # One client id per ComfyUI address: ComfyUI reports steps only to the queuing client.
    monitor = backend.monitor(client)
    if monitor is not None and not monitor.ensure_connected(timeout=2.0):
        logger.info("ComfyUI progress socket is not connected; Create shows estimated times only")
    client_id = monitor.client_id if monitor is not None else str(uuid.uuid4())
    runs: list[dict[str, Any]] = []
    for index, seed in enumerate(seeds):
        if cancelled and cancelled():
            break
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
            elif req.mode == "variation":
                workflow = compile_variation_workflow(
                    profile,
                    positive_prompt=positive_prompt,
                    source_image=source_name,
                    seed=seed,
                    denoise=denoise,
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
                        "cmv_user_request": user_request,
                        "cmv_prompt": positive_prompt,
                        **({"cmv_agent": req.agent_name} if req.agent_name else {}),
                        **({"cmv_edit_profile": edit_profile.id} if edit_profile else {}),
                        **({"cmv_negative_prompt": edit_negative} if edit_negative else {}),
                        **({"cmv_denoise": denoise} if req.mode == "variation" else {}),
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
        if monitor is not None:
            monitor.track(prompt_id, workflow)
        run = wf_store.create_run(draft_id=draft.id, prompt_id=prompt_id, client_id=client_id)
        runs.append({"run_id": run.id, "prompt_id": prompt_id, "seed": seed})
        if on_queued:
            on_queued(runs[-1])

    if not runs:
        return jsonify({"error": "Generation was stopped.", "code": "cancelled"}), 409

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
        "denoise": denoise if req.mode == "variation" else None,
        "seconds_per_image": edit_profile.seconds if edit_profile else None,
        "width": width,
        "height": height,
        "positive_prompt": positive_prompt,
        "negative_prompt": edit_negative,
    })


@simple_blueprint.route("/api/simple/agent", methods=["POST"])
def simple_agent():
    """Let the configured agent write the prompt (or edit command) for one Create run."""
    try:
        req = SimpleAgentRequest.model_validate(request.get_json(silent=True) or {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        run_kwargs, finish = _prepare_agent_request(req)
    except CreateAgentError as exc:
        return jsonify(_agent_error_payload(exc)), exc.status
    if request.args.get("stream") == "1":
        return _stream_agent_run(run_kwargs, finish)
    try:
        result = run_create_agent(**run_kwargs)
    except CreateAgentError as exc:
        return jsonify(_agent_error_payload(exc)), exc.status
    return jsonify(finish(result))


def _prepare_agent_request(req: SimpleAgentRequest):
    profile = APPROVED_PROFILES.get(req.profile_id or _default_profile_id())
    if not profile:
        raise CreateAgentError("Create has no model profile.", code="profile_missing", status=500)
    if req.mode not in profile.modes:
        raise CreateAgentError(f"Mode '{req.mode}' is not supported.", code="mode_unsupported", status=400)

    image = subject = None
    if req.mode in PHOTO_MODES:
        image = _decode_image_data_url(req.reference_image)
        if image is None:
            raise CreateAgentError("Attach a valid image for this mode.", code="image_required", status=400)
    if req.mode == "edit" and req.subject_image:
        subject = _decode_image_data_url(req.subject_image)
        if subject is None:
            raise CreateAgentError("The second photo is not a valid image.", code="subject_invalid", status=400)
    if req.mode == "variation" and image:
        dimensions = variation_dimensions(image[1])
    else:
        dimensions = resolve_dimensions(profile, req.aspect_ratio, image[1] if image else None)
    run_kwargs: dict[str, Any] = {
        "profile": profile,
        "store": _ai_profile_store(),
        # The variation prompt is the same faithful description of the photo as in reference mode.
        "mode": "reference" if req.mode == "variation" else req.mode,
        "request": req.request,
        "image": image[0] if image else None,
        "subject": subject[0] if subject else None,
        "previous_prompt": req.previous_prompt,
        "dimensions": dimensions,
        "agent_profile_id": req.agent_profile_id,
        "agent_model": (req.agent_model or "").strip() or None,
        "agent_variant": (req.agent_variant or "").strip() or None,
        "agent_host": req.agent_host,
    }

    def finish(result: CreateAgentResult) -> dict[str, Any]:
        payload = result.to_dict()
        payload.update({"mode": req.mode, "profile_id": profile.id, "width": dimensions[0], "height": dimensions[1]})
        if result.edit_profile:
            chosen = next((item for item in profile.edit_profiles if item.id == result.edit_profile), None)
            if chosen:
                payload["edit_profile_label"] = chosen.label
                payload["seconds_per_image"] = chosen.seconds
        return payload

    return run_kwargs, finish


def _agent_error_payload(exc: CreateAgentError) -> dict[str, Any]:
    return {
        "error": str(exc),
        "code": exc.code,
        "technical_error": (exc.technical_error or "")[:4000] or None,
    }


AGENT_STREAM_KEEPALIVE_SECONDS = 15


def _stream_agent_run(run_kwargs: dict[str, Any], finish) -> Response:
    """Run the agent in a worker and stream its progress as server-sent events.

    Every event is ``data: {json}``: ``progress`` (``phase`` plus an optional ``log``
    line or ``text``), then exactly one ``result`` (the JSON answer of the plain
    request) or ``error`` (with the HTTP ``status`` the plain request would use).
    A client that goes away does not stop the agent; its answer is dropped.
    """
    events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()

    def worker() -> None:
        try:
            result = run_create_agent(**run_kwargs, on_progress=lambda event: events.put(("progress", event)))
            events.put(("result", finish(result)))
        except CreateAgentError as exc:
            events.put(("error", {**_agent_error_payload(exc), "status": exc.status}))
        except Exception as exc:
            logger.exception("Create agent stream failed")
            events.put(("error", {
                "error": "The agent run failed.",
                "code": "agent_failed",
                "status": 500,
                "technical_error": str(exc)[:4000] or None,
            }))

    threading.Thread(target=worker, name="cmv-create-agent", daemon=True).start()

    def generate():
        while True:
            try:
                kind, data = events.get(timeout=AGENT_STREAM_KEEPALIVE_SECONDS)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {json.dumps({'event': kind, **data}, ensure_ascii=False)}\n\n"
            if kind in {"result", "error"}:
                return

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@simple_blueprint.route("/api/simple/generate", methods=["POST"])
def simple_generate():
    try:
        req = SimpleGenerateRequest.model_validate(request.get_json(silent=True) or {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    profile = APPROVED_PROFILES.get(req.profile_id or _default_profile_id())
    if not profile:
        return jsonify({"error": f"Unknown model '{req.profile_id}'"}), 400

    if not profile.workflow_ready:
        return jsonify({
            "error": "Workflow for this model is still being calibrated.",
            "code": "workflow_pending",
        }), 409

    return _generate_fixed(profile, req)


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
    backend = _backend()
    client = backend.client()
    try:
        run = WorkflowExecutionService(store=wf_store, client=client).refresh(run_id)
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
        # Live sampler progress from the ComfyUI WebSocket; null when it was not seen.
        "live": monitor.snapshot(run.prompt_id) if (monitor := backend.monitor(client)) else None,
        # A remote GPU: the runtime starting or models downloading before ComfyUI runs the job.
        "remote": backend.run_details(run.prompt_id),
    })


@simple_blueprint.route("/api/simple/runs/<int:run_id>/cancel", methods=["POST"])
def simple_cancel_run(run_id: int):
    wf_store = _workflow_store()
    try:
        run = WorkflowExecutionService(store=wf_store, client=_backend().client()).cancel(run_id)
        return jsonify({"ok": True, "run": run.model_dump(mode="json")})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
