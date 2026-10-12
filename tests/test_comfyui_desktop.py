from __future__ import annotations

import json

import pytest

from app.comfyui import desktop, routes
from app.comfyui.desktop import (
    CMV_MODEL_PATHS_FILE,
    desktop_installation_for,
    find_desktop_installations,
    launch_settings,
    selected_installation,
)
from app.comfyui.model_paths import parse_model_paths_yaml, resolve_model_folders
from app.comfyui.validation import validate_extra_args
from app.config_store import ConfigStore
from app.main import app


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


@pytest.fixture
def desktop_env(tmp_path, monkeypatch):
    """A Comfy Desktop app data folder with two local installations, a cloud entry and a broken one."""
    data = tmp_path / "Comfy Desktop"
    shared = tmp_path / "Shared"
    installs = tmp_path / "Installs"
    for name in ("Main", "Second"):
        (installs / name / "ComfyUI").mkdir(parents=True)
        (installs / name / "ComfyUI" / "main.py").write_text("", encoding="utf-8")
    _write(data / "installations.json", [
        {"id": "inst-2", "name": "Second", "installPath": str(installs / "Second"), "status": "installed",
         "launchArgs": "--lowvram --port 8000", "lastLaunchedAt": 200, "comfyVersionTag": "v0.38.0"},
        {"id": "inst-1", "name": "Main", "installPath": str(installs / "Main"), "status": "installed",
         "launchArgs": "--enable-manager --listen 0.0.0.0", "lastLaunchedAt": 100,
         "comfyVersion": {"baseTag": "v0.39.2"}},
        {"id": "cloud", "name": "Comfy Cloud", "sourceId": "cloud", "remoteUrl": "https://cloud.comfy.org/",
         "status": "installed"},
        {"id": "inst-3", "name": "Gone", "installPath": str(installs / "Gone"), "status": "installed"},
        {"id": "inst-4", "name": "Half", "installPath": str(installs / "Main"), "status": "installing"},
    ])
    _write(data / "settings.json", {
        "modelsDirs": [str(shared / "models")],
        "inputDir": str(shared / "input"),
        "outputDir": str(shared / "output"),
    })
    _write(data / "last-session.json", {"kind": "instance", "installationId": "inst-1"})
    _write(data / "instance-model-paths" / "inst-1.yaml",
           f"comfy.desktop_0:\n  base_path: '{shared / 'models'}'\n  is_default: true\n  'loras': 'loras/'\n")
    monkeypatch.setattr(desktop, "desktop_data_dir", lambda: data)
    return {"data": data, "shared": shared, "installs": installs}


def test_installations_skip_cloud_and_broken_entries_and_lead_with_last_used(desktop_env):
    found = find_desktop_installations()

    assert [item.id for item in found] == ["inst-1", "inst-2"]
    main, second = found
    assert main.last_used is True
    assert main.comfy_dir == desktop_env["installs"] / "Main" / "ComfyUI"
    assert main.version == "v0.39.2"
    assert second.version == "v0.38.0"
    # CMV owns the listen address; the rest of Desktop's launch arguments stay.
    assert main.launch_args == ("--enable-manager",)
    assert second.launch_args == ("--lowvram",)
    assert second.model_paths_config is None
    assert main.to_dict()["install_path"] == str(main.comfy_dir)


def test_installation_matches_its_root_or_comfyui_folder(desktop_env):
    main = desktop_env["installs"] / "Main"
    assert desktop_installation_for(main).id == "inst-1"
    assert desktop_installation_for(main / "ComfyUI").id == "inst-1"
    assert desktop_installation_for(desktop_env["installs"]) is None
    assert desktop_installation_for("") is None


def test_launch_settings_use_desktops_current_arguments(desktop_env):
    data, shared = desktop_env["data"], desktop_env["shared"]
    stale = (data / "instance-model-paths" / "inst-old.yaml").as_posix()
    settings = {
        "install_path": str(desktop_env["installs"] / "Main" / "ComfyUI"),
        "extra_args": (
            f"--extra-model-paths-config '{stale}' --highvram --output-directory D:/old/output"
            " --extra-model-paths-config=x.yaml --input-directory=D:/old/input"
        ),
    }

    tokens = launch_settings(settings)["extra_args"]

    assert tokens == [
        "--enable-manager",
        "--extra-model-paths-config", str(data / "instance-model-paths" / "inst-1.yaml"),
        "--input-directory", str(shared / "input"),
        "--output-directory", str(shared / "output"),
        # The user's own arguments follow, without saved copies of Desktop's folders and instance configs.
        "--highvram",
        "--extra-model-paths-config=x.yaml",
    ]


def test_launch_settings_leave_other_installs_and_unsafe_args_alone(desktop_env, tmp_path):
    portable = {"install_path": str(tmp_path / "portable"), "extra_args": "--lowvram"}
    assert launch_settings(portable) == portable
    unsafe = {"install_path": str(desktop_env["installs"] / "Main"), "extra_args": "--lowvram & calc"}
    assert launch_settings(unsafe)["extra_args"] == "--lowvram & calc"


def test_desktop_models_resolve_from_the_install_path_alone(desktop_env):
    folders = resolve_model_folders({"install_path": str(desktop_env["installs"] / "Main" / "ComfyUI")})
    assert folders.search_paths("loras")[0] == desktop_env["shared"] / "models" / "loras"
    assert folders.download_dir("loras") == desktop_env["shared"] / "models" / "loras"


def test_extra_args_accept_non_latin_paths_but_not_shell_symbols():
    assert validate_extra_args("--input-directory C:/Users/Олег/input") == ["--input-directory", "C:/Users/Олег/input"]
    with pytest.raises(ValueError):
        validate_extra_args("--lowvram & calc")


def test_installation_routes_report_desktop(desktop_env):
    client = app.test_client()
    listed = client.get("/api/comfyui/installations").get_json()["installations"]
    assert [item["id"] for item in listed] == ["inst-1", "inst-2"]

    detected = client.post("/api/comfyui/detect", json={"path": str(desktop_env["installs"] / "Main")}).get_json()
    assert detected["desktop"]["id"] == "inst-1"
    assert detected["comfy_dir"] == str(desktop_env["installs"] / "Main" / "ComfyUI")


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "cmv" / "config.json")
    monkeypatch.setitem(app.config, "CONFIG_STORE", store)
    return store


def test_selected_installation_prefers_choice_then_last_used(desktop_env, tmp_path):
    main = desktop_env["installs"] / "Main"
    assert selected_installation({}).id == "inst-1"
    assert selected_installation({"desktop_installation_id": "inst-2"}).id == "inst-2"
    # A chosen installation that Desktop no longer has falls back to the last used one.
    assert selected_installation({"desktop_installation_id": "inst-9"}).id == "inst-1"
    # Configurations saved before the choice existed still name the folder.
    assert selected_installation({"install_path": str(desktop_env["installs"] / "Second")}).id == "inst-2"
    assert selected_installation({"install_path": str(tmp_path / "elsewhere")}) is None
    resolved = launch_settings({"custom_python": "C:/old/python.exe"})
    assert resolved["install_path"] == str(main / "ComfyUI")
    assert resolved["custom_python"] is None
    assert resolved["desktop_installation_id"] == "inst-1"


def test_cmv_folders_apply_over_desktop(desktop_env, tmp_path):
    own_models, own_input = tmp_path / "My Models", tmp_path / "in"
    state = tmp_path / "cmv"
    resolved = launch_settings(
        {"models_dir": str(own_models), "input_dir": str(own_input), "extra_args": "--input-directory D:/x"},
        state_dir=state,
    )

    config = state / CMV_MODEL_PATHS_FILE
    assert resolved["extra_args"] == [
        "--enable-manager",
        "--extra-model-paths-config", str(desktop_env["data"] / "instance-model-paths" / "inst-1.yaml"),
        # Listed after Desktop's config, so its default folders lead every search list.
        "--extra-model-paths-config", str(config),
        "--input-directory", str(own_input),
        "--output-directory", str(desktop_env["shared"] / "output"),
    ]
    assert resolved["models_download_dir"] == str(own_models)
    section = parse_model_paths_yaml(config.read_text(encoding="utf-8"))["comfy_meta_viewer"]
    assert section["base_path"] == str(own_models)
    assert section["is_default"] is True
    assert section["diffusion_models"] == "diffusion_models/"

    folders = resolve_model_folders({"models_dir": str(own_models)}, state_dir=state)
    assert folders.search_paths("loras")[:2] == [own_models / "loras", desktop_env["shared"] / "models" / "loras"]
    assert folders.download_dir("loras") == own_models / "loras"
    assert folders.download_dir("unet_gguf") == own_models / "unet_gguf"


def test_config_route_saves_installation_and_folders(desktop_env, store, tmp_path):
    client = app.test_client()
    store.update_comfyui_settings(civitai_api_token="secret", custom_python="C:/old/python.exe")

    shown = client.get("/api/comfyui/config").get_json()
    assert shown["install_path"] == str(desktop_env["installs"] / "Main" / "ComfyUI")
    assert shown["desktop_installation_id"] == "inst-1"
    assert "civitai_api_token" not in shown and shown["has_civitai_token"] is True

    output = tmp_path / "new" / "output"
    saved = client.post("/api/comfyui/config", json={
        "desktop_installation_id": "inst-2", "output_dir": str(output), "models_dir": "",
    }).get_json()
    assert saved["desktop_installation_id"] == "inst-2"
    assert saved["output_dir"] == str(output) and output.is_dir()
    assert saved["models_dir"] is None
    raw = store.comfyui_settings()
    assert raw["install_path"] == str(desktop_env["installs"] / "Second" / "ComfyUI")
    assert raw["custom_python"] is None

    # A folder override can be cleared to follow Desktop again.
    cleared = client.post("/api/comfyui/config", json={"output_dir": None}).get_json()
    assert cleared["output_dir"] is None

    bad = client.post("/api/comfyui/config", json={"input_dir": "relative/input"})
    assert bad.status_code == 400 and bad.get_json()["field"] == "input_dir"
    missing = client.post("/api/comfyui/config", json={"desktop_installation_id": "inst-9"})
    assert missing.status_code == 400 and missing.get_json()["code"] == "installation_not_found"


def test_installations_route_lists_desktop_folders(desktop_env):
    data = app.test_client().get("/api/comfyui/installations").get_json()
    assert data["folders"] == {
        "models_dir": str(desktop_env["shared"] / "models"),
        "input_dir": str(desktop_env["shared"] / "input"),
        "output_dir": str(desktop_env["shared"] / "output"),
    }
    assert data["download_url"].startswith("https://")


def test_start_route_launches_desktop_with_its_arguments(desktop_env, store, monkeypatch):
    started = []
    monkeypatch.setattr(
        routes.comfy_manager, "check_external_or_status", lambda host, port: {"online": False, "status": "stopped"}
    )
    monkeypatch.setattr(routes.comfy_manager, "start_managed", lambda **kwargs: started.append(kwargs) or {})

    resp = app.test_client().post("/api/comfyui/start", json={
        "install_path": str(desktop_env["installs"] / "Main"), "extra_args": "", "port": 8190,
    })

    assert resp.status_code == 200
    assert started[0]["port"] == 8190
    assert started[0]["extra_args"][:3] == [
        "--enable-manager", "--extra-model-paths-config",
        str(desktop_env["data"] / "instance-model-paths" / "inst-1.yaml"),
    ]
