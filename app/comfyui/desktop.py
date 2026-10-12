"""ComfyUI Desktop installations, read from the Desktop app's own settings.

CMV launches ComfyUI only from a ComfyUI Desktop installation on Windows.
Desktop keeps its state in ``<app data>/Comfy Desktop``: ``installations.json``
lists the installations, ``settings.json`` the shared models, input and output
folders, and ``instance-model-paths/<id>.yaml`` the model folders of each
installation. It launches a local installation as::

    <install>/ComfyUI/.venv/Scripts/python.exe -s ComfyUI/main.py <launchArgs>
        --extra-model-paths-config <app data>/instance-model-paths/<id>.yaml
        --input-directory <inputDir> --output-directory <outputDir>

CMV reads these settings each time it launches or inspects ComfyUI instead of
saving copies of them, so a regenerated instance file or a moved shared folder
never leaves CMV pointing at stale paths. CMV's own models, input and output
folders, when set, apply over Desktop's to the ComfyUI that CMV launches;
Desktop's files are never written.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .validation import validate_extra_args

logger = logging.getLogger(__name__)

DESKTOP_APP_DIR = "Comfy Desktop"
DESKTOP_DOWNLOAD_URL = "https://www.comfy.org/download"
# CMV's models folder as a model paths config, rewritten from its settings.
CMV_MODEL_PATHS_FILE = "comfyui-model-paths.yaml"
# ComfyUI's standard model folders (comfy/folder_paths.py).
MODEL_FOLDERS = (
    "checkpoints", "configs", "loras", "vae", "text_encoders", "diffusion_models", "clip_vision",
    "style_models", "embeddings", "diffusers", "vae_approx", "controlnet", "gligen", "upscale_models",
    "latent_upscale_models", "hypernetworks", "photomaker", "classifiers", "model_patches", "audio_encoders",
)
FOLDER_SETTINGS = ("models_dir", "input_dir", "output_dir")
# CMV decides the address ComfyUI listens on.
_ADDRESS_OPTIONS = {"--listen", "--port"}
# Folders set by Desktop or by CMV's own folder settings.
_FOLDER_OPTIONS = {"--input-directory", "--output-directory"}
_MODEL_PATHS_OPTION = "--extra-model-paths-config"


@dataclass(frozen=True)
class DesktopInstallation:
    id: str
    name: str
    root: Path
    comfy_dir: Path
    version: str | None
    launch_args: tuple[str, ...]
    model_paths_config: Path | None
    last_used: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "desktop",
            "id": self.id,
            "name": self.name,
            "install_path": str(self.comfy_dir),
            "version": self.version,
            "last_used": self.last_used,
        }


def desktop_data_dir() -> Path | None:
    """Electron's per-user app data folder of Comfy Desktop."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / DESKTOP_APP_DIR if base else None


def desktop_folders(data_dir: Path | None = None) -> dict[str, str | None]:
    """Desktop's shared models, input and output folders (the first models folder is its default)."""
    data_dir = data_dir or desktop_data_dir()
    settings = _read_json(data_dir / "settings.json") if data_dir else None
    settings = settings if isinstance(settings, dict) else {}
    models = settings.get("modelsDirs")
    first_models = models[0] if isinstance(models, list) and models else None
    return {
        "models_dir": _text(first_models),
        "input_dir": _text(settings.get("inputDir")),
        "output_dir": _text(settings.get("outputDir")),
    }


def find_desktop_installations(data_dir: Path | None = None) -> list[DesktopInstallation]:
    """Local Desktop installations with a ComfyUI checkout, the last used one first."""
    data_dir = data_dir or desktop_data_dir()
    if data_dir is None:
        return []
    records = _read_json(data_dir / "installations.json")
    if not isinstance(records, list):
        return []
    session = _read_json(data_dir / "last-session.json")
    last_id = session.get("installationId") if isinstance(session, dict) else None

    found: list[tuple[float, DesktopInstallation]] = []
    for record in records:
        # Cloud entries carry a remoteUrl and nothing to launch.
        if not isinstance(record, dict) or record.get("status") != "installed" or record.get("remoteUrl"):
            continue
        install_id, install_path = record.get("id"), record.get("installPath")
        if not isinstance(install_id, str) or not isinstance(install_path, str) or not install_path:
            continue
        root = Path(install_path)
        comfy_dir = root / "ComfyUI"
        if not (comfy_dir / "main.py").is_file():
            continue
        config = data_dir / "instance-model-paths" / f"{install_id}.yaml"
        comfy_version = record.get("comfyVersion")
        version = comfy_version.get("baseTag") if isinstance(comfy_version, dict) else None
        installation = DesktopInstallation(
            id=install_id,
            name=str(record.get("name") or "ComfyUI"),
            root=root,
            comfy_dir=comfy_dir,
            version=version or record.get("comfyVersionTag") or record.get("version"),
            launch_args=tuple(_without_options(_split_args(record.get("launchArgs")), _ADDRESS_OPTIONS)),
            model_paths_config=config if config.is_file() else None,
            last_used=install_id == last_id,
        )
        last_launch = record.get("lastLaunchedAt")
        found.append((float(last_launch) if isinstance(last_launch, (int, float)) else 0.0, installation))
    found.sort(key=lambda item: (not item[1].last_used, -item[0]))
    return [installation for _, installation in found]


def desktop_installation_for(path: str | Path | None, data_dir: Path | None = None) -> DesktopInstallation | None:
    """The Desktop installation whose root or ComfyUI folder is ``path``."""
    if not path or not str(path).strip():
        return None
    target = _same(path)
    for installation in find_desktop_installations(data_dir):
        if target in (_same(installation.root), _same(installation.comfy_dir)):
            return installation
    return None


def selected_installation(settings: Mapping[str, Any], data_dir: Path | None = None) -> DesktopInstallation | None:
    """The chosen Desktop installation, else the one Desktop used last.

    An older configuration that points at a ComfyUI outside Desktop keeps it.
    A chosen installation that no longer exists falls back to the last used one.
    """
    installations = find_desktop_installations(data_dir)
    if not installations:
        return None
    wanted = settings.get("desktop_installation_id")
    chosen = next((item for item in installations if item.id == wanted), None) if wanted else None
    if chosen is not None:
        return chosen
    install_path = settings.get("install_path")
    if install_path and not wanted:
        target = _same(install_path)
        return next(
            (item for item in installations if target in (_same(item.root), _same(item.comfy_dir))), None
        )
    return installations[0]


def launch_settings(
    settings: Mapping[str, Any],
    *,
    state_dir: Path | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """ComfyUI settings as CMV launches them.

    For a Desktop installation the result names its ComfyUI folder and lists,
    in order: Desktop's launch arguments and model paths config, CMV's models
    folder config (so that folder is searched first), the input and output
    folders (CMV's own, else Desktop's), then the user's own extra arguments.
    Saved copies of Desktop's folders and instance configs are dropped from the
    user's arguments: ComfyUI refuses to start when such a file no longer exists.
    ``models_download_dir`` names CMV's models folder when one is set.
    """
    resolved = dict(settings)
    data_dir = data_dir or desktop_data_dir()
    installation = selected_installation(settings, data_dir) if data_dir else None
    if installation is None or data_dir is None:
        return resolved
    resolved.update(
        install_path=str(installation.comfy_dir),
        # Desktop's own environment sits next to its ComfyUI folder.
        custom_python=None,
        desktop_installation_id=installation.id,
    )
    try:
        own = validate_extra_args(settings.get("extra_args"))
    except ValueError:
        # Unsafe user arguments must still fail at launch, not vanish here.
        return resolved

    defaults = desktop_folders(data_dir)
    folders = {key: _text(settings.get(key)) or defaults[key] for key in FOLDER_SETTINGS}
    tokens = list(installation.launch_args)
    if installation.model_paths_config is not None:
        tokens += [_MODEL_PATHS_OPTION, str(installation.model_paths_config)]
    models_dir = _text(settings.get("models_dir"))
    if models_dir:
        tokens += [_MODEL_PATHS_OPTION, str(write_models_config(Path(models_dir), state_dir))]
    if folders["input_dir"]:
        tokens += ["--input-directory", folders["input_dir"]]
    if folders["output_dir"]:
        tokens += ["--output-directory", folders["output_dir"]]

    instance_configs = _same(data_dir / "instance-model-paths")
    own = _without_options(own, _FOLDER_OPTIONS)
    own = _without_values(own, _MODEL_PATHS_OPTION, lambda value: os.path.dirname(_same(value)) == instance_configs)
    resolved["extra_args"] = tokens + own
    resolved["models_download_dir"] = models_dir
    return resolved


def write_models_config(models_dir: Path, state_dir: Path | None = None) -> Path:
    """A model paths config that makes ``models_dir`` ComfyUI's default models folder.

    Listed after Desktop's config, its default entries move to the front of
    every folder's search list. Rewritten only when its content changes.
    """
    path = (state_dir or Path(tempfile.gettempdir()) / "comfy-meta-viewer") / CMV_MODEL_PATHS_FILE
    base = "'" + str(models_dir).replace("'", "''") + "'"
    content = (
        "# Written by Comfy Meta Viewer from its ComfyUI settings; rewritten before each launch.\n"
        f"comfy_meta_viewer:\n  base_path: {base}\n  is_default: true\n"
        + "".join(f"  {folder}: {folder}/\n" for folder in MODEL_FOLDERS)
    )
    try:
        if path.read_text(encoding="utf-8") == content:
            return path
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def state_dir_of(store: Any) -> Path | None:
    """The folder of CMV's configuration file, where CMV keeps files it writes for ComfyUI."""
    path = getattr(store, "path", None)
    return Path(path).parent if path else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if path.exists():
            logger.debug("Comfy Desktop file %s is unreadable: %s", path, exc)
        return None


def _same(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _split_args(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        # Desktop stores Windows-style text; POSIX splitting would eat backslashes.
        return [token.strip('"') for token in shlex.split(value, posix=False)]
    except ValueError:
        return []


def _without_options(tokens: Sequence[str], names: set[str]) -> list[str]:
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.split("=", 1)[0] in names:
            if "=" not in token and index < len(tokens) and not tokens[index].startswith("-"):
                index += 1
            continue
        kept.append(token)
    return kept


def _without_values(tokens: Sequence[str], name: str, drop: Callable[[str], bool]) -> list[str]:
    """Remove values of a multi-value option that match ``drop``, and the option if none remain."""
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith(name + "="):
            if not drop(token.split("=", 1)[1]):
                kept.append(token)
            continue
        if token != name:
            kept.append(token)
            continue
        values: list[str] = []
        while index < len(tokens) and not tokens[index].startswith("-"):
            if not drop(tokens[index]):
                values.append(tokens[index])
            index += 1
        if values:
            kept += [name, *values]
    return kept
