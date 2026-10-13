from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from .agent_cli import (
    AgentCLIInputError,
    agent_run_workspace,
    image_file_name,
    render_task_package,
    unwrap_markdown_fence,
    validate_image_path,
    validate_user_input,
)

DEFAULT_CODEX_TIMEOUT_SECONDS = 5 * 60
LAST_MESSAGE_FILE = "cmv-last-message.txt"
# Model ids that mean "use the model configured in ~/.codex/config.toml".
CODEX_DEFAULT_MODELS = {"", "default", "auto"}


class CodexPromptExecutionError(RuntimeError):
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
class CodexPromptExecutionResult:
    result: PromptResult
    bundle: InstructionBundle
    latency_ms: int
    raw_response_sha256: str
    transport: str = "codex"
    agent: str = "codex-cli"
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


def is_codex_profile(profile: dict[str, Any]) -> bool:
    return profile.get("kind") == "cli" and profile.get("cli_type") == "codex"


def codex_model(profile: dict[str, Any]) -> str | None:
    value = str(profile.get("model") or "").split("\t")[0].strip()
    return None if value.casefold() in CODEX_DEFAULT_MODELS else value


def build_codex_command(
    executable: str, *, model: str | None, image_name: str | None, reasoning_effort: str | None = None
) -> list[str]:
    """``codex exec`` reading the task package from stdin (the trailing ``-``).

    The read-only sandbox keeps the agent from changing anything; the reply is
    taken from ``--output-last-message`` instead of the progress log on stdout.
    """
    command = [executable, "exec"]
    if image_name:
        # --image takes several values, so it must not sit right before the "-" prompt.
        command.extend(["--image", image_name])
    command.extend([
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-last-message",
        LAST_MESSAGE_FILE,
    ])
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])
    command.append("-")
    return command


class CodexPromptExecutor:
    """Execute a PromptTask through the OpenAI Codex CLI (``codex exec``)."""

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
    ) -> CodexPromptExecutionResult:
        if not is_codex_profile(profile):
            raise CodexPromptExecutionError(
                "Profile must be a CLI profile with cli_type='codex'.",
                code="invalid_profile",
                stage="preflight",
            )
        try:
            cleaned_input = validate_user_input(user_input)
            validated_image = validate_image_path(image_path)
        except AgentCLIInputError as exc:
            raise CodexPromptExecutionError(str(exc), code=exc.code, stage="input") from exc

        executable = find_executable("codex", profile.get("executable"))
        if executable is None:
            raise CodexPromptExecutionError(
                "Codex CLI binary is not installed or not found on PATH.",
                code="codex_not_found",
                stage="preflight",
            )
        try:
            instruction_bundle = bundle or self.compiler.compile(task)
        except PromptCompilerError as exc:
            raise CodexPromptExecutionError(
                str(exc), code="prompt_compile_error", stage="compile", technical_error=str(exc)
            ) from exc

        timeout_sec = int(profile.get("timeout_seconds") or DEFAULT_CODEX_TIMEOUT_SECONDS)
        image_name = image_file_name(validated_image)
        command = build_codex_command(executable, model=codex_model(profile), image_name=image_name,
                                      reasoning_effort=profile.get("reasoning_effort"))
        task_package = render_task_package(
            instruction_bundle, cleaned_input, image_name=image_name, image_attached=True
        )
        try:
            with agent_run_workspace("cmv-codex-", image_path=validated_image) as workspace:
                completed = run_command(
                    command,
                    timeout=timeout_sec,
                    cwd=workspace.root,
                    input_text=task_package,
                    on_output_chunk=on_output_chunk,
                    on_error_chunk=on_output_chunk,
                )
                last_message_path = workspace.root / LAST_MESSAGE_FILE
                reply = (
                    last_message_path.read_text(encoding="utf-8", errors="replace").strip()
                    if last_message_path.is_file()
                    else ""
                )
        except CLIIntegrationError as exc:
            raise CodexPromptExecutionError(
                f"Codex CLI execution failed: {exc}",
                code=exc.code,
                stage="execution",
                technical_error=str(exc),
            ) from exc
        except OSError as exc:
            raise CodexPromptExecutionError(
                f"Cannot prepare the Codex run folder: {exc}",
                code="host_workspace_error",
                stage="host",
                technical_error=str(exc),
            ) from exc

        if not reply:
            detail = (completed.stderr or completed.stdout or "").strip()
            lowered = detail.casefold()
            code = (
                "cli_authentication"
                if any(
                    marker in lowered
                    for marker in ("login", "logged in", "log in", "auth", "credential", "api key")
                )
                else "cli_error"
            )
            raise CodexPromptExecutionError(
                f"Codex CLI returned no reply (exit code {completed.returncode}): {detail[-500:]}",
                code=code,
                stage="execution",
                technical_error=detail[-16_000:],
            )

        raw_sha256 = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        cleaned_reply, was_unwrapped = unwrap_markdown_fence(reply)
        normalizations = ("unwrapped_markdown_code_block",) if was_unwrapped else ()
        try:
            parsed_result = parse_prompt_result(cleaned_reply)
        except (PromptContractError, PromptCompilerError) as exc:
            raise CodexPromptExecutionError(
                f"Failed to parse Codex output into PromptResult: {exc}",
                code="contract_violation",
                stage="normalization",
                technical_error=reply[:16_000],
            ) from exc
        return CodexPromptExecutionResult(
            result=parsed_result,
            bundle=instruction_bundle,
            latency_ms=completed.elapsed_ms,
            raw_response_sha256=raw_sha256,
            response_normalizations=normalizations,
        )


__all__ = [
    "CodexPromptExecutionError",
    "CodexPromptExecutionResult",
    "CodexPromptExecutor",
    "build_codex_command",
    "codex_model",
    "is_codex_profile",
]
