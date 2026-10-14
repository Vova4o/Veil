"""Model folders of a ComfyUI installation, resolved the way ComfyUI resolves them.

ComfyUI searches ``<models>/<folder>`` plus every folder listed in
``extra_model_paths.yaml`` next to main.py and in each
``--extra-model-paths-config`` file. ComfyUI Desktop keeps all models in such a
shared folder, so looking only at ``<ComfyUI>/models`` reports installed models
as missing whenever ComfyUI is not running.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .desktop import launch_settings
from .detector import detect_comfyui
from .validation import validate_extra_args

logger = logging.getLogger(__name__)

# ComfyUI's folder_paths.map_legacy: old folder names listed under their current names.
LEGACY_FOLDERS = {"unet": "diffusion_models", "clip": "text_encoders"}
# Default folders ComfyUI searches under more than one directory name.
_DEFAULT_SUBDIRS = {
    "text_encoders": ("text_encoders", "clip"),
    "diffusion_models": ("unet", "diffusion_models"),
    "controlnet": ("controlnet", "t2i_adapter"),
}
_QUOTED_KEY = re.compile(r"""^(?P<quote>['"])(?P<key>.*?)(?P=quote)\s*:(?:\s+(?P<value>.*)|$)""")
_PLAIN_KEY = re.compile(r"^(?P<key>[^:]+?)\s*:(?:\s+(?P<value>.*)|$)")
_BLOCK_SCALARS = {"|", "|-", "|+", ">", ">-", ">+"}


class ModelFolders:
    """Search directories per model folder, in ComfyUI's priority order."""

    def __init__(
        self,
        models_dir: Path,
        entries: Iterable[tuple[str, Path, bool]] = (),
        *,
        download_root: Path | None = None,
    ) -> None:
        self.models_dir = models_dir
        # A models folder chosen in MediaStudio receives every download.
        self.download_root = download_root
        self._paths: dict[str, list[Path]] = {}
        self._preferred: dict[str, Path] = {}
        # Mirrors folder_paths.add_model_folder_path: a default entry moves to the front.
        for name, path, is_default in entries:
            folder = LEGACY_FOLDERS.get(name, name)
            paths = self._paths.setdefault(folder, self._defaults(folder))
            if path in paths:
                if is_default:
                    paths.remove(path)
                    paths.insert(0, path)
            elif is_default:
                paths.insert(0, path)
            else:
                paths.append(path)
            if is_default:
                self._preferred[name] = path

    def _defaults(self, folder: str) -> list[Path]:
        return [self.models_dir / name for name in _DEFAULT_SUBDIRS.get(folder, (folder,))]

    def search_paths(self, folder: str) -> list[Path]:
        folder = LEGACY_FOLDERS.get(folder, folder)
        return list(self._paths.get(folder) or self._defaults(folder))

    def download_dir(self, folder: str) -> Path:
        """Where a new file of this folder belongs: MediaStudio's models folder, the config's default, else ``<models>``."""
        if self.download_root is not None:
            return self.download_root / folder
        aliases = [alias for alias, current in LEGACY_FOLDERS.items() if current == folder]
        for name in (folder, *aliases):
            if name in self._preferred:
                return self._preferred[name]
        return self.models_dir / folder


def resolve_model_folders(settings: Mapping[str, Any], *, state_dir: Path | None = None) -> ModelFolders | None:
    """Model folders of the configured installation, or None when no ComfyUI folder is set."""
    settings = launch_settings(settings, state_dir=state_dir)
    install_path = str(settings.get("install_path") or "").strip()
    if not install_path:
        return None
    detection = detect_comfyui(install_path, custom_python=settings.get("custom_python"))
    if detection.comfy_dir is None:
        return None
    folders = model_folders(Path(detection.comfy_dir), settings.get("extra_args"))
    download_root = settings.get("models_download_dir")
    folders.download_root = Path(download_root) if download_root else None
    return folders


def model_folders(comfy_dir: Path, extra_args: str | Sequence[str] | None = None) -> ModelFolders:
    try:
        tokens = validate_extra_args(extra_args)
    except ValueError:
        tokens = []
    # A managed ComfyUI runs with main.py's folder as its working directory.
    base = _option_values(tokens, "--base-directory")
    base_dir = _absolute(base[-1], comfy_dir) if base else comfy_dir
    models = _option_values(tokens, "--models-directory")
    models_dir = _absolute(models[-1], comfy_dir) if models else base_dir / "models"

    configs = [comfy_dir / "extra_model_paths.yaml"]
    configs += [_absolute(value, comfy_dir) for value in _option_values(tokens, "--extra-model-paths-config", many=True)]
    entries: list[tuple[str, Path, bool]] = []
    for config in configs:
        if config.is_file():
            entries += _config_entries(config)
    return ModelFolders(models_dir, entries)


def _absolute(value: str, cwd: Path) -> Path:
    path = Path(value)
    return Path(os.path.normpath(path if path.is_absolute() else cwd / path))


def _option_values(tokens: list[str], name: str, *, many: bool = False) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith(name + "="):
            values.append(token.split("=", 1)[1])
        elif token == name:
            while index < len(tokens) and not tokens[index].startswith("-"):
                values.append(tokens[index])
                index += 1
                if not many:
                    break
    return values


def _config_entries(config: Path) -> list[tuple[str, Path, bool]]:
    """(folder, directory, is_default) entries of one config, as utils/extra_config.py reads them."""
    try:
        sections = parse_model_paths_yaml(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Model paths config %s is unreadable: %s", config, exc)
        return []
    config_dir = config.resolve().parent
    entries: list[tuple[str, Path, bool]] = []
    for section in sections.values():
        if not section:
            continue
        section = dict(section)
        base_path = section.pop("base_path", None)
        if isinstance(base_path, str) and base_path:
            base_path = os.path.expandvars(os.path.expanduser(base_path))
            if not os.path.isabs(base_path):
                base_path = os.path.join(config_dir, base_path)
        else:
            base_path = None
        is_default = section.pop("is_default", False) is True
        for folder, value in section.items():
            if not isinstance(value, str):
                continue
            for line in value.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if base_path:
                    full = os.path.join(base_path, line)
                else:
                    full = line if os.path.isabs(line) else os.path.join(config_dir, line)
                entries.append((folder, Path(os.path.normpath(full)), is_default))
    return entries


def parse_model_paths_yaml(text: str) -> dict[str, dict[str, Any]]:
    """Read the two-level mapping of an extra_model_paths.yaml.

    Covers what these files use: quoted or plain keys and scalars, booleans and
    ``|`` / ``>`` block scalars holding one path per line. Anything deeper is ignored.
    """
    sections: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    lines = text.lstrip("﻿").splitlines()
    index = 0
    while index < len(lines):
        content = _strip_comment(lines[index]).rstrip()
        index += 1
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        match = _QUOTED_KEY.match(content.strip()) or _PLAIN_KEY.match(content.strip())
        if not match:
            continue
        key, value = match.group("key").strip(), (match.group("value") or "").strip()
        if indent == 0:
            current = sections.setdefault(key, {})
            continue
        if current is None:
            continue
        if value in _BLOCK_SCALARS:
            block: list[str] = []
            while index < len(lines):
                line = lines[index]
                if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
                    break
                block.append(line.strip())
                index += 1
            parts = [line for line in block if line]
            current[key] = ("\n" if value.startswith("|") else " ").join(parts)
        else:
            current[key] = _scalar(value)
    return sections


def _strip_comment(line: str) -> str:
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _scalar(value: str) -> Any:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"", "~", "null"}:
        return None
    return value
