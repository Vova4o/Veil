"""Install pinned ComfyUI custom node packs required by Create profiles."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from .simple_profiles import CustomNodeRequirement

MARKER_NAME = ".mediastudio-node-pack.json"
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
SKIPPED_TOP_LEVEL = {"tests", ".github", "node.zip"}


class NodePackInstallError(RuntimeError):
    def __init__(self, message: str, *, code: str):
        self.code = code
        super().__init__(message)


def custom_nodes_root(comfy_dir: Path | None) -> Path | None:
    if comfy_dir is None:
        return None
    root = Path(comfy_dir) / "custom_nodes"
    return root if root.is_dir() else None


def installed_node_packs(
    requirements: tuple[CustomNodeRequirement, ...] | list[CustomNodeRequirement],
    root: Path | None,
) -> set[str]:
    """Return ids of packs whose folder already exists, whoever installed them."""
    if root is None:
        return set()
    return {pack.id for pack in requirements if (root / pack.folder).is_dir()}


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "mediastudio"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise NodePackInstallError("Node pack archive is unexpectedly large.", code="archive_too_large")
    return data


def _safe_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Strip the single top-level directory and reject unsafe or unexpected paths."""
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
            raise NodePackInstallError(f"Unsafe path in archive: {info.filename}", code="unsafe_archive")
        if len(path.parts) < 2 or info.is_dir():
            continue
        relative = PurePosixPath(*path.parts[1:])
        if relative.parts[0] in SKIPPED_TOP_LEVEL:
            continue
        members.append((info, relative))
    if not members:
        raise NodePackInstallError("Node pack archive is empty.", code="empty_archive")
    return members


def install_node_pack(
    pack: CustomNodeRequirement,
    root: Path,
    *,
    download: Callable[[str], bytes] = _download,
) -> dict[str, Any]:
    """Install one pack into ``root``; existing folders are reported, never overwritten."""
    target = root / pack.folder
    if target.exists():
        return {"id": pack.id, "status": "already_present", "path": str(target)}

    try:
        data = download(pack.archive_url)
    except NodePackInstallError:
        raise
    except Exception as exc:
        raise NodePackInstallError(f"Could not download {pack.display_name}: {exc}", code="download_failed") from exc

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise NodePackInstallError("Downloaded node pack is not a zip archive.", code="bad_archive") from exc

    with archive:
        members = _safe_members(archive)
        verify = next((info for info, rel in members if str(rel) == pack.verify_file), None)
        if verify is None:
            raise NodePackInstallError(f"{pack.verify_file} is missing from the archive.", code="verify_missing")
        digest = hashlib.sha256(archive.read(verify)).hexdigest()
        if digest != pack.verify_sha256:
            raise NodePackInstallError(
                f"{pack.display_name} does not match the reviewed commit (sha256 {digest}).",
                code="checksum_mismatch",
            )

        staging = root / f".{pack.folder}.mediastudio-staging"
        if staging.exists():
            shutil.rmtree(staging)
        try:
            for info, relative in members:
                destination = staging.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(info))
            (staging / MARKER_NAME).write_text(
                json.dumps({"id": pack.id, "repository": pack.repository, "commit": pack.commit}, indent=2),
                encoding="utf-8",
            )
            staging.rename(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    return {"id": pack.id, "status": "installed", "path": str(target), "commit": pack.commit}
