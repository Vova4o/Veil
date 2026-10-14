from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIRNAME = ".mediastudio_data"
LEGACY_DATA_DIRNAME = ".comfy_meta_uploads"

log = logging.getLogger(__name__)


class PathValidationError(ValueError):
    """Raised when a user-provided filesystem path cannot be used."""


def normalize_path(
    value: str | Path,
    *,
    base_dir: str | Path = PROJECT_ROOT,
) -> Path:
    """Return an absolute native path without requiring it to exist."""
    raw_value = os.path.expandvars(os.fspath(value))
    if not raw_value or "\x00" in raw_value:
        raise PathValidationError("Path is empty or contains invalid characters")

    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        path = Path(base_dir).expanduser() / path

    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathValidationError(f"Invalid path: {value}") from exc


def normalize_existing_directory(
    value: str | Path,
    *,
    base_dir: str | Path = PROJECT_ROOT,
) -> Path:
    """Normalize and validate a source directory without changing its contents."""
    path = normalize_path(value, base_dir=base_dir)
    try:
        is_directory = path.is_dir()
    except OSError as exc:
        raise PathValidationError(f"Cannot access directory: {path}") from exc
    if not is_directory:
        raise PathValidationError(f"Not a directory: {path}")
    return path


def portable_filename(value: str) -> str:
    """Extract a filename from browser input using either common separator style."""
    return PureWindowsPath(value).name or "upload"


@dataclass(frozen=True)
class RuntimePaths:
    project_root: Path
    data_dir: Path
    database: Path
    config: Path
    cache_dir: Path
    thumbnails: Path
    previews: Path
    cutouts: Path

    def ensure_directories(self) -> None:
        """Create application-owned directories, never indexed source directories."""
        for directory in (
            self.data_dir,
            self.thumbnails,
            self.previews,
            self.cutouts,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def flask_config(self) -> dict[str, str]:
        return {
            "UPLOAD_FOLDER": str(self.data_dir),
            "CONFIG_FILE": str(self.config),
            "THUMBNAIL_FOLDER": str(self.thumbnails),
            "PREVIEW_FOLDER": str(self.previews),
            "CUTOUT_FOLDER": str(self.cutouts),
        }


def build_runtime_paths(
    environ: Mapping[str, str] | None = None,
    *,
    project_root: str | Path = PROJECT_ROOT,
) -> RuntimePaths:
    """Build stable application paths independently of the process working directory."""
    env = os.environ if environ is None else environ
    root = normalize_path(project_root, base_dir=Path.cwd())

    # MEDIASTUDIO_UPLOAD remains supported for compatibility with existing installs.
    data_value = env.get("MEDIASTUDIO_DATA_DIR") or env.get("MEDIASTUDIO_UPLOAD")
    cache_value = env.get("MEDIASTUDIO_CACHE_DIR")
    data_dir = normalize_path(data_value, base_dir=root) if data_value else root / DATA_DIRNAME
    cache_dir = normalize_path(cache_value, base_dir=root) if cache_value else root / "cache"

    return RuntimePaths(
        project_root=root,
        data_dir=data_dir,
        database=data_dir / "meta.db",
        config=data_dir / "config.json",
        cache_dir=cache_dir,
        thumbnails=cache_dir / "thumbnails",
        previews=cache_dir / "previews",
        cutouts=cache_dir / "cutouts",
    )


def migrate_legacy_data_dir(paths: RuntimePaths) -> None:
    """Move a pre-codename development data folder to its current name, once.

    Only the default location is touched; an explicit data directory is the caller's.
    """
    legacy = paths.project_root / LEGACY_DATA_DIRNAME
    if paths.data_dir != paths.project_root / DATA_DIRNAME:
        return
    if not legacy.is_dir() or paths.data_dir.exists():
        return
    try:
        legacy.rename(paths.data_dir)
    except OSError as exc:
        log.warning("Could not move %s to %s: %s", legacy, paths.data_dir, exc)
