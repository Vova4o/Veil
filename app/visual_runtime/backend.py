"""Studio v1 visual backend pin (EDITOR-STUDIO-RUNTIME-PLAN §1, §3).

The Studio product path uses exactly one tested image model. This module is
the single source of truth for that pin so skills, primitives, health checks,
and the future Studio API never drift into a model zoo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: Primary Studio v1 checkpoint file (plan §3).
STUDIO_BACKEND_MODEL_FILENAME = "moodyKrea2Mix_v40.safetensors"

STUDIO_BACKEND_DISPLAY_NAME = "Moody Krea 2 Mix v40 (Studio)"

STUDIO_BACKEND_ARCHITECTURE = "krea2"

#: ComfyUI models subfolder that holds the checkpoint.
STUDIO_BACKEND_RESOURCE_FOLDER = "diffusion_models"

#: Filenames that resolve to the same backend family (migration bridge).
#: model_04 currently ships `Moody-Krea-Mix-v7_00002__clean_fp8.safetensors`
#: with alias `moody_krea_mix.safetensors`; both are accepted as the Studio
#: backend until the v40 file lands locally.
STUDIO_BACKEND_ALIASES: tuple[str, ...] = (
    "moody_krea_mix.safetensors",
    "Moody-Krea-Mix-v7_00002__clean_fp8.safetensors",
)


def _normalize_filename(name: str | Path) -> str:
    return Path(str(name)).name.casefold()


def is_studio_backend_file(filename: str | Path) -> bool:
    """Return True when *filename* is the Studio backend or a known alias."""
    normalized = _normalize_filename(filename)
    candidates = {STUDIO_BACKEND_MODEL_FILENAME.casefold()}
    candidates.update(alias.casefold() for alias in STUDIO_BACKEND_ALIASES)
    return normalized in candidates


def studio_backend_candidates() -> tuple[str, ...]:
    """All filenames accepted as the Studio backend (primary first)."""
    return (STUDIO_BACKEND_MODEL_FILENAME, *STUDIO_BACKEND_ALIASES)


def check_studio_backend_health(inventory: Any | None = None) -> dict[str, Any]:
    """Describe whether the Studio backend is present in a runtime inventory.

    *inventory* is the object returned by ``collect_runtime_inventory`` (has a
    ``models`` dict mapping folder -> file list). ``None``/unknown shape means
    the state cannot be determined yet (mirrors simple_profiles health).
    """
    models = getattr(inventory, "models", None)
    if not isinstance(models, dict):
        return {
            "ready": False,
            "status": "unknown",
            "message": "Проверяем локальные компоненты",
            "filename": STUDIO_BACKEND_MODEL_FILENAME,
        }
    folder_files = models.get(STUDIO_BACKEND_RESOURCE_FOLDER, [])
    known = {str(name).casefold() for name in folder_files}
    # The backend may also appear under checkpoints on some installs.
    for extra_folder in ("checkpoints", "diffusion_models"):
        extra = models.get(extra_folder, [])
        if isinstance(extra, list):
            known.update(str(name).casefold() for name in extra)
    wanted = {name.casefold() for name in studio_backend_candidates()}
    if known & wanted:
        return {
            "ready": True,
            "status": "ready",
            "message": "Studio backend готов",
            "filename": STUDIO_BACKEND_MODEL_FILENAME,
        }
    return {
        "ready": False,
        "status": "not_installed",
        "message": f"Нет Studio backend: {STUDIO_BACKEND_MODEL_FILENAME}",
        "filename": STUDIO_BACKEND_MODEL_FILENAME,
    }


__all__ = [
    "STUDIO_BACKEND_ALIASES",
    "STUDIO_BACKEND_ARCHITECTURE",
    "STUDIO_BACKEND_DISPLAY_NAME",
    "STUDIO_BACKEND_MODEL_FILENAME",
    "STUDIO_BACKEND_RESOURCE_FOLDER",
    "check_studio_backend_health",
    "is_studio_backend_file",
    "studio_backend_candidates",
]
