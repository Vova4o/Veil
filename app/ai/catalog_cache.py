"""Profile-local model cards, reusable after a backend restart; no credentials.

Cards listed by this process stay valid until it ends or someone asks for a refresh;
TTL_SECONDS only bounds cards a previous run left on disk.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from app.paths import build_runtime_paths

logger = logging.getLogger(__name__)
TTL_SECONDS = 600
memory: dict[str, dict[str, Any]] = {}
_listed_here: set[str] = set()


def cache_directory() -> Path:
    return build_runtime_paths().cache_dir / "model-catalogs"


def _identity(host: str, executable: str) -> dict[str, Any]:
    path = Path(executable)
    try:
        stat = path.stat()
        version = [stat.st_mtime_ns, stat.st_size]
    except OSError:
        version = None
    return {"host": host, "executable": str(path.resolve()), "version": version}


def _key(identity: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def load(host: str, executable: str) -> list[dict[str, Any]] | None:
    identity = _identity(host, executable)
    key = _key(identity)
    location = cache_directory() / f"{key}.json"
    record = memory.get(str(location))
    listed_here = record is not None and str(location) in _listed_here
    if record is None:
        _listed_here.discard(str(location))
        try:
            if location.stat().st_size > 4_000_000:
                return None
            record = json.loads(location.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    if not isinstance(record, dict) or record.get("schema") != 1 or record.get("identity") != identity:
        return None
    saved_at = record.get("saved_at")
    if not listed_here and (
        not isinstance(saved_at, (float, int)) or not 0 <= time.time() - saved_at < TTL_SECONDS
    ):
        return None
    models = record.get("models")
    if not isinstance(models, list) or not models or any(
        not isinstance(model, dict) or not isinstance(model.get("id"), str) for model in models
    ):
        return None
    if host in {"opencode", "opencode-zen"} and any(
        not isinstance(model.get("provider"), str) or not isinstance(model.get("name"), str)
        or not isinstance(model.get("variants", []), list)
        for model in models
    ):
        return None
    memory[str(location)] = record
    return models


def save(host: str, executable: str, models: list[dict[str, Any]]) -> None:
    if not models:
        return
    identity = _identity(host, executable)
    location = cache_directory() / f"{_key(identity)}.json"
    record = {"schema": 1, "identity": identity, "saved_at": time.time(), "models": models}
    memory[str(location)] = record
    _listed_here.add(str(location))
    temporary = location.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        location.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(location)
    except OSError:
        logger.debug("Could not persist the %s model catalog", host, exc_info=True)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
