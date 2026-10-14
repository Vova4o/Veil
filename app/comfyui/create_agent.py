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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.ai import agents
from app.ai.agents import AgentError, sees_images
from app.ai.execution import ExecutionRouter, ExecutionRouterError
from app.ai.prompting import (
    InstructionBundle,
    InstructionSection,
    PromptOperation,
    PromptScenario,
    PromptTask,
)
from app.ai.prompting.models import CapabilityStatus
from app.ai.prompting.registry import CONTENT_DIR
from app.ai.providers import AIProviderError, AIProviderStore

from .simple_profiles import ApprovedProfile, edit_frame_change, resolve_edit_profile

logger = logging.getLogger(__name__)

CreateMode = Literal["text", "reference", "variation", "edit"]
CREATE_CONTENT_DIR = CONTENT_DIR / "create" / "krea2"
CREATE_INSTRUCTION_VERSION = "4"
# Agent CLIs need time to read a long task package and an image.
MIN_AGENT_TIMEOUT_SECONDS = agents.DEFAULT_TIMEOUT_SECONDS
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
    r"lingerie|bikini\w*|microbikini\w*|swimsuit\w*|swimwear\w*|thong\w*|micro[\s-]?bikini\w*|"
    r"erotic\w*|sex\w*|porn\w*|intima\w*|boudoir|genital\w*|vagina\w*|labia\w*|anus|anal|"
    r"breasts?|boobs?|tits?|pussy|penis|masturbat\w*|orgasm\w*|"
    r"unbutton\w*|unzip\w*|pull(?:ed|ing)?\s+(?:down|up|open|aside)|slide(?:d)?\s+down|slip(?:ped)?\s+off|"
    r"expose\w*|reveal\w*|sheer(?:ify)?|"
    r"хард|ню|голая|голый|голые|голой|обнаж\w*|разде\w*|сним\w*|сня\w*|стяни\w*|стяну\w*|"
    r"спусти\w*|спуска\w*|приспусти\w*|расстегн\w*|задер\w*|задери|оголи\w*|оголя\w*|"
    r"бикини|микробикини|микро[\s-]?бикини|купальник\w*|стринги|плавк\w*|"
    r"топлес|сос[оа]к\w*|эрот\w*|секс\w*|порн\w*|интим\w*|бель[её]|трусик\w*|лифчик\w*|груд\w*|"
    r"сиськ\w*|вагин\w*|киск\w*|попк\w*|поп[ауеы]|попе|попой|ягодиц\w*|анал\w*|мастурб\w*|оргазм\w*)\b",
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
    intimate: bool | None = None,
    original: bool = False,
) -> CreateInstructionBundle:
    """Compile the Create instruction for one mode.

    The mode operation section is placed immediately after the family base, so the
    agent knows its primary task objective before reading the visual and wardrobe manuals.
    Reference mode includes the intimate module by default (per the unrated reference baseline).
    """
    if mode not in _MODE_OPERATIONS:
        raise CreateAgentError(f"Unknown Create mode '{mode}'.", code="mode_unsupported")
    intimate_enabled = (mode == "reference") if intimate is None else intimate
    sections = [
        _section("krea2_base", "family_base", *_content("base.md")),
        _section(f"mode_{mode}", "operation", *_content(f"{mode}.md")),
    ]
    if mode == "edit" and profile.edit_profiles:
        sections.append(_section(
            "edit_profiles", "operation",
            _edit_profiles_section(profile, references=references),
            f"{profile.id}/manifest.json",
        ))
    if mode != "edit":
        sections.append(_section("composition", "module", *_content("composition.md")))
    sections.append(_section("wardrobe", "module", *_content("wardrobe.md")))
    if intimate_enabled:
        sections.append(_section("intimate", "module", *_content("intimate.md")))
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


# --- agent ---------------------------------------------------------------------------


def _agent_profile(store: AIProviderStore, host: str | None, model: str | None, variant: str | None,
                   *, wants_image: bool) -> dict[str, Any] | None:
    """The model picked in Create's agent menu, else the default model of Integrations."""
    if not model:
        return agents.default_profile(store, wants_image=wants_image)
    try:
        return agents.resolve(store, {"host": host or "opencode", "model": model, "variant": variant})
    except AgentError as exc:
        raise CreateAgentError(str(exc), code=exc.code, status=exc.status, technical_error=exc.technical_error) from exc


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
    source_size: tuple[int, int] | None = None,
) -> str:
    """The per-run part of the task; ``source_size`` (photo 1) lets an edit see a frame change."""
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
    if mode == "edit" and source_size and dimensions:
        parts.append(f"PHOTO 1 SIZE: {source_size[0]}x{source_size[1]} pixels")
        strips = edit_frame_change(source_size, dimensions)
        if strips:
            shape = "wider" if strips == "sides" else "taller"
            parts.append(
                f"FRAME CHANGE: the output is {shape} than photo 1. The generator keeps all of photo 1 "
                f"centered and draws new strips at the {strips}; follow \"Frame change\" in the mode section "
                f"(extend ambient background seamlessly; NEVER list furniture, appliances, or room objects)."
            )
    return "\n\n".join(parts)


def image_size(data: bytes | None) -> tuple[int, int] | None:
    """Pixel size of an image, or ``None`` when it cannot be read."""
    if not data:
        return None
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            return int(image.width), int(image.height)
    except (UnidentifiedImageError, OSError):
        return None


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


def _antigravity_reader(emit: ProgressSink) -> Callable[[str], None]:
    state = {"answer": False}

    def read(line: str) -> None:
        stripped = line.strip()
        if not stripped or state["answer"]:
            return
        # agy's final JSON envelope is transport data, not a progress journal.
        # Suppress every line of a pretty-printed envelope as well as one-line JSON.
        if stripped.startswith(("{", "```")):
            state["answer"] = True
            emit({"phase": "writing"})
        else:
            emit({"phase": "working", "log": stripped[:MAX_PROGRESS_LINE_CHARS]})

    return read


def progress_reader(agent: dict[str, Any], emit: ProgressSink) -> Callable[[str], None] | None:
    """Turn one host's raw output lines into progress events for the Create page.

    OpenCode prints whole parts (no token deltas), Codex keeps its journal on stderr,
    Antigravity's final JSON envelope is hidden. Claude Code answers with one JSON
    envelope at the end and direct API calls are not streamed; they report only start and finish.
    """
    if agent.get("kind") != "cli":
        return None
    cli_type = agent.get("cli_type")
    if cli_type == "opencode":
        return _opencode_reader(emit)
    if cli_type == "codex":
        return _codex_reader(emit)
    if cli_type == "antigravity":
        return _antigravity_reader(emit)
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
    store: AIProviderStore,
    mode: CreateMode,
    request: str,
    image: bytes | None = None,
    subject: bytes | None = None,
    previous_prompt: str | None = None,
    dimensions: tuple[int, int] | None = None,
    router: ExecutionRouter | None = None,
    agent_model: str | None = None,
    agent_variant: str | None = None,
    agent_host: str | None = None,
    on_progress: ProgressSink | None = None,
) -> CreateAgentResult:
    """Write the prompt for one Create run with the model picked in Create (or the default).

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

    agent = _agent_profile(store, agent_host, agent_model, agent_variant, wants_image=image is not None)
    if agent is None:
        raise CreateAgentError(
            "No AI model is available. Connect a provider in Integrations › Providers.",
            code="no_ai_model",
            status=409,
        )
    router = router or ExecutionRouter()
    try:
        accepted = router.capabilities_for(agent).image_inputs
    except ExecutionRouterError as exc:
        raise CreateAgentError(str(exc), code=exc.code, status=409) from exc
    sees = image is not None and sees_images(agent) and bool(accepted)
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
        intimate=mode == "reference" or wants_intimate_module(request, previous_prompt),
        original=wants_original_module(request),
    )
    user_input = build_user_input(
        mode=mode,
        request=request,
        previous_prompt=previous_prompt,
        image_state=_image_state(mode, has_image=image is not None, has_subject=subject is not None, sees=sees),
        dimensions=dimensions,
        source_size=image_size(image) if mode == "edit" else None,
    )
    execution_profile = dict(agent)
    execution_profile["timeout_seconds"] = max(
        int(agent.get("timeout_seconds") or 0), MIN_AGENT_TIMEOUT_SECONDS
    )
    try:
        api_key = store.resolve_api_key(agent)
    except AIProviderError as exc:
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
    "instruction_paths",
    "progress_reader",
    "run_create_agent",
    "wants_intimate_module",
]
