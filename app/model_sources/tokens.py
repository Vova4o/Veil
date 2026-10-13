"""The Civitai API token, kept in the system credential store like provider API keys.

Civitai serves some models (Moody Krea2) only to signed-in users. config.json holds the token
only where no system credential store exists; a token that an older version saved there moves
into the credential store the first time it is read.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

from app.ai.secrets import SecretStore, SecretStoreError, SystemSecretStore
from app.config_store import ConfigStore

CIVITAI_SECRET = "model-source:civitai"
CIVITAI_ME_URL = "https://civitai.com/api/v1/me"
USER_AGENT = "comfy-meta-viewer"

_lock = threading.Lock()


class ModelSourceError(RuntimeError):
    def __init__(self, message: str, code: str = "model_source_failed"):
        super().__init__(message)
        self.code = code


def secret_store() -> SecretStore:
    """Where model-host tokens live; tests swap in an in-memory store."""
    return SystemSecretStore()


def _config_token(store: ConfigStore) -> str:
    return str(store.comfyui_settings().get("civitai_api_token") or "").strip()


def civitai_token(store: ConfigStore) -> str:
    """The saved Civitai token, or an empty string."""
    secrets = secret_store()
    with _lock:
        plain = _config_token(store)
        if not secrets.status().available:
            return plain
        try:
            if plain:
                # Saved by an older version, or while no credential store was available.
                secrets.set(CIVITAI_SECRET, plain)
                store.update_comfyui_settings(civitai_api_token="")
                return plain
            return (secrets.get(CIVITAI_SECRET) or "").strip()
        except SecretStoreError:
            return plain


def save_civitai_token(store: ConfigStore, token: str) -> str:
    """Keep the token; returns where it went: "system" or "config"."""
    token = token.strip()
    if not token:
        raise ModelSourceError("Paste a Civitai API key.", code="token_empty")
    secrets = secret_store()
    with _lock:
        if not secrets.status().available:
            store.update_comfyui_settings(civitai_api_token=token)
            return "config"
        try:
            secrets.set(CIVITAI_SECRET, token)
        except SecretStoreError as exc:
            raise ModelSourceError(str(exc), code="secret_store_failed") from exc
        if _config_token(store):
            store.update_comfyui_settings(civitai_api_token="")
        return "system"


def clear_civitai_token(store: ConfigStore) -> None:
    secrets = secret_store()
    with _lock:
        if secrets.status().available:
            try:
                secrets.delete(CIVITAI_SECRET)
            except SecretStoreError as exc:
                raise ModelSourceError(str(exc), code="secret_store_failed") from exc
        if _config_token(store):
            store.update_comfyui_settings(civitai_api_token="")


def civitai_state(store: ConfigStore) -> dict[str, Any]:
    """Whether a token is saved and where; never the token itself."""
    status = secret_store().status()
    saved = bool(civitai_token(store))
    return {
        "saved": saved,
        "storage": ("system" if status.available else "config") if saved else None,
        "secret_store": status.to_dict(),
    }


def check_civitai_token(token: str, *, opener: Any = None) -> dict[str, Any]:
    """Ask Civitai whose token this is; raises ModelSourceError when it is not accepted."""
    request = urllib.request.Request(
        CIVITAI_ME_URL,
        headers={"Authorization": f"Bearer {token.strip()}", "User-Agent": USER_AGENT},
    )
    try:
        with (opener or urllib.request.build_opener()).open(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ModelSourceError(
                "Civitai did not accept this API key. Create a new one under API Keys in your Civitai account settings.",
                code="token_rejected",
            ) from exc
        raise ModelSourceError(f"Civitai answered HTTP {exc.code}.", code="civitai_failed") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ModelSourceError(f"Cannot reach Civitai: {exc}", code="civitai_unreachable") from exc
    if not isinstance(data, dict):
        raise ModelSourceError("Civitai returned an unexpected answer.", code="civitai_failed")
    return {"username": str(data.get("username") or ""), "member": bool(data.get("isMember"))}
