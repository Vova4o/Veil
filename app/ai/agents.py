"""Models the user can run, across local AI tools and API providers (Integrations › Models).

A model is addressed by ``{"host", "model", "variant"}``: the host is a local tool
(``opencode``, ``claude``, ``codex``, ``antigravity``) or ``api:<provider id>``.
``catalog`` lists every model of the connected providers with its card and whether
model pickers show it; ``resolve`` checks one reference against the host's own
catalog and returns the execution profile the ``ExecutionRouter`` runs.

Photo support comes from the host: OpenCode cards report image input, Claude Code,
Codex and Antigravity models see photos (GPT-OSS on Antigravity excepted), and API
providers use the models.dev card of the model when there is one. A model without a
card is tried with the photo; the provider reports it if it cannot read images.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import catalog_cache, opencode_host, provider_catalog
from .cli import CLIIntegrationError, find_executable, run_command
from .model_catalog import family_vision, native_cli_models
from .providers import LOCAL_TOOLS, AIProviderError, AIProviderStore
from .transport import AIProviderRequestError, list_openai_compatible_models

logger = logging.getLogger(__name__)

HOST_LABELS = {"opencode": "OpenCode", "claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity"}
HOST_ICONS = {"opencode": "opencode", "claude": "anthropic", "codex": "openai", "antigravity": "antigravity"}
OPENCODE_PROVIDER_LABELS = {"opencode": "OpenCode Zen", "opencode-go": "OpenCode Go", "google-vertex": "Vertex"}
# OpenCode's own subscriptions: every model is shown until the user hides it.
OPENCODE_HOME_PROVIDERS = {"opencode", "opencode-go"}
# Agent CLIs need time to read a long task package and an image.
DEFAULT_TIMEOUT_SECONDS = 240
# Like OpenCode: newer models of a family replace older ones in the pickers.
RECENT_RELEASE = timedelta(days=183)


class AgentError(RuntimeError):
    def __init__(self, message: str, *, code: str, status: int = 409, technical_error: str | None = None):
        self.code = code
        self.status = status
        self.technical_error = technical_error
        super().__init__(message)


def model_key(host: str, model: str) -> str:
    return f"{host}|{model}"


# --- OpenCode ----------------------------------------------------------------------

IMPLICIT_OPENCODE_PROFILE_ID = "implicit-opencode-free"
# Free Zen model families in order of preference; ids change between releases.
PREFERRED_FREE_MODELS = ("muse-spark", "mimo", "exo", "longcat", "space-bunny", "step")
_FREE_MODEL_TTL_SECONDS = 15 * 60
_free_model_cache: dict[str, tuple[float, opencode_host.OpenCodeModel | None]] = {}
_models_cache = catalog_cache.memory


def _opencode_card(card: dict[str, Any]) -> opencode_host.OpenCodeModel:
    return opencode_host.OpenCodeModel(
        id=card["id"], provider=card["provider"], name=card["name"], vision=card.get("vision"),
        free=card.get("free"), variants=tuple(card.get("variants") or ()),
        family=card.get("family"), released=card.get("released"),
        media_outputs=tuple(card.get("media_outputs") or ()),
    )


def opencode_models(executable: str, *, refresh: bool = False) -> list[opencode_host.OpenCodeModel]:
    """Every model one installation can run, with its card (vision, free, variants); cached."""
    cached = catalog_cache.load("opencode", executable) if not refresh else None
    if cached:
        return [_opencode_card(card) for card in cached]
    models = opencode_host.list_models(executable)
    catalog_cache.save("opencode", executable, [model.to_dict() for model in models])
    return models


def default_free_model(executable: str) -> opencode_host.OpenCodeModel | None:
    """The preferred free Zen model of one installation, vision models first."""
    cached = _free_model_cache.get(executable)
    if cached and time.monotonic() - cached[0] < _FREE_MODEL_TTL_SECONDS:
        return cached[1]
    try:
        cards = catalog_cache.load("opencode", executable) or catalog_cache.load("opencode-zen", executable)
        if cards:
            models = [_opencode_card(card) for card in cards if card.get("provider") == opencode_host.ZEN_PROVIDER]
        else:
            models = opencode_host.list_models(executable, provider=opencode_host.ZEN_PROVIDER)
            catalog_cache.save("opencode-zen", executable, [model.to_dict() for model in models])
    except CLIIntegrationError as exc:
        logger.info("OpenCode model list failed for %s: %s", executable, exc)
        models = []
    free = [model for model in models if model.free]

    def rank(model: opencode_host.OpenCodeModel) -> tuple[int, int]:
        name = model.id.split("/", 1)[-1]
        family = next(
            (index for index, prefix in enumerate(PREFERRED_FREE_MODELS) if name.startswith(prefix)),
            len(PREFERRED_FREE_MODELS),
        )
        return (0 if model.vision else 1, family)

    chosen = min(free, key=rank) if free else None
    _free_model_cache[executable] = (time.monotonic(), chosen)
    return chosen


def opencode_profile(model_id: str, variant: str | None = None) -> dict[str, Any]:
    """The run of one OpenCode model; its card says whether it sees photos."""
    installations = opencode_host.find_installations()
    if not installations:
        raise AgentError("OpenCode is not installed.", code="cli_unavailable")
    installation = installations[0]
    try:
        models = opencode_models(installation.executable)
    except CLIIntegrationError as exc:
        raise AgentError("OpenCode could not list its models.", code="cli_unavailable", technical_error=str(exc)) from exc
    model = next((item for item in models if item.id == model_id), None)
    if model is None:
        raise AgentError(f"OpenCode has no model '{model_id}'.", code="model_unavailable")
    if variant and variant not in model.variants:
        raise AgentError(f"{model.name} has no reasoning level '{variant}'.", code="variant_unavailable", status=400)
    run_model = f"{model.id}{opencode_host.VARIANT_SEPARATOR}{variant}" if variant else model.id
    return {
        "id": f"opencode:{run_model}",
        "model_card": True,
        "kind": "cli",
        "cli_type": "opencode",
        "name": " · ".join(["OpenCode", model.name, *([variant] if variant else [])]),
        "model": run_model,
        "multimodal": bool(model.vision),
        "roles": ["text", "vision"] if model.vision else ["text"],
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "executable": installation.executable,
    }


def implicit_opencode_profile() -> dict[str, Any] | None:
    """A zero-setup agent: the newest OpenCode installation with a free Zen model."""
    installations = opencode_host.find_installations()
    if not installations:
        return None
    installation = installations[0]
    model = default_free_model(installation.executable)
    if model is None:
        return None
    return {
        "id": IMPLICIT_OPENCODE_PROFILE_ID,
        "model_card": True,
        "kind": "cli",
        "cli_type": "opencode",
        "name": f"OpenCode · {model.name}",
        "model": model.id,
        "multimodal": bool(model.vision),
        "roles": ["text", "vision"] if model.vision else ["text"],
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "executable": installation.executable,
    }


# --- Antigravity ---------------------------------------------------------------------

_AGY_LEVEL = re.compile(r"^(?P<base>.+?)-(?P<level>minimal|low|medium|high|xhigh|max)$")
_AGY_NAME_LEVEL = re.compile(r"^(?P<name>.+?)\s*\((?P<level>[^()]+)\)$")
_AGY_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")


def _tool_vision(model: str) -> bool:
    """Claude Code, Codex and Antigravity models see photos; GPT-OSS is text only."""
    return family_vision(model) is not False


def parse_antigravity_models(output: str) -> list[dict[str, Any]]:
    """``agy models``: one ``id<TAB>Name (Level)`` line per model and reasoning level.

    Levels of one model are grouped: ``gemini-3.8-flash-high`` is Gemini 3.8 Flash at high.
    """
    groups: dict[str, dict[str, Any]] = {}
    for line in (output or "").splitlines():
        if "\t" not in line:
            continue
        model_id, label = (part.strip() for part in line.split("\t", 1))
        if not _AGY_MODEL_ID.fullmatch(model_id):
            continue
        level_match = _AGY_LEVEL.match(model_id)
        base, level = (level_match["base"], level_match["level"]) if level_match else (model_id, None)
        name_match = _AGY_NAME_LEVEL.match(label)
        group = groups.setdefault(base, {
            "id": base,
            "name": (name_match["name"] if name_match else label) or base,
            "vision": _tool_vision(base),
            "levels": [],
        })
        if level and not any(item["id"] == model_id for item in group["levels"]):
            group["levels"].append({"id": model_id, "level": level})
        else:
            group["model"] = model_id
    return list(groups.values())


def antigravity_models(*, refresh: bool = False) -> tuple[str | None, list[dict[str, Any]]]:
    """Antigravity's executable and its models (cached); no executable when it is not installed."""
    executable = find_executable("antigravity")
    if executable is None:
        return None, []
    cached = catalog_cache.load("antigravity", executable) if not refresh else None
    if cached:
        return executable, cached
    result = run_command([executable, "models"], timeout=30, max_timeout=30)
    if result.returncode != 0:
        raise CLIIntegrationError(result.stderr or result.stdout or "Antigravity could not list its models.")
    models = parse_antigravity_models(result.stdout)
    if not models:
        raise CLIIntegrationError("Antigravity returned no readable model catalog.")
    catalog_cache.save("antigravity", executable, models)
    return executable, models


def _antigravity_level(model: dict[str, Any], level: str | None = None) -> dict[str, Any] | None:
    levels = model.get("levels") or []
    return (
        next((item for item in levels if item["level"] == level), None)
        or next((item for item in levels if item["level"] == "medium"), None)
        or (levels[0] if levels else None)
    )


def antigravity_profile(model_id: str, variant: str | None = None) -> dict[str, Any]:
    """The run of one Antigravity model; the id may carry its reasoning level."""
    try:
        executable, models = antigravity_models()
    except CLIIntegrationError as exc:
        raise AgentError("Antigravity could not list its models.", code="cli_unavailable",
                         technical_error=str(exc)) from exc
    if executable is None:
        raise AgentError("Antigravity is not installed.", code="cli_unavailable")
    for model in models:
        levels = {item["id"]: item["level"] for item in model["levels"]}
        if model_id == model["id"] and model_id not in levels and not model.get("model"):
            # A base model picked without a level: run its chosen (or medium) level.
            chosen = _antigravity_level(model, variant)
            if chosen is None:
                continue
            model_id = chosen["id"]
        if model_id == model.get("model") or model_id in levels:
            level = levels.get(model_id)
            vision = model.get("vision") is not False
            return {
                "id": f"antigravity:{model_id}",
                "model_card": True,
                "kind": "cli",
                "cli_type": "antigravity",
                "name": " · ".join(["Antigravity", model["name"], *([level] if level else [])]),
                "model": model_id,
                "multimodal": vision,
                "roles": ["text", "vision"] if vision else ["text"],
                "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                "executable": executable,
            }
    raise AgentError(f"Antigravity has no model '{model_id}'.", code="model_unavailable")


# --- Claude Code, Codex ----------------------------------------------------------------


def native_profile(host: str, model_id: str, variant: str | None = None) -> dict[str, Any]:
    """Check a Claude Code or Codex model against the host's own catalog."""
    label = HOST_LABELS[host]
    try:
        executable, models = native_cli_models(host)
    except CLIIntegrationError as exc:
        raise AgentError(f"{label} could not list its models.", code="cli_unavailable",
                         technical_error=str(exc)) from exc
    if executable is None:
        raise AgentError(f"{label} is not installed.", code="cli_unavailable")
    model = next((item for item in models if item["id"] == model_id), None)
    if model is None:
        raise AgentError(f"{label} has no model '{model_id}'.", code="model_unavailable")
    if variant and variant not in model["variants"]:
        raise AgentError(f"{model['name']} has no reasoning level '{variant}'.", code="variant_unavailable", status=400)
    vision = model.get("vision") is not False and _tool_vision(model_id)
    return {
        "id": f"{host}:{model_id}", "model_card": True, "kind": "cli", "cli_type": host,
        "name": " · ".join([label, model["name"], *([variant] if variant else [])]),
        "model": model_id, "multimodal": vision, "reasoning_effort": variant,
        "roles": ["text", "vision"] if vision else ["text"],
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS, "executable": executable,
    }


# --- API providers --------------------------------------------------------------------

_provider_models_cache = catalog_cache.memory
_FREE_ID = re.compile(r"[:-]free$", re.IGNORECASE)


def provider_models(store: AIProviderStore, provider: dict[str, Any], *, refresh: bool = False) -> tuple[
        list[dict[str, Any]], str | None]:
    """The models of one API provider: its ``/models`` list with models.dev cards.

    Without a reachable list, the manual models and the template's models remain.
    """
    cards = provider_catalog.model_cards(provider.get("template"))
    manual = {item["id"]: item for item in provider.get("models") or []}
    cache_key = f"api|{provider['id']}|{provider['base_url']}"
    ids: list[str] | None = None
    error = None
    cached = _provider_models_cache.get(cache_key)
    if cached and not refresh and time.time() - cached["saved_at"] < catalog_cache.TTL_SECONDS:
        ids = cached["ids"]
    else:
        try:
            profile = provider_profile_base(provider)
            ids = list_openai_compatible_models(provider["base_url"], api_key=store.resolve_api_key(profile))
            _provider_models_cache[cache_key] = {"saved_at": time.time(), "ids": ids}
        except (AIProviderRequestError, AIProviderError) as exc:
            error = str(exc)
    if ids is None:
        ids = list(manual) + [model_id for model_id in cards if model_id not in manual]
    models = []
    for model_id in dict.fromkeys([*manual, *ids]):
        card = cards.get(model_id) or {}
        models.append({
            "id": model_id,
            "name": (manual.get(model_id) or {}).get("name") or card.get("name") or model_id,
            "vision": card.get("vision") if card else None,
            # Only an explicit free variant is free; a zero price may be an unknown one.
            "free": bool(_FREE_ID.search(model_id)),
            "family": card.get("family"),
            "released": card.get("released"),
            "manual": model_id in manual,
        })
    return models, error


def provider_profile_base(provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "openai_compatible",
        "provider_id": provider["id"],
        "provider_name": provider.get("name"),
        "base_url": provider["base_url"],
        "api_key_source": provider.get("api_key_source"),
        "api_key_env": provider.get("api_key_env"),
        "extra_body": {},
    }


def provider_profile(store: AIProviderStore, provider_id: str, model_id: str) -> dict[str, Any]:
    try:
        provider = store.get(provider_id)
    except AIProviderError as exc:
        raise AgentError(str(exc), code=exc.code, status=404 if exc.code == "provider_not_found" else 409) from exc
    models, error = provider_models(store, provider)
    model = next((item for item in models if item["id"] == model_id), None)
    if model is None and (error or not models):
        # Without the provider's own list, an unknown id is left for the provider to judge.
        model = {"id": model_id, "name": model_id, "vision": None}
    if model is None:
        raise AgentError(f"{provider['name']} has no model '{model_id}'.", code="model_unavailable")
    vision = model.get("vision") is not False
    return {
        **provider_profile_base(provider),
        "id": f"api:{provider_id}:{model_id}",
        "model_card": True,
        "name": f"{provider['name']} · {model['name']}",
        "model": model_id,
        "multimodal": vision,
        "roles": ["text", "vision"] if vision else ["text"],
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    }


# --- resolution --------------------------------------------------------------------


def resolve(store: AIProviderStore, ref: dict[str, Any]) -> dict[str, Any]:
    """The execution profile of one ``{"host", "model", "variant"}`` reference."""
    host, model, variant = ref["host"], ref["model"], ref.get("variant")
    if host in LOCAL_TOOLS and host in store.disconnected_tools():
        raise AgentError(f"{HOST_LABELS[host]} is disconnected in Integrations.", code="cli_unavailable")
    if host == "opencode":
        return opencode_profile(model, variant)
    if host == "antigravity":
        return antigravity_profile(model, variant)
    if host in {"codex", "claude"}:
        return native_profile(host, model, variant)
    if host.startswith("api:"):
        return provider_profile(store, host.removeprefix("api:"), model)
    raise AgentError("Unknown model host.", code="invalid_model", status=400)


def sees_images(profile: dict[str, Any]) -> bool:
    """Whether a task may send this run the photo (before the adapter's own input check)."""
    if profile.get("model_card"):
        return profile.get("multimodal") is True
    if profile.get("kind") == "cli" and profile.get("cli_type") in {"codex", "claude", "antigravity"}:
        return _tool_vision(str(profile.get("model") or ""))
    return profile.get("multimodal") is True


def default_profile(store: AIProviderStore, *, wants_image: bool) -> dict[str, Any] | None:
    """The run where no model was picked: the default model, else a free OpenCode model.

    A photo task with a text-only default model prefers the free OpenCode vision model
    and otherwise still runs the default model without the photo.
    """
    chosen = None
    ref = store.default_model()
    if ref:
        try:
            chosen = resolve(store, ref)
        except AgentError as exc:
            logger.info("The default model is unavailable: %s", exc)
    if chosen is not None and (not wants_image or sees_images(chosen)):
        return chosen
    implicit = implicit_opencode_profile()
    if implicit is not None and (chosen is None or sees_images(implicit)):
        return implicit
    return chosen


def profile_for_request(store: AIProviderStore, ref: dict[str, Any] | None, *, wants_image: bool) -> dict[str, Any]:
    """The picked model, else the default; an error when nothing can run."""
    profile = resolve(store, ref) if ref else default_profile(store, wants_image=wants_image)
    if profile is None:
        raise AgentError(
            "No AI model is available. Connect a provider in Integrations › Providers.",
            code="no_ai_model",
        )
    return profile


# --- catalog -----------------------------------------------------------------------


def _released(value: Any) -> date | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _recent_latest(models: list[dict[str, Any]]) -> set[str]:
    """Per family, the newest model when it is recent (OpenCode's default model list)."""
    today = datetime.now(timezone.utc).date()
    newest: dict[str, tuple[date, str]] = {}
    for model in models:
        released = _released(model.get("released"))
        family = model.get("family") or model["id"]
        if released and abs((today - released).days) <= RECENT_RELEASE.days:
            if family not in newest or released > newest[family][0]:
                newest[family] = (released, model["id"])
    return {model_id for _, model_id in newest.values()}


def _apply_visibility(group: dict[str, Any], models: list[dict[str, Any]], visibility: dict[str, bool],
                      *, show_all: bool) -> None:
    latest = set() if show_all else _recent_latest([model for model in models if not model.get("media")])
    for model in models:
        default = not model.get("media") and (
            show_all or model.get("manual") or model["id"] in latest or _released(model.get("released")) is None
        )
        model["key"] = model_key(group["host"], model["id"])
        model["default_visible"] = bool(default)
        model["visible"] = visibility.get(model["key"], bool(default))
    group["models"] = models


def _opencode_groups(refresh: bool) -> list[dict[str, Any]]:
    installations = opencode_host.find_installations()
    if not installations:
        return []
    installation = installations[0]
    try:
        cards = [model.to_dict() for model in opencode_models(installation.executable, refresh=refresh)]
    except CLIIntegrationError as exc:
        return [{"id": "opencode", "host": "opencode", "kind": "tool", "label": "OpenCode", "icon": "opencode",
                 "source": installation.label, "error": str(exc), "models": []}]
    groups: dict[str, dict[str, Any]] = {}
    for card in cards:
        provider = card["provider"]
        template = provider_catalog.template(provider)
        group = groups.setdefault(provider, {
            "id": f"opencode:{provider}", "host": "opencode", "provider": provider, "kind": "tool",
            "label": OPENCODE_PROVIDER_LABELS.get(provider) or (template or {}).get("name") or provider,
            "icon": "opencode" if provider in OPENCODE_HOME_PROVIDERS else provider,
            "source": "OpenCode", "error": None, "models": [],
        })
        group["models"].append({
            "id": card["id"], "name": card["name"], "vision": card.get("vision"),
            # A zero price elsewhere may only mean an unknown or per-request price.
            "free": bool(card.get("free")) and provider in OPENCODE_HOME_PROVIDERS,
            "variants": list(card.get("variants") or []), "family": card.get("family"),
            "released": card.get("released"), "media": bool(card.get("media_outputs")),
        })
    order = sorted(groups.values(), key=lambda item: (item["provider"] not in OPENCODE_HOME_PROVIDERS, item["label"]))
    return order


def _antigravity_groups(refresh: bool) -> list[dict[str, Any]]:
    group = {"id": "antigravity", "host": "antigravity", "kind": "tool", "label": "Antigravity",
             "icon": "antigravity", "source": None, "error": None, "models": []}
    try:
        executable, models = antigravity_models(refresh=refresh)
    except CLIIntegrationError as exc:
        return [{**group, "error": str(exc)}]
    if executable is None:
        return []
    group["models"] = [{
        "id": model["id"], "name": model["name"], "vision": model.get("vision") is not False,
        "levels": model.get("levels") or [], "model": model.get("model"),
    } for model in models]
    return [group]


def _native_groups(host: str, refresh: bool) -> list[dict[str, Any]]:
    group = {"id": host, "host": host, "kind": "tool", "label": HOST_LABELS[host], "icon": HOST_ICONS[host],
             "source": None, "error": None, "models": []}
    try:
        executable, models = native_cli_models(host, refresh=refresh)
    except CLIIntegrationError as exc:
        return [{**group, "error": str(exc)}]
    if executable is None:
        return []
    group["models"] = [{
        "id": model["id"], "name": model["name"],
        "vision": model.get("vision") is not False and _tool_vision(model["id"]),
        "variants": list(model.get("variants") or []), "default_variant": model.get("default_variant"),
    } for model in models]
    return [group]


def _provider_groups(store: AIProviderStore, provider: dict[str, Any], refresh: bool) -> list[dict[str, Any]]:
    models, error = provider_models(store, provider, refresh=refresh)
    return [{
        "id": f"api:{provider['id']}", "host": f"api:{provider['id']}", "provider": provider["id"], "kind": "api",
        "label": provider["name"], "icon": provider.get("template") or "custom", "source": None,
        "error": error, "models": models,
    }]


def catalog(store: AIProviderStore, *, refresh: bool = False, visible_only: bool = False) -> dict[str, Any]:
    """Every model of the connected providers, grouped as Integrations › Models shows them.

    Hosts are listed in parallel and each keeps its own error.
    """
    disconnected = store.disconnected_tools()
    jobs: list[tuple[str, Any, tuple[Any, ...]]] = []
    if "opencode" not in disconnected:
        jobs.append(("opencode", _opencode_groups, (refresh,)))
    for host in ("claude", "codex"):
        if host not in disconnected:
            jobs.append((host, _native_groups, (host, refresh)))
    if "antigravity" not in disconnected:
        jobs.append(("antigravity", _antigravity_groups, (refresh,)))
    for provider in store.providers():
        jobs.append((f"api:{provider['id']}", _provider_groups, (store, provider, refresh)))
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(jobs)))) as pool:
        futures = [(name, pool.submit(function, *args)) for name, function, args in jobs]
        groups: list[dict[str, Any]] = []
        for name, future in futures:
            try:
                groups.extend(future.result())
            except Exception as exc:  # one host must never hide the others
                logger.warning("Model catalog of %s failed: %s", name, exc)
    visibility = store.visibility()
    for group in groups:
        show_all = group["kind"] == "tool" and group.get("provider") in (None, *OPENCODE_HOME_PROVIDERS)
        _apply_visibility(group, group["models"], visibility, show_all=show_all)
        if visible_only:
            group["models"] = [model for model in group["models"] if model["visible"]]
    if visible_only:
        groups = [group for group in groups if group["models"] or group.get("error")]
    return {"groups": groups, "default_model": store.default_model()}


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "HOST_LABELS",
    "IMPLICIT_OPENCODE_PROFILE_ID",
    "AgentError",
    "antigravity_models",
    "antigravity_profile",
    "catalog",
    "default_free_model",
    "default_profile",
    "implicit_opencode_profile",
    "model_key",
    "native_profile",
    "opencode_models",
    "opencode_profile",
    "parse_antigravity_models",
    "profile_for_request",
    "provider_models",
    "provider_profile",
    "resolve",
    "sees_images",
]
