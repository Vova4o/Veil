from __future__ import annotations

import copy
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ai.prompting import PromptFamily

SIMPLE_MODELS_DIR = Path(__file__).parent / "simple_models"


@dataclass(frozen=True)
class ProfileResourceDependency:
    resource_type: str
    folder: str
    filename: str
    display_name: str
    download_url: str | None = None
    aliases: tuple[str, ...] = ()
    sha256: str | None = None


@dataclass(frozen=True)
class CustomNodeRequirement:
    """A ComfyUI custom node pack pinned to one reviewed commit."""

    id: str
    display_name: str
    folder: str
    repository: str
    commit: str
    archive_url: str
    verify_file: str
    verify_sha256: str
    node_classes: tuple[str, ...]
    modes: tuple[str, ...] = ()


# Edit-graph knobs a manifest edit profile may set, with the type the node input expects.
EDIT_SETTING_TYPES: dict[str, type] = {
    "cfg": float,
    "steps": int,
    "ref_boost": float,
    "ref_boost_a": float,
    "grounding_px": int,
}


@dataclass(frozen=True)
class EditProfile:
    """A calibrated set of edit-graph knobs for one kind of photo edit.

    ``references`` is 1 for a scene-only edit and 2 when a donor photo is wired as the
    second reference; with two references ``ref_boost`` acts on the donor and
    ``ref_boost_a`` on the scene.
    """

    id: str
    label: str
    description: str
    references: int
    seconds: int
    settings: dict[str, float | int]


@dataclass(frozen=True)
class ApprovedProfile:
    id: str
    name: str
    technical_name: str
    description: str
    prompt_family: PromptFamily
    architecture: str
    source_url: str
    source_version_id: int | None
    vram_min_gb: float
    vram_rec_gb: float
    workflow_ready: bool
    default_negative_prompt: str
    prompt_prefix: str
    prompt_style: str
    aspect_ratios: tuple[dict[str, Any], ...]
    required_resources: tuple[ProfileResourceDependency, ...]
    directory: Path
    max_batch: int = 4
    modes: tuple[str, ...] = ("text",)
    sda_modes: tuple[str, ...] = ()
    custom_nodes: tuple[CustomNodeRequirement, ...] = ()
    edit_profiles: tuple[EditProfile, ...] = ()


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def _load_prompt_style(directory: Path) -> str:
    path = directory / "prompt_style.md"
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return content.strip()


def _load_edit_profile(raw: dict[str, Any], directory: Path) -> EditProfile:
    settings_raw = raw.get("settings") or {}
    unknown = sorted(set(settings_raw) - set(EDIT_SETTING_TYPES))
    if unknown:
        raise ValueError(f"Unknown edit settings {unknown} in {directory / 'manifest.json'}")
    references = int(raw.get("references") or 1)
    if references not in (1, 2):
        raise ValueError(f"Edit profile {raw.get('id')} must use 1 or 2 references")
    return EditProfile(
        id=str(raw["id"]),
        label=str(raw.get("label") or raw["id"]),
        description=str(raw.get("description") or ""),
        references=references,
        seconds=int(raw.get("seconds") or 0),
        settings={key: EDIT_SETTING_TYPES[key](value) for key, value in settings_raw.items()},
    )


def _load_profile(directory: Path) -> ApprovedProfile:
    manifest = _read_json(directory / "manifest.json")
    resources_data = _read_json(directory / "resources.json")
    resources: list[ProfileResourceDependency] = []
    for raw in resources_data.get("required", []):
        resources.append(
            ProfileResourceDependency(
                resource_type=str(raw.get("resource_type") or ""),
                folder=str(raw.get("folder") or ""),
                filename=str(raw.get("filename") or ""),
                display_name=str(raw.get("display_name") or raw.get("filename") or ""),
                download_url=str(raw.get("download_url") or "") or None,
                aliases=tuple(str(item) for item in raw.get("aliases", []) if item),
                sha256=str(raw.get("sha256") or "").casefold() or None,
            )
        )
    custom_nodes = tuple(
        CustomNodeRequirement(
            id=str(raw["id"]),
            display_name=str(raw.get("display_name") or raw["id"]),
            folder=str(raw.get("folder") or raw["id"]),
            repository=str(raw.get("repository") or ""),
            commit=str(raw["commit"]),
            archive_url=str(raw["archive_url"]),
            verify_file=str(raw["verify_file"]),
            verify_sha256=str(raw["verify_sha256"]).casefold(),
            node_classes=tuple(str(item) for item in raw.get("node_classes", []) if item),
            modes=tuple(str(item) for item in raw.get("modes", []) if item),
        )
        for raw in resources_data.get("custom_nodes", [])
    )
    family = PromptFamily(str(manifest.get("prompt_family") or "sdxl"))
    return ApprovedProfile(
        id=str(manifest["id"]),
        name=str(manifest["name"]),
        technical_name=str(manifest.get("technical_name") or manifest["name"]),
        description=str(manifest.get("description") or ""),
        prompt_family=family,
        architecture=str(manifest.get("architecture") or ""),
        source_url=str(manifest.get("source_url") or ""),
        source_version_id=(
            int(manifest["source_version_id"])
            if manifest.get("source_version_id") is not None
            else None
        ),
        vram_min_gb=float(manifest.get("vram_min_gb") or 0),
        vram_rec_gb=float(manifest.get("vram_rec_gb") or 0),
        workflow_ready=bool(manifest.get("workflow_ready")),
        default_negative_prompt=str(manifest.get("default_negative_prompt") or ""),
        prompt_prefix=str(manifest.get("prompt_prefix") or ""),
        prompt_style=_load_prompt_style(directory),
        aspect_ratios=tuple(manifest.get("aspect_ratios") or ()),
        required_resources=tuple(resources),
        directory=directory,
        max_batch=max(1, int(manifest.get("max_batch") or 4)),
        modes=tuple(str(item) for item in manifest.get("modes", ("text",))),
        sda_modes=tuple(str(item) for item in manifest.get("sda_modes", ())),
        custom_nodes=custom_nodes,
        edit_profiles=tuple(_load_edit_profile(raw, directory) for raw in manifest.get("edit_profiles", [])),
    )


def load_approved_profiles(root: Path = SIMPLE_MODELS_DIR) -> dict[str, ApprovedProfile]:
    profiles: dict[str, ApprovedProfile] = {}
    if not root.is_dir():
        return profiles
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest = directory / "manifest.json"
        resources = directory / "resources.json"
        if not manifest.is_file() or not resources.is_file():
            continue
        profile = _load_profile(directory)
        profiles[profile.id] = profile
    return profiles


APPROVED_PROFILES = load_approved_profiles()


def get_simple_workflow_path(profile_or_id: ApprovedProfile | str) -> Path:
    profile = (
        profile_or_id
        if isinstance(profile_or_id, ApprovedProfile)
        else APPROVED_PROFILES[str(profile_or_id)]
    )
    return profile.directory / "workflow.json"


def load_simple_workflow_json(profile_or_id: ApprovedProfile | str) -> dict[str, Any]:
    path = get_simple_workflow_path(profile_or_id)
    if not path.is_file():
        profile_id = profile_or_id.id if isinstance(profile_or_id, ApprovedProfile) else profile_or_id
        raise FileNotFoundError(f"Simple Mode workflow is not prepared yet for {profile_id}")
    return _read_json(path)


def _inventory_names(inventory: Any, folder: str) -> set[str]:
    models = getattr(inventory, "models", None)
    if not isinstance(models, dict):
        return set()
    return {Path(str(name)).name.casefold() for name in models.get(folder, [])}


def _missing_node_packs(
    profile: ApprovedProfile,
    inventory: Any | None,
    installed_node_packs: set[str] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return node packs whose classes ComfyUI does not expose, plus whether that is known.

    While ComfyUI is offline a pack folder on disk counts as present: ComfyUI
    loads it when it starts, and generation checks the live node list again.
    """
    if not profile.custom_nodes:
        return [], True
    online = inventory is not None and bool(getattr(inventory, "online", False))
    if not online and installed_node_packs is None:
        return [], False
    exposed = set(getattr(inventory, "node_types", None) or ()) if online else set()
    installed = installed_node_packs or set()
    missing: list[dict[str, Any]] = []
    for pack in profile.custom_nodes:
        if not online and pack.id in installed:
            continue
        absent = [name for name in pack.node_classes if name not in exposed]
        if absent:
            missing.append({
                "id": pack.id,
                "display_name": pack.display_name,
                "repository": pack.repository,
                "commit": pack.commit,
                "node_classes": absent,
                "modes": list(pack.modes),
                "installed_on_disk": pack.id in installed,
            })
    return missing, True


def check_profile_health(
    profile: ApprovedProfile,
    inventory: Any | None = None,
    *,
    installed_node_packs: set[str] | None = None,
) -> dict[str, Any]:
    missing: list[dict[str, Any]] = []
    online = inventory is not None and bool(getattr(inventory, "online", False))
    # Offline with nothing scanned on disk (no ComfyUI folder set) means unknown, not missing.
    inventory_known = (
        inventory is not None
        and isinstance(getattr(inventory, "models", None), dict)
        and (online or getattr(inventory, "source", None) != "none")
    )
    for dep in profile.required_resources if inventory_known else ():
        candidates = {dep.filename.casefold(), *(alias.casefold() for alias in dep.aliases)}
        if not candidates & _inventory_names(inventory, dep.folder):
            missing.append(
                {
                    "resource_type": dep.resource_type,
                    "folder": dep.folder,
                    "filename": dep.filename,
                    "display_name": dep.display_name,
                    "download_url": dep.download_url,
                }
            )

    if not inventory_known:
        status = "unknown"
        message = "Checking local components"
    elif missing:
        status = "not_installed"
        message = f"Missing components: {len(missing)}"
    elif not profile.workflow_ready or not get_simple_workflow_path(profile).is_file():
        status = "workflow_pending"
        message = "Components are ready; the workflow is still being calibrated"
    else:
        status = "ready"
        message = "Ready to generate"

    missing_nodes, nodes_known = _missing_node_packs(profile, inventory, installed_node_packs)
    restart_required = bool(missing_nodes) and all(item["installed_on_disk"] for item in missing_nodes)
    blocked_modes = {mode for item in missing_nodes for mode in item["modes"]}
    if not nodes_known:
        blocked_modes |= {mode for pack in profile.custom_nodes for mode in pack.modes}
    modes_ready = {
        mode: status == "ready" and mode not in blocked_modes
        for mode in profile.modes
    }
    if status == "ready" and missing_nodes:
        message = (
            "Restart ComfyUI to load the new nodes"
            if restart_required
            else "Photo editing needs ComfyUI nodes that are missing"
        )
    downloadable = all(item.get("download_url") for item in missing)
    installable_nodes = any(not item["installed_on_disk"] for item in missing_nodes)

    return {
        "status": status,
        "ready": status == "ready",
        "comfyui_online": online,
        "missing_resources": missing,
        "missing_nodes": missing_nodes,
        "nodes_known": nodes_known,
        "restart_required": restart_required,
        "modes_ready": modes_ready,
        "installable": (not missing or downloadable) and (bool(missing) or installable_nodes),
        "message": message,
    }


def serialize_approved_profile(
    profile: ApprovedProfile,
    inventory: Any | None = None,
    *,
    include_health: bool = True,
    installed_node_packs: set[str] | None = None,
) -> dict[str, Any]:
    payload = {
        "id": profile.id,
        "name": profile.name,
        "technical_name": profile.technical_name,
        "description": profile.description,
        "prompt_family": profile.prompt_family.value,
        "architecture": profile.architecture,
        "source_url": profile.source_url,
        "source_version_id": profile.source_version_id,
        "vram_min_gb": profile.vram_min_gb,
        "vram_rec_gb": profile.vram_rec_gb,
        "workflow_ready": profile.workflow_ready,
        "aspect_ratios": list(profile.aspect_ratios),
        "max_batch": profile.max_batch,
        "modes": list(profile.modes),
        "edit_profiles": [
            {
                "id": item.id,
                "label": item.label,
                "description": item.description,
                "references": item.references,
                "seconds": item.seconds,
            }
            for item in profile.edit_profiles
        ],
    }
    if include_health:
        payload["health"] = check_profile_health(profile, inventory, installed_node_packs=installed_node_packs)
    return payload


def _set_binding(workflow: dict[str, Any], binding: dict[str, Any] | list | None, value: Any) -> None:
    if not binding:
        return
    if isinstance(binding, list):
        for item in binding:
            _set_binding(workflow, item, value)
        return
    node = workflow.get(str(binding.get("node")))
    if not isinstance(node, dict):
        raise KeyError(f"Workflow node {binding.get('node')} is missing")
    inputs = node.setdefault("inputs", {})
    inputs[str(binding.get("input"))] = value


def _load_bindings(profile: ApprovedProfile, workflow_name: str = "") -> dict[str, Any]:
    path = profile.directory / f"{workflow_name}bindings.json"
    if not path.is_file():
        raise FileNotFoundError(f"Bindings are not prepared yet for {profile.id}")
    return _read_json(path)


TARGET_PIXELS = 1024 * 1024
DIMENSION_STEP = 32


def resolve_dimensions(
    profile: ApprovedProfile,
    aspect_ratio: str,
    source_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Return generation width/height for a preset ratio or the source photo's own ratio.

    ``aspect_ratio="source"`` keeps the source proportions at about one megapixel,
    rounded to the latent grid and clamped to a 1:2.5 .. 2.5:1 range.
    """
    if aspect_ratio == "source" and source_size and min(source_size) > 0:
        ratio = min(max(source_size[0] / source_size[1], 0.4), 2.5)
        width = math.sqrt(TARGET_PIXELS * ratio)
        height = width / ratio
        return (
            max(DIMENSION_STEP, round(width / DIMENSION_STEP) * DIMENSION_STEP),
            max(DIMENSION_STEP, round(height / DIMENSION_STEP) * DIMENSION_STEP),
        )
    item = next(
        (entry for entry in profile.aspect_ratios if str(entry.get("ratio")) == aspect_ratio),
        profile.aspect_ratios[0] if profile.aspect_ratios else {"width": 1024, "height": 1024},
    )
    return int(item.get("width") or 1024), int(item.get("height") or 1024)


def compile_simple_workflow(
    profile: ApprovedProfile,
    *,
    positive_prompt: str,
    negative_prompt: str | None = None,
    aspect_ratio: str = "1:1",
    batch_size: int = 1,
    seed: int = -1,
    mode: str = "text",
    dimensions: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Compile the text-to-image graph; sampling is fixed inside the profile workflow."""
    if not profile.workflow_ready:
        raise FileNotFoundError(f"Workflow is still being calibrated for {profile.name}")

    workflow = copy.deepcopy(load_simple_workflow_json(profile))
    bindings = _load_bindings(profile)

    width, height = dimensions or resolve_dimensions(profile, aspect_ratio)
    effective_seed = seed if seed >= 0 else random.randint(0, 1125899906842624)
    effective_positive = positive_prompt.strip()
    if profile.prompt_prefix and effective_positive:
        effective_positive = f"{profile.prompt_prefix}, {effective_positive}"
    effective_negative = negative_prompt if negative_prompt is not None else profile.default_negative_prompt

    _set_binding(workflow, bindings.get("positive_prompt"), effective_positive)
    _set_binding(workflow, bindings.get("negative_prompt"), effective_negative or "")
    _set_binding(workflow, bindings.get("seed"), effective_seed)
    _set_binding(workflow, bindings.get("width"), width)
    _set_binding(workflow, bindings.get("height"), height)
    _set_binding(workflow, bindings.get("batch_size"), max(1, min(int(batch_size), profile.max_batch)))
    # The SDA diversity LoRA only acts on the first sampling steps; strength 0 makes it a no-op.
    _set_binding(workflow, bindings.get("sda_strength"), 1.0 if mode in profile.sda_modes else 0.0)
    return workflow


def has_edit_workflow(profile: ApprovedProfile) -> bool:
    return "edit" in profile.modes and (profile.directory / "edit.workflow.json").is_file()


def resolve_edit_profile(
    profile: ApprovedProfile,
    edit_profile: str | None = None,
    *,
    references: int = 1,
) -> EditProfile | None:
    """Return the named edit profile, or the first one made for ``references`` photos.

    Profiles without edit profiles keep the knobs stored in their edit graph (``None``).
    Raises ``ValueError`` for an unknown id or one made for another number of photos.
    """
    if not profile.edit_profiles:
        if edit_profile:
            raise ValueError(f"{profile.name} has no edit profiles")
        return None
    if not edit_profile:
        return next((item for item in profile.edit_profiles if item.references == references), None)
    item = next((entry for entry in profile.edit_profiles if entry.id == edit_profile), None)
    if item is None:
        raise ValueError(f"Unknown edit profile '{edit_profile}'")
    if item.references != references:
        raise ValueError(f"Edit profile '{edit_profile}' needs {item.references} photo(s), got {references}")
    return item


def _add_subject_reference(
    profile: ApprovedProfile,
    workflow: dict[str, Any],
    bindings: dict[str, Any],
    subject_image: str,
) -> None:
    """Wire a donor photo in as the second reference: scene first, subject second."""
    fragment = profile.directory / "edit.subject.workflow.json"
    if not fragment.is_file() or not bindings.get("subject_image"):
        raise FileNotFoundError(f"Second reference is not prepared for {profile.name}")
    workflow.update(copy.deepcopy(_read_json(fragment)))
    _set_binding(workflow, bindings["subject_image"], subject_image)
    for link in bindings.get("subject_links", []):
        _set_binding(workflow, link, list(link["from"]))


def compile_edit_workflow(
    profile: ApprovedProfile,
    *,
    instruction: str,
    source_image: str,
    dimensions: tuple[int, int],
    seed: int = -1,
    edit_profile: str | None = None,
    subject_image: str | None = None,
    negative_instruction: str = "",
) -> dict[str, Any]:
    """Compile the instruction-edit graph for images already uploaded to ComfyUI input.

    ``subject_image`` adds a donor photo as the second reference; ``edit_profile``
    picks the calibrated knobs (default: the first profile for that photo count).
    ``negative_instruction`` describes what must disappear (e.g. a removed garment);
    it is grounded on the same photo and only acts when the profile's cfg is above 1.
    """
    if not has_edit_workflow(profile):
        raise FileNotFoundError(f"Edit workflow is not prepared for {profile.name}")
    settings = resolve_edit_profile(profile, edit_profile, references=2 if subject_image else 1)
    workflow = copy.deepcopy(_read_json(profile.directory / "edit.workflow.json"))
    bindings = _load_bindings(profile, "edit.")
    effective_seed = seed if seed >= 0 else random.randint(0, 1125899906842624)
    _set_binding(workflow, bindings.get("positive_prompt"), instruction.strip())
    _set_binding(workflow, bindings.get("negative_prompt"), negative_instruction.strip())
    _set_binding(workflow, bindings.get("source_image"), source_image)
    _set_binding(workflow, bindings.get("seed"), effective_seed)
    _set_binding(workflow, bindings.get("width"), int(dimensions[0]))
    _set_binding(workflow, bindings.get("height"), int(dimensions[1]))
    if subject_image:
        _add_subject_reference(profile, workflow, bindings, subject_image)
    for key, value in (settings.settings.items() if settings else ()):
        if not bindings.get(key):
            raise KeyError(f"Edit setting '{key}' has no binding in {profile.id}")
        _set_binding(workflow, bindings[key], value)
    return workflow
