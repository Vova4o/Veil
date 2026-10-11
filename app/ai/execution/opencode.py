from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import opencode_host
from ..cli import CLIIntegrationError, find_executable, run_command
from ..prompting import (
    InstructionBundle,
    PromptCompiler,
    PromptCompilerError,
    PromptContractError,
    PromptResult,
    PromptTask,
    parse_prompt_result,
)

MAX_USER_INPUT_CHARS = 100_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
OPENCODE_AGENT_NAME = "cmv-prompt-smoke"
LEGACY_OPENCODE_TIMEOUT_SECONDS = 60
DEFAULT_OPENCODE_TIMEOUT_SECONDS = 5 * 60
# The JSON event stream repeats every text part; long prompts exceed the default cap.
RUN_OUTPUT_LIMIT = 2_000_000
_SINGLE_JSON_FENCE_RE = re.compile(
    r"\A\s*```(?:json)?[ \t]*\r?\n(?P<body>\{.*\})\r?\n```[ \t]*\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


class OpenCodePromptExecutionError(RuntimeError):
    """Normalized failure raised by the managed OpenCode execution path."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        stage: str,
        technical_error: str | None = None,
    ):
        self.code = code
        self.stage = stage
        self.technical_error = technical_error
        super().__init__(message)


@dataclass(frozen=True)
class OpenCodePromptExecutionResult:
    result: PromptResult
    bundle: InstructionBundle
    latency_ms: int
    raw_response_sha256: str
    transport: str = "opencode"
    agent: str = OPENCODE_AGENT_NAME
    response_normalizations: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "agent": self.agent,
            "latency_ms": self.latency_ms,
            "raw_response_sha256": self.raw_response_sha256,
            "response_normalizations": list(self.response_normalizations),
            "bundle": self.bundle.metadata(),
        }


@dataclass(frozen=True)
class OpenCodeRawExecutionResult:
    text: str
    latency_ms: int
    raw_response_sha256: str
    response_normalizations: tuple[str, ...] = ()
    agent: str = OPENCODE_AGENT_NAME
    # Text parts as emitted; the stock agent may talk before its final answer.
    parts: tuple[str, ...] = ()


class OpenCodePromptExecutor:
    """Execute a PromptTask through an OpenCode CLI profile.

    This is the managed CLI form of the agent-host integration. Every run gets a
    temporary project and an empty config home, so the user's global OpenCode
    config (MCP servers, plugins, agents) stays out. Zen models run on the stock
    ``plan`` agent, which the free tier requires; other providers run on a
    temporary primary agent with every tool permission denied. OpenCode still owns
    provider authentication, model selection, session execution and JSON events.
    Both the npm CLI and the desktop app's bundled CLI are supported
    (``opencode_host.build_run_command``).
    """

    def __init__(self, compiler: PromptCompiler | None = None):
        self.compiler = compiler or PromptCompiler()

    def execute(
        self,
        *,
        profile: dict[str, Any],
        task: PromptTask,
        user_input: str,
        image_path: str | Path | None = None,
        bundle: InstructionBundle | None = None,
        on_output_chunk: Any = None,
    ) -> OpenCodePromptExecutionResult:
        self._validate_profile(profile)
        cleaned_input = self._validate_user_input(user_input)
        validated_image = self._validate_image_path(image_path)
        if validated_image is not None and profile.get("multimodal") is not True:
            raise OpenCodePromptExecutionError(
                "This OpenCode profile is not marked as multimodal.",
                code="incompatible_format",
                stage="input",
            )

        if bundle is None:
            try:
                bundle = self.compiler.compile(task)
            except PromptCompilerError as exc:
                raise OpenCodePromptExecutionError(
                    str(exc),
                    code="prompt_compile_error",
                    stage="compile",
                    technical_error=str(exc),
                ) from exc
        elif bundle.task != task:
            raise OpenCodePromptExecutionError(
                "The prepared instruction bundle does not match the prompt task.",
                code="bundle_task_mismatch",
                stage="compile",
            )

        raw_execution = self.execute_raw(
            profile=profile,
            task_package=self._render_task_package(bundle, cleaned_input),
            image_path=validated_image,
            title="ComfyUI Meta Viewer prompt smoke",
            on_output_chunk=on_output_chunk,
        )
        try:
            result = self._parse_result(raw_execution)
        except PromptContractError as exc:
            raise OpenCodePromptExecutionError(
                str(exc),
                code=exc.code,
                stage="contract",
                technical_error=exc.technical_error,
            ) from exc

        return OpenCodePromptExecutionResult(
            result=result,
            bundle=bundle,
            latency_ms=raw_execution.latency_ms,
            raw_response_sha256=raw_execution.raw_response_sha256,
            agent=raw_execution.agent,
            response_normalizations=raw_execution.response_normalizations,
        )

    def _parse_result(self, raw_execution: OpenCodeRawExecutionResult) -> PromptResult:
        try:
            return parse_prompt_result(raw_execution.text)
        except PromptContractError:
            # The stock agent can emit a remark before the final JSON part.
            if len(raw_execution.parts) < 2:
                raise
            last, _ = self._normalize_response(raw_execution.parts[-1].strip())
            try:
                return parse_prompt_result(last)
            except PromptContractError:
                pass
            raise

    def execute_raw(
        self,
        *,
        profile: dict[str, Any],
        task_package: str,
        image_path: str | Path | None = None,
        title: str = "ComfyUI Meta Viewer structured task",
        on_output_chunk: Any = None,
    ) -> OpenCodeRawExecutionResult:
        self._validate_profile(profile)
        timeout_seconds = self._resolve_timeout(profile)
        if not isinstance(task_package, str) or not task_package.strip():
            raise OpenCodePromptExecutionError(
                "The OpenCode task package cannot be empty.",
                code="invalid_input",
                stage="input",
            )
        if len(task_package) > 500_000:
            raise OpenCodePromptExecutionError(
                "The OpenCode task package is too large.",
                code="input_too_large",
                stage="input",
            )
        validated_image = self._validate_image_path(image_path)
        if validated_image is not None and profile.get("multimodal") is not True:
            raise OpenCodePromptExecutionError(
                "This OpenCode profile is not marked as multimodal.",
                code="incompatible_format",
                stage="input",
            )

        executable = find_executable("opencode", profile.get("executable"))
        if executable is None and not profile.get("executable"):
            executable = opencode_host.resolve_executable()
        if executable is None:
            raise OpenCodePromptExecutionError(
                "OpenCode was not found in PATH or at the configured executable path.",
                code="cli_unavailable",
                stage="host",
            )
        generation = opencode_host.generation_for(executable)
        stock_agent = opencode_host.uses_stock_agent(profile["model"])
        agent = opencode_host.STOCK_AGENT if stock_agent else OPENCODE_AGENT_NAME

        try:
            with tempfile.TemporaryDirectory(prefix="cmv-opencode-", ignore_cleanup_errors=True) as temp_dir:
                workspace = Path(temp_dir) / "project"
                config_home = Path(temp_dir) / "config"
                workspace.mkdir()
                config_home.mkdir()
                self._write_isolated_config(workspace, stock_agent=stock_agent)
                task_file = workspace / "cmv-task.md"
                task_file.write_text(
                    task_package.strip() + "\n",
                    encoding="utf-8",
                    newline="\n",
                )

                files = [task_file]
                if validated_image is not None:
                    copied_image = workspace / f"input{validated_image.suffix.lower()}"
                    shutil.copyfile(validated_image, copied_image)
                    files.append(copied_image)

                prompt = (
                    "Use the attached CMV task package as the authoritative task. "
                    "Do not use tools, subagents, skills, file browsing, shell commands, "
                    "or network access. Return only the strict JSON object required by "
                    "the task package, with no Markdown fences or commentary."
                )
                args = opencode_host.build_run_command(
                    executable,
                    generation=generation,
                    model=profile["model"],
                    agent=agent,
                    title=title,
                    message=prompt,
                    files=files,
                )
                command = run_command(
                    args,
                    timeout=timeout_seconds,
                    cwd=workspace,
                    env_overrides=opencode_host.isolated_env(config_home),
                    output_limit=RUN_OUTPUT_LIMIT,
                    on_output_chunk=on_output_chunk,
                )
        except CLIIntegrationError as exc:
            raise OpenCodePromptExecutionError(
                str(exc),
                code=exc.code,
                stage="host",
                technical_error=str(exc),
            ) from exc
        except OSError as exc:
            raise OpenCodePromptExecutionError(
                f"Cannot prepare the isolated OpenCode task: {exc}",
                code="host_workspace_error",
                stage="host",
                technical_error=str(exc),
            ) from exc

        combined_output = "\n".join(
            part for part in (command.stdout, command.stderr) if part
        )
        parts = tuple(opencode_host.event_texts(command.stdout))
        if opencode_host.is_free_tier_refusal(combined_output):
            raise OpenCodePromptExecutionError(
                opencode_host.FREE_TIER_HINT,
                code="free_tier_refused",
                stage="host",
                technical_error=combined_output[:16_000] or None,
            )
        reported_error = opencode_host.event_error(command.stdout)
        if command.returncode != 0 or (reported_error and not parts):
            lowered = combined_output.casefold()
            code = (
                "cli_authentication"
                if any(
                    marker in lowered
                    for marker in ("auth", "login", "credential", "access denied")
                )
                else "provider_error"
            )
            raise OpenCodePromptExecutionError(
                reported_error or combined_output[:2_000] or "OpenCode request failed.",
                code=code,
                stage="host",
                technical_error=combined_output[:16_000] or None,
            )

        raw_text = "".join(parts).strip()
        if not raw_text:
            raise OpenCodePromptExecutionError(
                "OpenCode returned no assistant text in its JSON event stream.",
                code="incompatible_format",
                stage="host",
                technical_error=combined_output[:16_000] or None,
            )

        normalized_text, response_normalizations = self._normalize_response(raw_text)
        return OpenCodeRawExecutionResult(
            text=normalized_text,
            latency_ms=command.elapsed_ms,
            raw_response_sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            response_normalizations=response_normalizations,
            agent=agent,
            parts=parts,
        )

    @staticmethod
    def _validate_profile(profile: dict[str, Any]) -> None:
        if profile.get("kind") != "cli" or profile.get("cli_type") != "opencode":
            raise OpenCodePromptExecutionError(
                "This operation requires an OpenCode CLI profile.",
                code="incompatible_profile",
                stage="input",
            )
        if not isinstance(profile.get("model"), str) or not profile["model"].strip():
            raise OpenCodePromptExecutionError(
                "The OpenCode profile has no model ID.",
                code="invalid_profile",
                stage="input",
            )

    @staticmethod
    def _resolve_timeout(profile: dict[str, Any]) -> int:
        """Map the old 60-second default to the five-minute host default.

        Values other than the legacy default are treated as explicit user choices
        and remain unchanged within the supported 5-600 second range.
        """
        raw_timeout = profile.get("timeout_seconds", DEFAULT_OPENCODE_TIMEOUT_SECONDS)
        try:
            timeout_seconds = int(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise OpenCodePromptExecutionError(
                "The OpenCode profile timeout must be a whole number of seconds.",
                code="invalid_profile",
                stage="input",
                technical_error=str(exc),
            ) from exc
        if not 5 <= timeout_seconds <= 600:
            raise OpenCodePromptExecutionError(
                "The OpenCode profile timeout must be between 5 and 600 seconds.",
                code="invalid_profile",
                stage="input",
            )
        if timeout_seconds == LEGACY_OPENCODE_TIMEOUT_SECONDS:
            return DEFAULT_OPENCODE_TIMEOUT_SECONDS
        return timeout_seconds

    @staticmethod
    def _validate_user_input(value: str) -> str:
        if not isinstance(value, str):
            raise OpenCodePromptExecutionError(
                "Prompt task input must be text.",
                code="invalid_input",
                stage="input",
            )
        cleaned = value.strip()
        if not cleaned:
            raise OpenCodePromptExecutionError(
                "Prompt task input cannot be empty.",
                code="invalid_input",
                stage="input",
            )
        if len(cleaned) > MAX_USER_INPUT_CHARS:
            raise OpenCodePromptExecutionError(
                "Prompt task input is too large.",
                code="input_too_large",
                stage="input",
            )
        return cleaned

    @staticmethod
    def _validate_image_path(value: str | Path | None) -> Path | None:
        if value is None:
            return None
        path = Path(value).expanduser().resolve(strict=False)
        if not path.is_file():
            raise OpenCodePromptExecutionError(
                f"Image file does not exist: {path}",
                code="image_not_found",
                stage="input",
            )
        if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            raise OpenCodePromptExecutionError(
                "OpenCode smoke images must be PNG, JPEG, WEBP, or GIF.",
                code="unsupported_image",
                stage="input",
            )
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise OpenCodePromptExecutionError(
                f"Cannot inspect image file: {path}: {exc}",
                code="image_read_error",
                stage="input",
                technical_error=str(exc),
            ) from exc
        if size <= 0:
            raise OpenCodePromptExecutionError(
                "The selected image is empty.",
                code="invalid_image",
                stage="input",
            )
        if size > MAX_IMAGE_BYTES:
            raise OpenCodePromptExecutionError(
                "Image input is too large.",
                code="image_too_large",
                stage="input",
            )
        return path

    @staticmethod
    def _write_isolated_config(workspace: Path, *, stock_agent: bool = False) -> None:
        config: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "share": "disabled",
        }
        if not stock_agent:
            config["agent"] = {
                OPENCODE_AGENT_NAME: {
                    "description": "Generate one CMV PromptResult without tools.",
                    "mode": "primary",
                    "permission": {"*": "deny"},
                    "prompt": (
                        "You execute deterministic ComfyUI Meta Viewer prompt tasks. "
                        "Use only the supplied task package and attachments. Return strict JSON only."
                    ),
                }
            }
        (workspace / "opencode.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    @staticmethod
    def _render_task_package(bundle: InstructionBundle, user_input: str) -> str:
        return (
            "# CMV managed OpenCode task\n\n"
            "The instruction bundle below is authoritative. Do not inspect the project or "
            "seek additional context.\n\n"
            f"{bundle.render()}\n"
            "USER TASK INPUT\n"
            f"{user_input}\n"
        )

    @staticmethod
    def _parse_json_events(output: str) -> str:
        return "".join(opencode_host.event_texts(output)).strip()

    @staticmethod
    def _normalize_response(raw: str) -> tuple[str, tuple[str, ...]]:
        """Remove only one lossless outer JSON code fence from a host response.

        The core PromptResult parser remains strict. This host-boundary normalization
        accepts no prose before or after the fence and does not attempt JSON repair.
        """
        match = _SINGLE_JSON_FENCE_RE.fullmatch(raw)
        if match is None:
            return raw, ()
        return match.group("body").strip(), ("markdown_json_fence_removed",)
