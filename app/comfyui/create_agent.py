"""The Create agent: turns a Create request into a Krea 2 prompt or edit command.

The instruction lives in ``app/ai/prompting/content/create/krea2`` (a base, one
overlay per mode and the output contract). It is compiled here into an
``InstructionBundle`` and executed through the shared ``ExecutionRouter``, so every
configured agent (OpenAI-compatible, OpenCode, Claude Code, Antigravity, Codex)
runs the same task package.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.ai import catalog_cache, opencode_host
from app.ai.cli import CLIIntegrationError
from app.ai.execution import ExecutionRouter, ExecutionRouterError
from app.ai.model_catalog import family_vision, native_cli_models
from app.ai.profiles import AIProfileStore, AIProfileStoreError
from app.ai.prompting import (
    InstructionBundle,
    InstructionSection,
    PromptOperation,
    PromptScenario,
    PromptTask,
)
from app.ai.prompting.models import CapabilityStatus
from app.ai.prompting.registry import CONTENT_DIR

from .simple_profiles import ApprovedProfile, resolve_edit_profile

logger = logging.getLogger(__name__)

CreateMode = Literal["text", "reference", "variation", "edit"]
CREATE_CONTENT_DIR = CONTENT_DIR / "create" / "krea2"
CREATE_INSTRUCTION_VERSION = "3"
# Agent CLIs need time to read a long task package and an image; profile timeouts
# saved for short translations (60 s) are too tight for Create.
MIN_AGENT_TIMEOUT_SECONDS = 240
AGENT_IMAGE_MAX_SIDE = 1536
MAX_REQUEST_CHARS = 8_000
MAX_PREVIOUS_PROMPT_CHARS = 20_000

_MODE_OPERATIONS: dict[str, PromptOperation] = {
    "text": PromptOperation.GENERATE,
    "reference": PromptOperation.RECONSTRUCT,
    "variation": PromptOperation.RECONSTRUCT,
    "edit": PromptOperation.EDIT,
}


class CreateAgentError(RuntimeError):
    def __init__(self, message: str, *, code: str, status: int = 400, technical_error: str | None = None):
        self.code = code
        self.status = status
        self.technical_error = technical_error
        super().__init__(message)


class CreateInstructionBundle(InstructionBundle):
    """An instruction bundle rendered without the family/scenario task header.

    Create has one model and explicit modes; the generic header ("family: flux,
    scenario: ...") would only mislead the agent.
    """

    def render(self) -> str:
        mode = self.versions.get("mode") or next(
            (name for name, operation in _MODE_OPERATIONS.items() if operation is self.task.operation),
            self.task.operation.value,
        )
        blocks = [
            "CMV CREATE TASK\n"
            "model: Moody Krea 2 (Krea 2 family, Qwen3-VL text encoder)\n"
            f"mode: {mode}\n"
            "precedence: output contract and content boundaries first, then the mode, then the base",
        ]
        blocks.extend(
            f"SECTION {section.kind.upper()}: {section.section_id}\n{section.content}"
            for section in self.sections
        )
        return "\n\n".join(blocks).strip() + "\n"


@dataclass(frozen=True)
class CreateAgentResult:
    prompt: str
    negative_prompt: str
    edit_profile: str | None
    note: str
    agent_name: str
    adapter_id: str
    model: str | None
    saw_image: bool
    job_id: int
    latency_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "positive_prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "edit_profile": self.edit_profile,
            "note": self.note,
            "agent": {
                "name": self.agent_name,
                "adapter": self.adapter_id,
                "model": self.model,
                "saw_image": self.saw_image,
                "job_id": self.job_id,
                "latency_ms": self.latency_ms,
            },
        }


def _section(section_id: str, kind: str, content: str, source: str) -> InstructionSection:
    normalized = content.strip()
    return InstructionSection(
        section_id=section_id,
        kind=kind,
        version=CREATE_INSTRUCTION_VERSION,
        source=source,
        content_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        content=normalized,
    )


def _content(name: str) -> tuple[str, str]:
    path = CREATE_CONTENT_DIR / name
    return path.read_text(encoding="utf-8"), f"app/ai/prompting/content/create/krea2/{name}"


def _edit_profiles_section(profile: ApprovedProfile, *, references: int) -> str:
    lines = ["Available edit profiles for the attached photos (id: purpose; photos; time per image):"]
    for item in profile.edit_profiles:
        if item.references != references:
            continue
        photos = "needs 2 photos" if item.references == 2 else "1 photo"
        lines.append(f"- {item.id}: {item.description} [{photos}; about {item.seconds} s]")
    return "\n".join(lines)


def create_task(profile: ApprovedProfile, mode: CreateMode) -> PromptTask:
    return PromptTask(
        family=profile.prompt_family,
        operation=_MODE_OPERATIONS[mode],
        scenario=PromptScenario.SINGLE_CHARACTER,
        checkpoint_profile=profile.id,
        output_contract="create_krea2",
    )


_INTIMATE_RE = re.compile(
    r"\b(?:hard|nsfw|nude|nudity|naked|undress\w*|strip\w*|topless|bottomless|nipples?|areolas?|"
    r"lingerie|erotic\w*|sex\w*|porn\w*|intima\w*|boudoir|genital\w*|vagina\w*|labia\w*|anus|anal|"
    r"breasts?|boobs?|tits?|pussy|penis|masturbat\w*|orgasm\w*|"
    r"хард|ню|голая|голый|голые|голой|обнаж\w*|разде\w*|сним\w*|сня\w*|топлес|сос[оа]к\w*|"
    r"эрот\w*|секс\w*|порн\w*|интим\w*|бель[её]|трусик\w*|лифчик\w*|груд\w*|сиськ\w*|вагин\w*|"
    r"киск\w*|анал\w*|мастурб\w*|оргазм\w*)\b",
    re.IGNORECASE,
)


def wants_intimate_module(*texts: str | None) -> bool:
    """Whether the request (or the prompt being refined) asks for intimacy or exposure.

    The intimate module is loaded only then, so ordinary tasks keep a short, neutral
    instruction that hosted agents do not refuse.
    """
    return any(text and _INTIMATE_RE.search(text) for text in texts)


_ORIGINAL_RE = re.compile(
    r"(?<!\w)(?:original|оригинал(?:а|у|ом|е|ьн(?:ый|ая|ое|ые|ого|ой|ому|ым|ую|ых|ыми))?|"
    r"(?<![:：.])1\s*[:：]\s*1(?![:：.])|one[\s-]+to[\s-]+one|один[\s-]+в[\s-]+один|"
    r"как\s+(?:есть|на\s+(?:фото|исходнике))|as[\s-]+is|"
    r"без\s+(?:приукрас|прикрас|приукрашивания|идеализации|ретуши)|"
    r"(?:no|without)\s+(?:beautification|idealization|retouching))(?!\w)",
    re.IGNORECASE,
)


def wants_original_module(request: str | None) -> bool:
    """Whether the current request explicitly asks for unembellished fidelity."""
    return bool(request and _ORIGINAL_RE.search(request))


def build_create_bundle(
    profile: ApprovedProfile,
    mode: CreateMode,
    *,
    references: int = 1,
    intimate: bool = False,
    original: bool = False,
) -> CreateInstructionBundle:
    """Compile the Create instruction for one mode.

    Description and reference modes get the composition manual; the edit mode gets
    the wardrobe module and the list of usable edit profiles instead.
    """
    if mode not in _MODE_OPERATIONS:
        raise CreateAgentError(f"Unknown Create mode '{mode}'.", code="mode_unsupported")
    sections = [_section("krea2_base", "family_base", *_content("base.md"))]
    if mode != "edit":
        sections.append(_section("composition", "module", *_content("composition.md")))
    sections.append(_section("wardrobe", "module", *_content("wardrobe.md")))
    if intimate:
        sections.append(_section("intimate", "module", *_content("intimate.md")))
    sections.append(_section(f"mode_{mode}", "operation", *_content(f"{mode}.md")))
    if mode == "edit" and profile.edit_profiles:
        sections.append(_section(
            "edit_profiles", "operation",
            _edit_profiles_section(profile, references=references),
            f"{profile.id}/manifest.json",
        ))
    if original:
        sections.append(_section("original", "module", *_content("original.md")))
    sections.append(_section("create_krea2", "output_contract", *_content("output.md")))
    return CreateInstructionBundle(
        task=create_task(profile, mode),
        capability_status=CapabilityStatus.SUPPORTED,
        sections=tuple(sections),
        versions={"create_krea2": CREATE_INSTRUCTION_VERSION, "mode": mode},
    )


def instruction_paths() -> list[str]:
    """Files the user may read and edit to change the agent's behaviour."""
    return [str(path) for path in sorted(CREATE_CONTENT_DIR.glob("*.md"))]


# --- agent profile -----------------------------------------------------------------


def _roles(profile: dict[str, Any]) -> set[str]:
    return {str(role) for role in profile.get("roles") or ()}


def _sees_images(profile: dict[str, Any]) -> bool:
    if profile.get("model_card"):
        # Picked in Create: what the model's card (or family) says.
        return profile.get("multimodal") is True
    if profile.get("kind") == "cli" and profile.get("cli_type") in {"codex", "claude", "antigravity"}:
        known = family_vision(str(profile.get("model") or ""))
        if known is not None:
            return known
        # Legacy Codex/Claude profiles keep their native image transport defaults.
        return profile.get("cli_type") != "antigravity"
    return profile.get("multimodal") is True or "vision" in _roles(profile)


def sees_images(profile: dict[str, Any]) -> bool:
    """Whether Create may send this agent the photo (before the host's own input check)."""
    return _sees_images(profile)


def pick_agent_profile(store: AIProfileStore, *, wants_image: bool) -> dict[str, Any] | None:
    """Choose the agent for one Create run from the Integrations defaults.

    Photo modes prefer the default vision agent; text prefers the default text agent.
    Either falls back to any profile with the other role. Translator-only profiles
    are never used. With no usable profile at all, an installed OpenCode runs a free
    Zen model, so Create works right after OpenCode is installed.
    """
    state = store.list()
    profiles = [item for item in state.get("profiles", []) if isinstance(item, dict)]
    defaults = state.get("defaults") or {}
    vision = [defaults.get("multimodal_profile_id")] + [p["id"] for p in profiles if _sees_images(p)]
    text = [defaults.get("text_profile_id")] + [p["id"] for p in profiles if "text" in _roles(p)]
    for profile_id in (vision + text) if wants_image else (text + vision):
        if not profile_id:
            continue
        try:
            return store.get(profile_id)
        except AIProfileStoreError:
            continue
    return implicit_opencode_profile()


IMPLICIT_OPENCODE_PROFILE_ID = "implicit-opencode-free"
# Free Zen model families in order of preference; ids change between releases.
PREFERRED_FREE_MODELS = ("muse-spark", "mimo", "exo", "longcat", "space-bunny", "step")
_FREE_MODEL_TTL_SECONDS = 15 * 60
_free_model_cache: dict[str, tuple[float, opencode_host.OpenCodeModel | None]] = {}


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


_models_cache = catalog_cache.memory


def _opencode_card(card: dict[str, Any]) -> opencode_host.OpenCodeModel:
    return opencode_host.OpenCodeModel(
        id=card["id"], provider=card["provider"], name=card["name"], vision=card.get("vision"),
        free=card.get("free"), variants=tuple(card.get("variants") or ()),
    )


def opencode_models(executable: str, *, refresh: bool = False) -> list[opencode_host.OpenCodeModel]:
    """Every model one installation can run, with its card (vision, free, variants); cached."""
    cached = catalog_cache.load("opencode", executable) if not refresh else None
    if cached:
        return [_opencode_card(card) for card in cached]
    models = opencode_host.list_models(executable)
    catalog_cache.save("opencode", executable, [model.to_dict() for model in models])
    return models


def chosen_opencode_profile(model_id: str, variant: str | None = None) -> dict[str, Any]:
    """The agent for an OpenCode model picked in Create; its card says whether it sees photos.

    The model must be one the newest installation lists, and the variant one of its card.
    OpenCode 1.x takes no variant, so the model runs at its default level there.
    """
    installations = opencode_host.find_installations()
    if not installations:
        raise CreateAgentError("OpenCode is not installed.", code="cli_unavailable", status=409)
    installation = installations[0]
    try:
        models = opencode_models(installation.executable)
    except CLIIntegrationError as exc:
        raise CreateAgentError(
            "OpenCode could not list its models.", code="cli_unavailable", status=409, technical_error=str(exc)
        ) from exc
    model = next((item for item in models if item.id == model_id), None)
    if model is None:
        raise CreateAgentError(f"OpenCode has no model '{model_id}'.", code="model_unavailable", status=409)
    if variant and variant not in model.variants:
        raise CreateAgentError(
            f"{model.name} has no reasoning level '{variant}'.", code="variant_unavailable", status=400
        )
    use_variant = bool(variant) and installation.generation >= 2
    run_model = f"{model.id}{opencode_host.VARIANT_SEPARATOR}{variant}" if use_variant else model.id
    return {
        "id": f"opencode:{run_model}",
        "model_card": True,
        "kind": "cli",
        "cli_type": "opencode",
        "name": " · ".join(["OpenCode", model.name, *([variant] if use_variant else [])]),
        "model": run_model,
        "multimodal": bool(model.vision),
        "roles": ["text", "vision"] if model.vision else ["text"],
        "timeout_seconds": MIN_AGENT_TIMEOUT_SECONDS,
        "executable": installation.executable,
    }


_AGY_LEVEL = re.compile(r"^(?P<base>.+?)-(?P<level>minimal|low|medium|high|xhigh|max)$")
_AGY_NAME_LEVEL = re.compile(r"^(?P<name>.+?)\s*\((?P<level>[^()]+)\)$")
_AGY_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
_agy_cache = catalog_cache.memory


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
            "vision": family_vision(base),
            "vision_source": "family" if family_vision(base) is not None else "unknown",
            "levels": [],
        })
        if level and not any(item["id"] == model_id for item in group["levels"]):
            group["levels"].append({"id": model_id, "level": level})
        else:
            group["model"] = model_id
    return list(groups.values())


def antigravity_models(*, refresh: bool = False) -> tuple[str | None, list[dict[str, Any]]]:
    """Antigravity's executable and its models (cached); no executable when it is not installed."""
    from app.ai.cli import find_executable, run_command

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


def chosen_antigravity_profile(model_id: str) -> dict[str, Any]:
    """The agent for an Antigravity model picked in Create (the id carries its reasoning level)."""
    try:
        executable, models = antigravity_models()
    except CLIIntegrationError as exc:
        raise CreateAgentError(
            "Antigravity could not list its models.", code="cli_unavailable", status=409, technical_error=str(exc)
        ) from exc
    if executable is None:
        raise CreateAgentError("Antigravity is not installed.", code="cli_unavailable", status=409)
    for model in models:
        levels = {item["id"]: item["level"] for item in model["levels"]}
        if model_id == model.get("model") or model_id in levels:
            level = levels.get(model_id)
            return {
                "id": f"antigravity:{model_id}",
                "model_card": True,
                "kind": "cli",
                "cli_type": "antigravity",
                "name": " · ".join(["Antigravity", model["name"], *([level] if level else [])]),
                "model": model_id,
                "multimodal": model["vision"],
                "roles": ["text", "vision"] if model["vision"] else ["text"],
                "timeout_seconds": MIN_AGENT_TIMEOUT_SECONDS,
                "executable": executable,
            }
    raise CreateAgentError(f"Antigravity has no model '{model_id}'.", code="model_unavailable", status=409)


def chosen_native_profile(host: str, model_id: str, variant: str | None = None) -> dict[str, Any]:
    """Validate a Create selection against the host's own catalog before execution."""
    label = {"codex": "Codex", "claude": "Claude Code"}[host]
    try:
        executable, models = native_cli_models(host)
    except CLIIntegrationError as exc:
        raise CreateAgentError(f"{label} could not list its models.", code="cli_unavailable", status=409,
                               technical_error=str(exc)) from exc
    if executable is None:
        raise CreateAgentError(f"{label} is not installed.", code="cli_unavailable", status=409)
    model = next((item for item in models if item["id"] == model_id), None)
    if model is None:
        raise CreateAgentError(f"{label} has no model '{model_id}'.", code="model_unavailable", status=409)
    if variant and variant not in model["variants"]:
        raise CreateAgentError(f"{model['name']} has no reasoning level '{variant}'.", code="variant_unavailable")
    return {
        "id": f"{host}:{model_id}", "model_card": True, "kind": "cli", "cli_type": host,
        "name": " · ".join([label, model["name"], *([variant] if variant else [])]),
        "model": model_id, "multimodal": model["vision"], "reasoning_effort": variant,
        "roles": ["text", "vision"] if model["vision"] else ["text"],
        "timeout_seconds": MIN_AGENT_TIMEOUT_SECONDS, "executable": executable,
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
        "kind": "cli",
        "cli_type": "opencode",
        "name": f"{installation.label}, free {model.name}",
        "model": model.id,
        "multimodal": bool(model.vision),
        "roles": ["text", "vision"] if model.vision else ["text"],
        "timeout_seconds": MIN_AGENT_TIMEOUT_SECONDS,
        "executable": installation.executable,
    }


# --- images ------------------------------------------------------------------------


def decode_data_url(value: str | None) -> bytes | None:
    if not value or not value.startswith("data:image/") or "," not in value:
        return None
    header, payload = value.split(",", 1)
    if ";base64" not in header:
        return None
    try:
        return base64.b64decode(payload, validate=True) or None
    except (binascii.Error, ValueError):
        return None


def agent_image_bytes(main: bytes, subject: bytes | None = None) -> bytes:
    """One JPEG for the agent: the photo, or photo 1 and photo 2 side by side."""
    from PIL import Image

    def load(data: bytes) -> Image.Image:
        with Image.open(io.BytesIO(data)) as image:
            return image.convert("RGB")

    first = load(main)
    if subject is not None:
        second = load(subject)
        height = min(1024, max(first.height, second.height))
        first = first.resize((max(1, round(first.width * height / first.height)), height))
        second = second.resize((max(1, round(second.width * height / second.height)), height))
        gap = 24
        sheet = Image.new("RGB", (first.width + gap + second.width, height), (255, 255, 255))
        sheet.paste(first, (0, 0))
        sheet.paste(second, (first.width + gap, 0))
        first = sheet
    first.thumbnail((AGENT_IMAGE_MAX_SIDE, AGENT_IMAGE_MAX_SIDE))
    buffer = io.BytesIO()
    first.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


# --- task input --------------------------------------------------------------------


def build_user_input(
    *,
    mode: CreateMode,
    request: str,
    previous_prompt: str | None,
    image_state: str,
    dimensions: tuple[int, int] | None,
) -> str:
    parts = [f"MODE: {mode}"]
    cleaned = request.strip()[:MAX_REQUEST_CHARS]
    if cleaned:
        parts.append(f"USER REQUEST (any language, translate it):\n{cleaned}")
    elif mode in {"reference", "variation"}:
        parts.append("USER REQUEST: none. Write a faithful reconstruction of the reference photo.")
    else:
        parts.append("USER REQUEST: none.")
    if previous_prompt and previous_prompt.strip():
        parts.append(
            "PREVIOUS PROMPT (the user is refining this result; keep it except what the request changes):\n"
            + previous_prompt.strip()[:MAX_PREVIOUS_PROMPT_CHARS]
        )
    parts.append(f"PHOTOS: {image_state}")
    if dimensions:
        parts.append(f"OUTPUT SIZE: {dimensions[0]}x{dimensions[1]} pixels")
    return "\n\n".join(parts)


def _image_state(mode: CreateMode, *, has_image: bool, has_subject: bool, sees: bool) -> str:
    if not has_image:
        return "none."
    if not sees:
        if mode == "edit":
            return (
                "a photo is attached to the generator, but you cannot see it. Write the command from the "
                "user's words only and refer to things generically; say in note that you could not see the photo."
            )
        return "a photo is attached, but you cannot see it."
    if has_subject:
        return (
            "the attached image is a side-by-side sheet: left half = photo 1 (the scene to edit), "
            "right half = photo 2 (the donor)."
        )
    return "one photo is attached; inspect it before writing."


# --- progress ----------------------------------------------------------------------

ProgressSink = Callable[[dict[str, Any]], None]
MAX_PROGRESS_LINE_CHARS = 500
# Codex journal markers (stderr); the block after "user" echoes the whole task package.
_CODEX_SECTIONS = {"user", "thinking", "codex", "exec", "tokens used"}
_JSON_FENCE_RE = re.compile(r"\A\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*\Z", re.DOTALL)


def display_text(text: str) -> str:
    """The prompt inside an output-contract JSON answer, else the text itself."""
    fenced = _JSON_FENCE_RE.match(text)
    candidate = fenced.group("body") if fenced else text
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return text
    prompt = data.get("positive_prompt") if isinstance(data, dict) else None
    return prompt if isinstance(prompt, str) and prompt.strip() else text


def _opencode_reader(emit: ProgressSink) -> Callable[[str], None]:
    """OpenCode ``--format json`` events: a step starts, the model reasons, the text lands."""

    def read(line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        text = part.get("text") if isinstance(part.get("text"), str) else ""
        if kind in {"step_start", "reasoning"}:
            emit({"phase": "thinking", **({"log": text[:MAX_PROGRESS_LINE_CHARS]} if text.strip() else {})})
        elif kind == "text" and text.strip():
            emit({"phase": "writing", "text": display_text(text)})
        elif kind in {"tool_use", "tool"}:
            emit({"phase": "working", "log": f"Tool: {part.get('tool') or 'unknown'}"})

    return read


def _codex_reader(emit: ProgressSink) -> Callable[[str], None]:
    """Codex journal: skip the header and the echoed task, show reasoning and tool lines.

    The answer itself (the "codex" section) arrives as fragments of the JSON contract,
    so only its start is reported; the parsed prompt comes with the result.
    """
    state = {"section": "header"}

    def read(line: str) -> None:
        text = line.rstrip()
        stripped = text.strip()
        if not stripped or stripped.startswith("--------"):
            return
        if stripped in _CODEX_SECTIONS:
            state["section"] = stripped
            phase = {"thinking": "thinking", "codex": "writing", "exec": "working"}.get(stripped)
            if phase:
                emit({"phase": phase})
            return
        section = state["section"]
        if stripped.startswith(("mcp:", "hook:")):
            emit({"phase": "working", "log": stripped[:MAX_PROGRESS_LINE_CHARS]})
        elif section in {"thinking", "exec"}:
            emit({"phase": "thinking" if section == "thinking" else "working",
                  "log": stripped[:MAX_PROGRESS_LINE_CHARS]})

    return read


def _line_reader(emit: ProgressSink) -> Callable[[str], None]:
    def read(line: str) -> None:
        stripped = line.strip()
        if stripped:
            emit({"phase": "working", "log": stripped[:MAX_PROGRESS_LINE_CHARS]})

    return read


def progress_reader(agent: dict[str, Any], emit: ProgressSink) -> Callable[[str], None] | None:
    """Turn one host's raw output lines into progress events for the Create page.

    OpenCode prints whole parts (no token deltas), Codex keeps its journal on stderr,
    Antigravity prints plain lines. Claude Code answers with one JSON envelope at the
    end and direct API calls are not streamed; they report only start and finish.
    """
    if agent.get("kind") != "cli":
        return None
    cli_type = agent.get("cli_type")
    if cli_type == "opencode":
        return _opencode_reader(emit)
    if cli_type == "codex":
        return _codex_reader(emit)
    if cli_type == "antigravity":
        return _line_reader(emit)
    return None


def _safe_sink(on_progress: ProgressSink | None) -> ProgressSink:
    def emit(event: dict[str, Any]) -> None:
        if on_progress is None:
            return
        try:
            on_progress(event)
        except Exception:  # a broken listener must not break the agent run
            logger.debug("Create progress listener failed", exc_info=True)

    return emit


# --- run ---------------------------------------------------------------------------


def run_create_agent(
    *,
    profile: ApprovedProfile,
    store: AIProfileStore,
    mode: CreateMode,
    request: str,
    image: bytes | None = None,
    subject: bytes | None = None,
    previous_prompt: str | None = None,
    dimensions: tuple[int, int] | None = None,
    router: ExecutionRouter | None = None,
    agent_profile_id: str | None = None,
    agent_model: str | None = None,
    agent_variant: str | None = None,
    agent_host: str | None = None,
    on_progress: ProgressSink | None = None,
) -> CreateAgentResult:
    """Write the prompt for one Create run; ``agent_profile_id`` overrides the default agent.

    ``on_progress`` receives small events while the agent works: ``started`` (who
    runs), then ``thinking`` / ``working`` / ``writing`` with an optional ``log`` line
    or ``text``, as far as the host reports them.
    """
    if mode in {"reference", "variation", "edit"} and image is None:
        raise CreateAgentError("Attach a photo for this mode.", code="image_required")
    if mode == "edit" and not request.strip():
        raise CreateAgentError("Describe what to change in the photo.", code="instruction_required")
    if mode == "text" and not request.strip() and not (previous_prompt or "").strip():
        raise CreateAgentError("Describe what to create.", code="prompt_required")

    if agent_model and agent_host == "antigravity":
        agent = chosen_antigravity_profile(agent_model)
    elif agent_model and agent_host in {"codex", "claude"}:
        agent = chosen_native_profile(agent_host, agent_model, agent_variant)
    elif agent_model:
        # An OpenCode model picked in Create, outside the Integrations profiles.
        agent = chosen_opencode_profile(agent_model, agent_variant)
    elif agent_profile_id:
        try:
            agent = store.get(agent_profile_id)
        except (AIProfileStoreError, KeyError) as exc:
            raise CreateAgentError("The chosen agent profile does not exist.", code="profile_not_found", status=404) from exc
    else:
        agent = pick_agent_profile(store, wants_image=image is not None)
    if agent is None:
        raise CreateAgentError(
            "No AI agent is configured. Add one on the Integrations page.",
            code="no_ai_profile",
            status=409,
        )
    router = router or ExecutionRouter()
    try:
        accepted = router.capabilities_for(agent).image_inputs
    except ExecutionRouterError as exc:
        raise CreateAgentError(str(exc), code=exc.code, status=409) from exc
    sees = image is not None and _sees_images(agent) and bool(accepted)
    if mode in {"reference", "variation"} and not sees:
        raise CreateAgentError(
            f"The agent '{agent.get('name')}' cannot see photos. Choose a model that sees photos "
            "in Create's agent menu for this mode.",
            code="vision_required",
            status=409,
        )

    references = 2 if subject is not None else 1
    bundle = build_create_bundle(
        profile,
        mode,
        references=references,
        intimate=wants_intimate_module(request, previous_prompt),
        original=wants_original_module(request),
    )
    user_input = build_user_input(
        mode=mode,
        request=request,
        previous_prompt=previous_prompt,
        image_state=_image_state(mode, has_image=image is not None, has_subject=subject is not None, sees=sees),
        dimensions=dimensions,
    )
    execution_profile = dict(agent)
    execution_profile["timeout_seconds"] = max(
        int(agent.get("timeout_seconds") or 0), MIN_AGENT_TIMEOUT_SECONDS
    )
    try:
        api_key = store.resolve_api_key(agent)
    except AIProfileStoreError as exc:
        raise CreateAgentError(str(exc), code=exc.code, status=409) from exc
    emit = _safe_sink(on_progress)
    emit({
        "phase": "started",
        "agent": str(agent.get("name") or agent.get("cli_type") or "Agent"),
        "model": agent.get("model"),
        "sees_images": sees,
    })
    reader = progress_reader(agent, emit) if on_progress is not None else None

    # A host may still hold the copied photo open; a leftover folder must not fail the run.
    with tempfile.TemporaryDirectory(prefix="cmv-create-agent-", ignore_cleanup_errors=True) as temp_dir:
        image_kwargs: dict[str, Any] = {}
        if sees and image is not None:
            try:
                jpeg = agent_image_bytes(image, subject)
            except Exception as exc:
                raise CreateAgentError("The photo could not be read.", code="invalid_image") from exc
            if "file_path" in accepted:
                path = Path(temp_dir) / "photo.jpg"
                path.write_bytes(jpeg)
                image_kwargs["image_path"] = path
            else:
                image_kwargs["image_data_url"] = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        try:
            outcome = router.execute(
                profile=execution_profile,
                task=bundle.task,
                user_input=user_input,
                api_key=api_key,
                bundle=bundle,
                on_output_chunk=reader,
                **image_kwargs,
            )
        except ExecutionRouterError as exc:
            logger.warning("Create agent failed (%s/%s): %s", exc.stage, exc.code, exc.technical_error or exc)
            raise CreateAgentError(
                f"The agent '{agent.get('name')}' failed: {exc}",
                code=exc.code,
                status=502,
                technical_error=exc.technical_error,
            ) from exc

    result = outcome.result
    edit_profile = None
    negative = ""
    note = result.note
    if mode == "edit":
        negative = result.negative_prompt
        try:
            if profile.edit_profiles and not result.edit_profile:
                raise ValueError("The agent did not select an edit profile")
            chosen = resolve_edit_profile(profile, result.edit_profile, references=references)
        except ValueError as exc:
            raise CreateAgentError(
                "The agent chose an edit type that does not match the attached photos. Try preparing the edit again.",
                code="edit_profile_invalid", status=502, technical_error=str(exc),
            ) from exc
        edit_profile = chosen.id if chosen else None
    metadata = outcome.metadata if isinstance(outcome.metadata, dict) else {}
    latency = metadata.get("latency_ms")
    return CreateAgentResult(
        prompt=result.positive_prompt,
        negative_prompt=negative,
        edit_profile=edit_profile,
        note=note,
        agent_name=str(agent.get("name") or outcome.adapter_id),
        adapter_id=outcome.adapter_id,
        model=agent.get("model"),
        saw_image=sees,
        job_id=outcome.job_id,
        latency_ms=int(latency) if isinstance(latency, int | float) else None,
    )


__all__ = [
    "CREATE_CONTENT_DIR",
    "CreateAgentError",
    "CreateAgentResult",
    "CreateInstructionBundle",
    "agent_image_bytes",
    "build_create_bundle",
    "build_user_input",
    "create_task",
    "decode_data_url",
    "default_free_model",
    "implicit_opencode_profile",
    "instruction_paths",
    "pick_agent_profile",
    "progress_reader",
    "run_create_agent",
    "wants_intimate_module",
]
