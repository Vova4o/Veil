"""Integrations › Providers: API providers, local tools, model visibility, default model.

A provider is an OpenAI-compatible endpoint reached with an API key (a models.dev
template such as OpenRouter, or a custom one). Local AI tools (OpenCode, Claude Code,
Codex, Antigravity) need no record: an installed tool is connected unless the user
disconnected it. Which models the pickers show, and the default model used where no
model was picked, are stored here as well; ``app/ai/agents.py`` turns them into runs.

API keys stay in the OS credential store under the provider id; config.json keeps
only non-secret settings. Files written before providers replaced the AI profiles
are migrated once: OpenAI-compatible profiles become providers and the default
profile becomes the default model.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config_store import ConfigStore
from . import provider_catalog
from .secrets import SecretStore, SecretStoreError, SystemSecretStore

LOCAL_TOOLS = ("opencode", "claude", "codex", "antigravity")
API_KEY_SOURCES = {"system", "environment", "none"}
PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MODEL_ID_RE = re.compile(r"^[^\s\x00]{1,200}$")
# A run target: a local tool, or "api:<provider id>".
HOST_RE = re.compile(r"^(?:opencode|claude|codex|antigravity|api:[a-z0-9][a-z0-9_-]{0,47})$")
MAX_MANUAL_MODELS = 200
_lock = threading.RLock()


class AIProviderError(RuntimeError):
    """Raised for an invalid or unavailable provider setting."""

    def __init__(self, message: str, *, code: str = "invalid_provider"):
        self.code = code
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AIProviderError(f"{field} is required.")
    cleaned = value.strip()
    if "\x00" in cleaned or len(cleaned) > maximum:
        raise AIProviderError(f"{field} is invalid.")
    return cleaned


def validate_base_url(value: Any, *, api_key_source: str) -> str:
    base_url = _clean_text(value, "Base URL", maximum=2048).rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AIProviderError("Base URL must start with http:// or https://.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AIProviderError("Base URL cannot contain credentials, query parameters, or a fragment.")
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and parsed.hostname.lower() not in local_hosts and api_key_source != "none":
        raise AIProviderError("API keys can only be sent over HTTPS or to a local endpoint.")
    return base_url


def _manual_models(value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > MAX_MANUAL_MODELS:
        raise AIProviderError("Models must be a list of model IDs.")
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        model_id = item.get("id") if isinstance(item, dict) else item
        if not isinstance(model_id, str) or not MODEL_ID_RE.fullmatch(model_id.strip()):
            raise AIProviderError("Each model needs an ID without spaces.")
        model_id = model_id.strip()
        if model_id in seen:
            raise AIProviderError(f"The model '{model_id}' is listed twice.")
        seen.add(model_id)
        name = item.get("name") if isinstance(item, dict) else None
        name = name.strip() if isinstance(name, str) and name.strip() else model_id
        models.append({"id": model_id, "name": name[:120]})
    return models


def validate_model_ref(value: Any) -> dict[str, Any]:
    """``{"host", "model", "variant"}``: one model of a local tool or API provider."""
    if not isinstance(value, dict):
        raise AIProviderError("A model reference is required.", code="invalid_model")
    host = value.get("host")
    model = value.get("model")
    variant = value.get("variant")
    if not isinstance(host, str) or not HOST_RE.fullmatch(host):
        raise AIProviderError("Unknown model host.", code="invalid_model")
    if not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model.strip()):
        raise AIProviderError("A model ID is required.", code="invalid_model")
    if variant in ("", None):
        variant = None
    elif not isinstance(variant, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}", variant):
        raise AIProviderError("The reasoning level is invalid.", code="invalid_model")
    return {"host": host, "model": model.strip(), "variant": variant}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.casefold()).strip("-_")
    return slug[:40] or "provider"


class AIProviderStore:
    """Persist provider settings and keep their keys in the system credential store."""

    def __init__(self, config_path: str | Path, *, secret_store: SecretStore | None = None):
        self.config = ConfigStore(config_path)
        self.secrets = secret_store or SystemSecretStore()

    # --- persistence ------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        config = self.config.load()
        if config["ai"]["providers"] is None:
            self._migrate_profiles(config)
            self.config.save(config)
            config = self.config.load()
        return config

    def _migrate_profiles(self, config: dict[str, Any]) -> None:
        ai = config["ai"]
        profiles = ai.pop("profiles", [])
        legacy_defaults = {key: ai["defaults"].pop(key, None) for key in (
            "multimodal_profile_id", "text_profile_id", "translator_profile_id")}
        providers: list[dict[str, Any]] = []
        provider_for_profile: dict[str, str] = {}
        by_endpoint: dict[tuple[Any, ...], dict[str, Any]] = {}
        for profile in profiles:
            if profile.get("kind", "openai_compatible") != "openai_compatible":
                continue
            source = profile.get("api_key_source") if profile.get("api_key_source") in API_KEY_SOURCES else "system"
            try:
                base_url = validate_base_url(profile.get("base_url"), api_key_source=source)
            except AIProviderError:
                continue
            model = profile.get("model") if isinstance(profile.get("model"), str) else ""
            endpoint = (base_url.casefold(), source, profile.get("api_key_env"))
            provider = by_endpoint.get(endpoint)
            if provider is None:
                old_id = str(profile.get("id") or "")
                template = provider_catalog.template_for_url(base_url)
                name = re.sub(r"\s*\((?:text|translator|vision)\)\s*$", "", str(profile.get("name") or ""), flags=re.IGNORECASE)
                taken = {item["id"] for item in providers}
                wanted = template["id"] if template else _slug(name)
                provider_id = wanted
                suffix = 2
                while provider_id in taken:
                    provider_id = f"{wanted}-{suffix}"
                    suffix += 1
                if source == "system" and old_id and old_id != provider_id:
                    try:
                        secret = self.secrets.get(old_id)
                        if secret:
                            self.secrets.set(provider_id, secret)
                            self.secrets.delete(old_id)
                    except SecretStoreError:
                        # Keep the key where it is; the provider takes the old id.
                        provider_id = old_id if PROVIDER_ID_RE.fullmatch(old_id) else provider_id
                provider = {
                    "id": provider_id,
                    "template": template["id"] if template else None,
                    "name": (template["name"] if template else name) or provider_id,
                    "base_url": base_url,
                    "api_key_source": source,
                    "api_key_env": profile.get("api_key_env") if source == "environment" else None,
                    "models": [],
                    "created_at": profile.get("created_at") or _now(),
                    "updated_at": _now(),
                }
                providers.append(provider)
                by_endpoint[endpoint] = provider
            if model and not any(item["id"] == model for item in provider["models"]):
                provider["models"].append({"id": model, "name": model})
            provider_for_profile[str(profile.get("id"))] = provider["id"]
        ai["providers"] = providers
        if ai.get("default_model") is None:
            by_id = {str(profile.get("id")): profile for profile in profiles}
            for profile_id in legacy_defaults.values():
                profile = by_id.get(str(profile_id)) if profile_id else None
                if not profile or not isinstance(profile.get("model"), str):
                    continue
                if profile.get("kind") == "cli" and profile.get("cli_type") in LOCAL_TOOLS:
                    model, _, variant = profile["model"].partition("#")
                    ai["default_model"] = {"host": profile["cli_type"], "model": model, "variant": variant or None}
                    break
                if profile["id"] in provider_for_profile:
                    ai["default_model"] = {
                        "host": f"api:{provider_for_profile[profile['id']]}", "model": profile["model"], "variant": None,
                    }
                    break

    # --- providers --------------------------------------------------------------

    def _secret_state(self, provider: dict[str, Any]) -> tuple[bool, str | None]:
        source = provider.get("api_key_source")
        if source == "none":
            return True, None
        if source == "environment":
            return bool(os.environ.get(provider.get("api_key_env") or "")), None
        try:
            return bool(self.secrets.get(provider["id"])), None
        except SecretStoreError as exc:
            return False, str(exc)

    def public_provider(self, provider: dict[str, Any]) -> dict[str, Any]:
        result = dict(provider)
        has_credentials, error = self._secret_state(provider)
        result["has_credentials"] = has_credentials
        if error:
            result["credential_error"] = error
        return result

    def providers(self) -> list[dict[str, Any]]:
        with _lock:
            return [dict(item) for item in self._load()["ai"]["providers"]]

    def get(self, provider_id: str) -> dict[str, Any]:
        provider = next((item for item in self.providers() if item.get("id") == provider_id), None)
        if provider is None:
            raise AIProviderError("This provider is not connected.", code="provider_not_found")
        return provider

    def list(self) -> dict[str, Any]:
        with _lock:
            ai = self._load()["ai"]
            return {
                "providers": [self.public_provider(item) for item in ai["providers"]],
                "disconnected_tools": list(ai["tools"]["disconnected"]),
                "default_model": ai["default_model"],
                "secret_store": self.secrets.status().to_dict(),
            }

    def _validated(self, payload: dict[str, Any], *, current: dict[str, Any] | None, taken: set[str]) -> dict[str, Any]:
        template = provider_catalog.template(payload.get("template") if current is None else current.get("template"))
        if current is None and payload.get("template") and template is None:
            raise AIProviderError("Unknown provider template.")
        source = payload.get("api_key_source") or (current or {}).get("api_key_source") or (
            "none" if template and template["local"] else "system")
        if source not in API_KEY_SOURCES:
            raise AIProviderError("Unsupported API key source.")
        if current is not None:
            provider_id = current["id"]
        else:
            requested = payload.get("id")
            if template and not requested:
                provider_id, suffix = template["id"], 2
                while provider_id in taken:
                    provider_id = f"{template['id']}-{suffix}"
                    suffix += 1
            else:
                provider_id = _clean_text(requested, "Provider ID", maximum=48)
                if not PROVIDER_ID_RE.fullmatch(provider_id):
                    raise AIProviderError("Use lowercase letters, numbers, hyphens, or underscores for the provider ID.")
                if provider_id in taken or provider_id in LOCAL_TOOLS:
                    raise AIProviderError("That provider ID already exists.", code="provider_exists")
        name = payload.get("name", (current or {}).get("name") or (template or {}).get("name"))
        base_url = payload.get("base_url", (current or {}).get("base_url") or (template or {}).get("base_url"))
        api_key_env = payload.get("api_key_env", (current or {}).get("api_key_env"))
        if source == "environment":
            api_key_env = _clean_text(api_key_env or ((template or {}).get("env") or [""])[0], "Environment variable", maximum=100)
            if not ENVIRONMENT_NAME_RE.fullmatch(api_key_env):
                raise AIProviderError("Environment variable name is invalid.")
        else:
            api_key_env = None
        return {
            "id": provider_id,
            "template": template["id"] if template else None,
            "name": _clean_text(name, "Display name", maximum=80),
            "base_url": validate_base_url(base_url, api_key_source=source),
            "api_key_source": source,
            "api_key_env": api_key_env,
            "models": _manual_models(payload["models"]) if "models" in payload else list((current or {}).get("models") or []),
            "created_at": (current or {}).get("created_at") or _now(),
            "updated_at": _now(),
        }

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            config = self._load()
            providers = config["ai"]["providers"]
            provider = self._validated(payload, current=None, taken={item["id"] for item in providers})
            api_key = payload.get("api_key").strip() if isinstance(payload.get("api_key"), str) else ""
            if provider["api_key_source"] == "system":
                if not api_key:
                    raise AIProviderError("API key is required.", code="missing_credentials")
                self.secrets.set(provider["id"], api_key)
            providers.append(provider)
            try:
                self.config.save(config)
            except Exception:
                if provider["api_key_source"] == "system":
                    self.secrets.delete(provider["id"])
                raise
            return self.public_provider(provider)

    def update(self, provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            config = self._load()
            providers = config["ai"]["providers"]
            index = next((i for i, item in enumerate(providers) if item.get("id") == provider_id), None)
            if index is None:
                raise AIProviderError("This provider is not connected.", code="provider_not_found")
            old = providers[index]
            updated = self._validated(payload, current=old, taken=set())
            api_key = payload.get("api_key").strip() if isinstance(payload.get("api_key"), str) else ""
            old_secret = self.secrets.get(provider_id) if old["api_key_source"] == "system" else None
            if updated["api_key_source"] == "system":
                if api_key:
                    self.secrets.set(provider_id, api_key)
                elif not old_secret:
                    raise AIProviderError("API key is required.", code="missing_credentials")
            elif old_secret:
                self.secrets.delete(provider_id)
            providers[index] = updated
            try:
                self.config.save(config)
            except Exception:
                if old_secret:
                    self.secrets.set(provider_id, old_secret)
                elif api_key:
                    self.secrets.delete(provider_id)
                raise
            return self.public_provider(updated)

    def delete(self, provider_id: str) -> None:
        with _lock:
            config = self._load()
            ai = config["ai"]
            provider = next((item for item in ai["providers"] if item.get("id") == provider_id), None)
            if provider is None:
                raise AIProviderError("This provider is not connected.", code="provider_not_found")
            old_secret = None
            if provider.get("api_key_source") == "system":
                old_secret = self.secrets.get(provider_id)
                self.secrets.delete(provider_id)
            host = f"api:{provider_id}"
            ai["providers"] = [item for item in ai["providers"] if item.get("id") != provider_id]
            ai["models"]["visibility"] = {
                key: value for key, value in ai["models"]["visibility"].items() if not key.startswith(f"{host}|")
            }
            if (ai.get("default_model") or {}).get("host") == host:
                ai["default_model"] = None
            try:
                self.config.save(config)
            except Exception:
                if old_secret:
                    self.secrets.set(provider_id, old_secret)
                raise

    def resolve_api_key(self, profile: dict[str, Any]) -> str | None:
        """The key of the provider an execution profile runs on (none for local tools)."""
        provider_id = profile.get("provider_id")
        if profile.get("kind") != "openai_compatible" or not provider_id:
            return None
        source = profile.get("api_key_source")
        if source == "none":
            return None
        value = (
            os.environ.get(profile.get("api_key_env") or "")
            if source == "environment"
            else self.secrets.get(provider_id)
        )
        if not value:
            raise AIProviderError(f"No API key is available for {profile.get('provider_name') or provider_id}.",
                                  code="missing_credentials")
        return value

    def delete_all_secrets(self) -> None:
        with _lock:
            entries = []
            for provider in self.providers():
                if provider.get("api_key_source") == "system":
                    value = self.secrets.get(provider["id"])
                    if value:
                        entries.append((provider["id"], value))
            deleted: list[tuple[str, str]] = []
            try:
                for provider_id, value in entries:
                    self.secrets.delete(provider_id)
                    deleted.append((provider_id, value))
            except Exception:
                for provider_id, value in deleted:
                    self.secrets.set(provider_id, value)
                raise

    # --- local tools, models ----------------------------------------------------

    def disconnected_tools(self) -> set[str]:
        with _lock:
            return set(self._load()["ai"]["tools"]["disconnected"])

    def set_tool_connected(self, tool: str, connected: bool) -> None:
        if tool not in LOCAL_TOOLS:
            raise AIProviderError("Unknown local AI tool.")
        with _lock:
            config = self._load()
            disconnected = [item for item in config["ai"]["tools"]["disconnected"] if item != tool]
            if not connected:
                disconnected.append(tool)
                if (config["ai"].get("default_model") or {}).get("host") == tool:
                    config["ai"]["default_model"] = None
            config["ai"]["tools"]["disconnected"] = disconnected
            self.config.save(config)

    def visibility(self) -> dict[str, bool]:
        with _lock:
            return dict(self._load()["ai"]["models"]["visibility"])

    def set_visibility(self, changes: dict[str, bool | None]) -> dict[str, bool]:
        """Show or hide models by key (``host|model``); None returns a model to its default."""
        with _lock:
            config = self._load()
            visibility = config["ai"]["models"]["visibility"]
            for key, visible in changes.items():
                host, separator, model = key.partition("|")
                if not separator or not HOST_RE.fullmatch(host) or not MODEL_ID_RE.fullmatch(model):
                    raise AIProviderError("Unknown model.", code="invalid_model")
                if visible is None:
                    visibility.pop(key, None)
                elif isinstance(visible, bool):
                    visibility[key] = visible
                else:
                    raise AIProviderError("Visibility must be true, false or null.", code="invalid_model")
            self.config.save(config)
            return dict(visibility)

    def default_model(self) -> dict[str, Any] | None:
        with _lock:
            value = self._load()["ai"].get("default_model")
        try:
            return validate_model_ref(value) if value else None
        except AIProviderError:
            return None

    def set_default_model(self, value: Any) -> dict[str, Any] | None:
        ref = validate_model_ref(value) if value is not None else None
        with _lock:
            config = self._load()
            config["ai"]["default_model"] = ref
            self.config.save(config)
        return ref

    def rating_auto_enabled(self) -> bool:
        with _lock:
            return bool(self._load()["ai"]["defaults"].get("rating_auto_enabled"))

    def set_rating_auto_enabled(self, enabled: bool) -> None:
        with _lock:
            config = self._load()
            config["ai"]["defaults"]["rating_auto_enabled"] = bool(enabled)
            self.config.save(config)
