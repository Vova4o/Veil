from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.config_store import ConfigStore
from app.model_sources.tokens import ModelSourceError, civitai_token, save_civitai_token

from .desktop import (
    DESKTOP_DOWNLOAD_URL,
    FOLDER_SETTINGS,
    desktop_folders,
    desktop_installation_for,
    find_desktop_installations,
    launch_settings,
    state_dir_of,
)
from .detector import detect_comfyui
from .launcher import generate_launcher_script
from .manager import ComfyUIMode, comfy_manager
from .workflow_inventory import invalidate_runtime_inventory

comfyui_blueprint = Blueprint("comfyui", __name__)


def _config_store() -> ConfigStore:
    return current_app.config["CONFIG_STORE"]


def _launch_request(payload: dict) -> dict:
    """Saved settings with the request's overrides, as ComfyUI is launched with them."""
    cfg = _config_store().comfyui_settings()
    merged = {
        **cfg,
        "install_path": payload.get("install_path") or cfg.get("install_path"),
        "host": payload.get("host") or cfg.get("host") or "127.0.0.1",
        "port": int(payload.get("port") or cfg.get("port") or 8188),
    }
    for key in ("extra_args", "custom_python"):
        if key in payload:
            merged[key] = payload[key]
    return launch_settings(merged, state_dir=state_dir_of(_config_store()))


def _public_settings(store: ConfigStore) -> dict:
    """Saved settings without the token, naming the ComfyUI they resolve to."""
    settings = store.comfyui_settings()
    launch = launch_settings(settings, state_dir=state_dir_of(store))
    settings.pop("civitai_api_token", None)
    settings["has_civitai_token"] = bool(civitai_token(store))
    settings["install_path"] = launch.get("install_path")
    settings["desktop_installation_id"] = launch.get("desktop_installation_id")
    return settings


@comfyui_blueprint.route("/api/comfyui/config", methods=["GET", "POST"])
def comfyui_config():
    store = _config_store()
    if request.method == "GET":
        return jsonify(_public_settings(store))

    payload = request.get_json(silent=True) or {}
    # CMV's own folders; an empty value follows Desktop again.
    folders: dict[str, str] = {}
    for key in FOLDER_SETTINGS:
        if key not in payload:
            continue
        value = str(payload.get(key) or "").strip()
        if value:
            if not Path(value).is_absolute():
                return jsonify({"error": f"Choose a full folder path: {value}", "code": "folder_invalid", "field": key}), 400
            try:
                Path(value).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return jsonify({"error": f"Cannot use folder {value}: {exc}", "code": "folder_invalid", "field": key}), 400
        folders[key] = value

    install_path, custom_python = payload.get("install_path"), payload.get("custom_python")
    installation_id = payload.get("desktop_installation_id")
    if installation_id:
        installation = next((item for item in find_desktop_installations() if item.id == installation_id), None)
        if installation is None:
            return jsonify({"error": "This ComfyUI Desktop installation was not found.", "code": "installation_not_found"}), 400
        # Kept in step for code that reads the folder directly.
        install_path, custom_python = str(installation.comfy_dir), ""

    # An empty token field keeps the saved token; Integrations › Model sources removes it.
    token = str(payload.get("civitai_api_token") or "").strip()
    if token:
        try:
            save_civitai_token(store, token)
        except ModelSourceError as exc:
            return jsonify({"error": str(exc), "code": exc.code}), 502
    store.update_comfyui_settings(
        install_path=install_path,
        custom_python=custom_python,
        host=payload.get("host"),
        port=payload.get("port"),
        extra_args=payload.get("extra_args"),
        auto_start=payload.get("auto_start"),
        desktop_installation_id=installation_id,
        **folders,
    )
    invalidate_runtime_inventory()
    return jsonify(_public_settings(store))


@comfyui_blueprint.route("/api/comfyui/detect", methods=["POST"])
def comfyui_detect():
    payload = request.get_json(silent=True) or {}
    path = payload.get("path", "")
    custom_python = payload.get("custom_python")
    result = detect_comfyui(path, custom_python=custom_python).to_dict()
    desktop = desktop_installation_for(path)
    result["desktop"] = desktop.to_dict() if desktop else None
    return jsonify(result)


@comfyui_blueprint.route("/api/comfyui/installations", methods=["GET"])
def comfyui_installations():
    return jsonify({
        "installations": [item.to_dict() for item in find_desktop_installations()],
        "folders": desktop_folders(),
        "download_url": DESKTOP_DOWNLOAD_URL,
    })


@comfyui_blueprint.route("/api/comfyui/status", methods=["GET"])
def comfyui_status():
    store = _config_store()
    cfg = store.comfyui_settings()
    host = cfg.get("host") or "127.0.0.1"
    port = int(cfg.get("port") or 8188)

    # Check external or update status
    status_data = comfy_manager.check_external_or_status(host=host, port=port)
    info = comfy_manager.get_info()
    info.update(status_data)
    return jsonify(info)


@comfyui_blueprint.route("/api/comfyui/start", methods=["POST"])
def comfyui_start():
    launch = _launch_request(request.get_json(silent=True) or {})
    install_path, host, port = launch["install_path"], launch["host"], launch["port"]
    if not install_path:
        return jsonify({"error": "No installation path specified", "code": "missing_path"}), 400

    # A second ComfyUI on a port that already answers, or one still starting, would only fail to bind.
    status_data = comfy_manager.check_external_or_status(host=host, port=port)
    info = comfy_manager.get_info()
    if status_data.get("online") or (info["mode"] == ComfyUIMode.MANAGED.value and info["status"] == "starting"):
        info.update(status_data)
        return jsonify(info)

    try:
        info = comfy_manager.start_managed(
            install_path=install_path,
            host=host,
            port=port,
            extra_args=launch.get("extra_args"),
            custom_python=launch.get("custom_python"),
        )
        invalidate_runtime_inventory()
        return jsonify(info)
    except Exception as exc:
        return jsonify({"error": str(exc), "code": "start_failed"}), 422


@comfyui_blueprint.route("/api/comfyui/stop", methods=["POST"])
def comfyui_stop():
    if comfy_manager.mode == ComfyUIMode.EXTERNAL:
        return jsonify({"error": "Cannot stop external ComfyUI process", "code": "external_process"}), 400

    comfy_manager.stop_managed()
    invalidate_runtime_inventory()
    return jsonify(comfy_manager.get_info())


@comfyui_blueprint.route("/api/comfyui/restart", methods=["POST"])
def comfyui_restart():
    launch = _launch_request(request.get_json(silent=True) or {})

    try:
        info = comfy_manager.restart_managed(
            install_path=launch["install_path"],
            host=launch["host"],
            port=launch["port"],
            extra_args=launch.get("extra_args"),
            custom_python=launch.get("custom_python"),
        )
        invalidate_runtime_inventory()
        return jsonify(info)
    except Exception as exc:
        return jsonify({"error": str(exc), "code": "restart_failed"}), 422


@comfyui_blueprint.route("/api/comfyui/interrupt", methods=["POST"])
def comfyui_interrupt():
    try:
        ok = comfy_manager.interrupt_generation()
        return jsonify({"success": ok})
    except Exception as exc:
        return jsonify({"error": str(exc), "code": "interrupt_failed"}), 422


@comfyui_blueprint.route("/api/comfyui/logs", methods=["GET"])
def comfyui_logs():
    lines_param = request.args.get("lines", "200")
    try:
        lines = int(lines_param)
    except ValueError:
        lines = 200
    return jsonify({"logs": comfy_manager.get_logs(lines)})


@comfyui_blueprint.route("/api/comfyui/launcher", methods=["POST"])
def comfyui_launcher():
    launch = _launch_request(request.get_json(silent=True) or {})
    install_path = launch["install_path"]
    if not install_path:
        return jsonify({"error": "No installation path specified", "code": "missing_path"}), 400

    detection = detect_comfyui(install_path, custom_python=launch.get("custom_python"))
    if not detection.is_valid:
        return jsonify({"error": detection.error, "code": "invalid_installation"}), 422

    try:
        script_path = generate_launcher_script(
            detection=detection,
            extra_args=launch.get("extra_args"),
            host=launch["host"],
            port=launch["port"],
        )
        return jsonify({
            "script_path": str(script_path),
            "script_content": script_path.read_text(encoding="utf-8"),
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "code": "launcher_failed"}), 422
