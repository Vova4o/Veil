"""OpenCode installations and the command dialect each one speaks.

MediaStudio exclusively supports OpenCode generation 2 (bundled with OpenCode desktop,
e.g. ``resources/opencode-cli.exe``). Generation 1 (npm CLI) is deprecated and
unsupported due to heavy startup latency, lack of reasoning variants, and slow CLI execution.

OpenCode Zen free models answer only the stock OpenCode agent with its stock tools:
a custom agent or a tool-restricted one gets ``FreeTierError`` ("free tier can only
be used from within OpenCode"). Zen runs therefore use the built-in ``plan`` agent,
while other providers keep MediaStudio's tool-denied agent. Every run gets an empty
``XDG_CONFIG_HOME``, so the user's global OpenCode config (MCP servers, plugins,
custom agents, skills) never reaches a MediaStudio task; credentials live in the data
directory and still apply. This matters in practice: a global plugin such as Forma
hooks every prompt (it appends its own preferences and offers ComfyUI tools), and
the desktop background service always runs with it loaded. MediaStudio runs therefore never
use the background service (``--standalone``); only the read-only model listing does.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import cli

ZEN_PROVIDER = "opencode"
STOCK_AGENT = "plan"
InstallationKind = Literal["cli", "desktop"]

# "1.18.34" from the npm CLI, "opencode v2.0.24" from the desktop CLI.
_VERSION_RE = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?:\.(\d+))?")
_MODEL_HEADER_RE = re.compile(r"([^\s{}\[\]]+/[^\s{}\[\]]+)[ \t]*\r?\n")
_version_cache: dict[tuple[str, float], str | None] = {}


@dataclass(frozen=True)
class OpenCodeInstallation:
    kind: InstallationKind
    executable: str
    version: str | None
    generation: int

    @property
    def label(self) -> str:
        name = "OpenCode desktop" if self.kind == "desktop" else "OpenCode CLI"
        return f"{name} {self.version}" if self.version else name

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "executable": self.executable,
            "version": self.version,
            "generation": self.generation,
            "label": self.label,
        }


@dataclass(frozen=True)
class OpenCodeModel:
    id: str
    provider: str
    name: str
    vision: bool | None
    free: bool | None
    # Reasoning levels the model offers (``provider/model#variant``), from its card.
    variants: tuple[str, ...] = ()
    # The model family and release time (ms since epoch) choose the default model list.
    family: str | None = None
    released: int | None = None
    # What the model produces besides text ("image", "audio"): a media model, not a chat model.
    media_outputs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "name": self.name,
            "vision": self.vision,
            "free": self.free,
            "variants": list(self.variants),
            "family": self.family,
            "released": self.released,
            "media_outputs": list(self.media_outputs),
        }


MODEL_LIST_OUTPUT_LIMIT = 4_000_000
VARIANT_SEPARATOR = "#"
_VARIANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")


# --- installations -----------------------------------------------------------------


def desktop_cli_candidates() -> list[Path]:
    """Where the desktop app keeps its bundled CLI on each platform."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return []
        programs = Path(local) / "Programs"
        return [
            programs / "@opencode-aidesktop" / "resources" / "opencode-cli.exe",
            programs / "OpenCode" / "resources" / "opencode-cli.exe",
            Path(local) / "OpenCode" / "opencode-cli.exe",
        ]
    if sys.platform == "darwin":
        bundles = [Path("/Applications/OpenCode.app"), Path.home() / "Applications" / "OpenCode.app"]
        return [
            bundle / "Contents" / folder / "opencode-cli"
            for bundle in bundles
            for folder in ("Resources", "MacOS")
        ]
    return [Path("/opt/OpenCode/resources/opencode-cli"), Path("/usr/lib/opencode/resources/opencode-cli")]


def installation_kind(executable: str | os.PathLike[str]) -> InstallationKind:
    return "desktop" if Path(executable).stem.casefold() == "opencode-cli" else "cli"


def parse_version(text: str) -> str | None:
    match = _VERSION_RE.search(text or "")
    if match is None:
        return None
    return ".".join(part for part in match.groups() if part is not None)


def read_version(executable: str) -> str | None:
    """``--version`` of one executable, cached until the file changes."""
    try:
        mtime = Path(executable).stat().st_mtime
    except OSError:
        mtime = 0.0
    key = (str(executable), mtime)
    if key not in _version_cache:
        try:
            result = cli.run_command([str(executable), "--version"], timeout=10, max_timeout=20)
        except cli.CLIIntegrationError:
            version = None
        else:
            version = parse_version(f"{result.stdout}\n{result.stderr}")
        _version_cache[key] = version
    return _version_cache[key]


def generation_for(executable: str, version: str | None = None) -> int:
    """1 for the classic CLI, 2 for the CLI with a background service."""
    version = version if version is not None else read_version(executable)
    if version:
        return 2 if int(version.split(".", 1)[0]) >= 2 else 1
    return 2 if installation_kind(executable) == "desktop" else 1


def _version_key(installation: OpenCodeInstallation) -> tuple[int, ...]:
    if not installation.version:
        return (-1,)
    return tuple(int(part) for part in installation.version.split("."))


def find_installations() -> list[OpenCodeInstallation]:
    """Every OpenCode 2+ desktop CLI found on this machine, newest version first."""
    found: list[tuple[InstallationKind, str]] = []
    for candidate in desktop_cli_candidates():
        try:
            if candidate.is_file():
                found.append(("desktop", str(candidate.resolve())))
        except OSError:
            continue
    # If the user explicitly has an opencode binary in PATH or desktop link, check if it's generation 2
    npm_cli = cli.find_executable("opencode")
    if npm_cli:
        v = read_version(npm_cli)
        if generation_for(npm_cli, v) >= 2:
            found.append((installation_kind(npm_cli), npm_cli))
    installations: list[OpenCodeInstallation] = []
    seen: set[str] = set()
    for kind, executable in found:
        if executable.casefold() in seen:
            continue
        seen.add(executable.casefold())
        version = read_version(executable)
        gen = generation_for(executable, version)
        if gen < 2:
            continue
        installations.append(OpenCodeInstallation(
            kind=kind,
            executable=executable,
            version=version,
            generation=gen,
        ))
    installations.sort(key=_version_key, reverse=True)
    return installations


def resolve_executable(requested: str | None = None) -> str | None:
    """The profile's executable when it exists, else the newest installation."""
    if requested:
        explicit = Path(requested).expanduser()
        if explicit.is_file():
            return str(explicit.resolve())
    installations = find_installations()
    return installations[0].executable if installations else None


# --- runs --------------------------------------------------------------------------


def uses_stock_agent(model: str) -> bool:
    """Zen models (the free tier included) need the stock OpenCode agent."""
    return str(model).split("/", 1)[0].strip().casefold() == ZEN_PROVIDER


def isolated_env(config_home: Path) -> dict[str, str | None]:
    """Hide the user's global OpenCode config from a MediaStudio run."""
    return {"XDG_CONFIG_HOME": str(config_home)}


def build_run_command(
    executable: str,
    *,
    generation: int = 2,
    model: str,
    agent: str,
    title: str,
    message: str,
    files: list[Path] | tuple[Path, ...] = (),
) -> list[str]:
    # Generation 2+: --standalone: a private server that sees the isolated environment; the
    # background service has the user's global config already loaded.
    args = [
        executable, "run", "--standalone",
        "--model", model, "--format", "json", "--agent", agent, "--title", title,
    ]
    for path in files:
        args.extend(["--file", str(path)])
    args.append(message)
    return args


def event_texts(output: str) -> list[str]:
    """Assistant text parts of a ``--format json`` event stream, in order."""
    parts: list[str] = []
    for line in (output or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part")
        text = part.get("text") if isinstance(part, dict) else None
        if not isinstance(text, str):
            text = event.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return parts


def event_error(output: str) -> str | None:
    """The message of the first error event, if the run reported one."""
    for line in (output or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "error":
            continue
        error = event.get("error")
        if isinstance(error, dict):
            data = error.get("data")
            message = data.get("message") if isinstance(data, dict) else None
            message = message or error.get("message") or error.get("name")
            if isinstance(message, str) and message:
                return message
        return "OpenCode reported an error."
    return None


def is_free_tier_refusal(output: str) -> bool:
    lowered = (output or "").casefold()
    return "freetiererror" in lowered or "free tier can only be used" in lowered


FREE_TIER_HINT = (
    "OpenCode refused the free-tier request. Free Zen models work only through the "
    "stock OpenCode agent; update OpenCode or pick another model."
)


# --- models ------------------------------------------------------------------------


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _is_free_cost(cost: Any) -> bool | None:
    tiers = cost if isinstance(cost, list) else [cost] if isinstance(cost, dict) else []
    tiers = [tier for tier in tiers if isinstance(tier, dict)]
    if not tiers:
        return None
    return all(not tier.get("input") and not tier.get("output") for tier in tiers)


def _variant_ids(variants: Any) -> tuple[str, ...]:
    """Variant names from a model card: a list of ``{"id": ...}`` or a mapping by name."""
    if isinstance(variants, dict):
        names = variants.keys()
    elif isinstance(variants, list):
        names = (item.get("id") if isinstance(item, dict) else item for item in variants)
    else:
        return ()
    return tuple(str(name) for name in names if isinstance(name, str) and _VARIANT_RE.fullmatch(name))


def _vision_from_capabilities(capabilities: Any) -> bool | None:
    if not isinstance(capabilities, dict):
        return None
    inputs = capabilities.get("input")
    if isinstance(inputs, dict):
        return _bool_or_none(inputs.get("image"))
    if isinstance(inputs, list):
        return "image" in inputs
    return _bool_or_none(capabilities.get("attachment"))


def parse_verbose_models(output: str) -> list[OpenCodeModel]:
    """Parse ``opencode models --verbose`` (generation 1): a header line, then JSON."""
    decoder = json.JSONDecoder()
    models: list[OpenCodeModel] = []
    position = 0
    while position < len(output):
        header = _MODEL_HEADER_RE.match(output, position)
        if header is None:
            newline = output.find("\n", position)
            if newline < 0:
                break
            position = newline + 1
            continue
        model_id = header.group(1)
        position = header.end()
        try:
            info, position = decoder.raw_decode(output, position)
        except json.JSONDecodeError:
            info = {}
        if not isinstance(info, dict):
            info = {}
        provider = model_id.split("/", 1)[0]
        models.append(OpenCodeModel(
            id=model_id,
            provider=provider,
            name=str(info.get("name") or model_id.split("/", 1)[1]),
            vision=_vision_from_capabilities(info.get("capabilities")),
            free=_is_free_cost(info.get("cost")),
            variants=_variant_ids(info.get("variants")),
        ))
    return models


def parse_plain_models(output: str) -> list[OpenCodeModel]:
    models: list[OpenCodeModel] = []
    for line in (output or "").splitlines():
        value = line.strip().lstrip("-*•│| ").strip()
        if re.fullmatch(r"[^\s/]+/[^\s]+", value):
            provider, name = value.split("/", 1)
            models.append(OpenCodeModel(id=value, provider=provider, name=name, vision=None, free=None))
    return models


def parse_api_models(payload: Any) -> list[OpenCodeModel]:
    """Parse ``api GET /api/model`` (generation 2)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    models: list[OpenCodeModel] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        provider = str(item.get("providerID") or "").strip()
        model = str(item.get("id") or item.get("modelID") or "").strip()
        if not provider or not model:
            continue
        models.append(OpenCodeModel(
            id=f"{provider}/{model}",
            provider=provider,
            name=str(item.get("name") or model),
            vision=_vision_from_capabilities(item.get("capabilities")),
            free=_is_free_cost(item.get("cost")),
            variants=_variant_ids(item.get("variants")),
            family=item.get("family") if isinstance(item.get("family"), str) else None,
            released=_released_ms(item.get("time")),
            media_outputs=_media_outputs(item.get("capabilities")),
        ))
    return models


def _media_outputs(capabilities: Any) -> tuple[str, ...]:
    outputs = capabilities.get("output") if isinstance(capabilities, dict) else None
    if not isinstance(outputs, list):
        return ()
    return tuple(str(item) for item in outputs if isinstance(item, str) and item != "text")


def _released_ms(value: Any) -> int | None:
    released = value.get("released") if isinstance(value, dict) else None
    return int(released) if isinstance(released, int | float) and released > 0 else None


def _listing_error(result: cli.CommandResult) -> cli.CLIIntegrationError:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return cli.CLIIntegrationError(output or "OpenCode could not list models.", code="cli_authentication")


def list_models(
    executable: str,
    *,
    generation: int | None = None,
    provider: str | None = None,
) -> list[OpenCodeModel]:
    """Models this installation can run, with vision and free flags when known."""
    models = _list_api_models(executable)
    return [model for model in models if not provider or model.provider == provider]


def _list_api_models(executable: str) -> list[OpenCodeModel]:
    with tempfile.TemporaryDirectory(prefix="mediastudio-opencode-models-", ignore_cleanup_errors=True) as location:
        args = [executable, "api", "GET", "/api/model", "--param", f"location[directory]={location}"]
        for attempt in range(2):
            result = cli.run_command(args, timeout=60, output_limit=MODEL_LIST_OUTPUT_LIMIT)
            if result.returncode != 0:
                raise _listing_error(result)
            try:
                models = parse_api_models(json.loads(result.stdout))
            except json.JSONDecodeError as exc:
                raise cli.CLIIntegrationError(
                    "OpenCode returned an unreadable model list.", code="incompatible_format"
                ) from exc
            if models or attempt:
                return models
            # A location that has just booted can answer with an empty list once.
            time.sleep(1.5)
    return []


__all__ = [
    "FREE_TIER_HINT",
    "STOCK_AGENT",
    "ZEN_PROVIDER",
    "OpenCodeInstallation",
    "OpenCodeModel",
    "build_run_command",
    "desktop_cli_candidates",
    "event_error",
    "event_texts",
    "find_installations",
    "generation_for",
    "installation_kind",
    "is_free_tier_refusal",
    "isolated_env",
    "list_models",
    "parse_api_models",
    "parse_plain_models",
    "parse_verbose_models",
    "parse_version",
    "read_version",
    "resolve_executable",
    "uses_stock_agent",
]
