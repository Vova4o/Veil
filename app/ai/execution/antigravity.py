from __future__ import annotations

import hashlib
import logging
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
    TASK_FILE_NAME,
    AgentCLIInputError,
    agent_run_workspace,
    image_file_name,
    render_task_package,
    unwrap_markdown_fence,
    validate_image_path,
    validate_user_input,
)

logger = logging.getLogger("cmv.ai.antigravity")

DEFAULT_ANTIGRAVITY_TIMEOUT_SECONDS = 5 * 60


class AntigravityPromptExecutionError(RuntimeError):
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
class AntigravityPromptExecutionResult:
    result: PromptResult
    bundle: InstructionBundle
    latency_ms: int
    raw_response_sha256: str
    transport: str = "antigravity"
    agent: str = "antigravity-cli"
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


def build_antigravity_command(executable: str, *, model: str | None, timeout_sec: int) -> list[str]:
    """The task package stays in the run folder; the argument is only a pointer to it."""
    command = [
        executable,
        "--print",
        (
            f"Read the file {TASK_FILE_NAME} in the current working directory in full. "
            "It is the authoritative task package. Follow it exactly and reply with only "
            "the output it requires."
        ),
        "--print-timeout",
        f"{timeout_sec}s",
        "--disable-slash-commands",
        "--dangerously-skip-permissions",
        "--sandbox",
    ]
    if model:
        command.extend(["--model", model])
    return command


class AntigravityPromptExecutor:
    """Execute a PromptTask through the Antigravity CLI (agy) in print mode.

    The task package and an optional input image are written into a throwaway
    run folder, so instruction length is not bound by the Windows command line.
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
    ) -> AntigravityPromptExecutionResult:
        if profile.get("kind") != "cli" or profile.get("cli_type") != "antigravity":
            raise AntigravityPromptExecutionError(
                "Profile must be a CLI profile with cli_type='antigravity'.",
                code="invalid_profile",
                stage="preflight",
            )
        try:
            cleaned_input = validate_user_input(user_input)
            validated_image = validate_image_path(image_path)
        except AgentCLIInputError as exc:
            raise AntigravityPromptExecutionError(str(exc), code=exc.code, stage="input") from exc

        executable = find_executable(
            "antigravity", profile.get("executable") or profile.get("custom_path")
        )
        if executable is None:
            logger.error("[Antigravity] Binary (agy) not found on PATH or at the profile path")
            raise AntigravityPromptExecutionError(
                "Antigravity CLI binary (agy) is not installed or not found on PATH.",
                code="antigravity_not_found",
                stage="preflight",
            )

        try:
            instruction_bundle = bundle or self.compiler.compile(task)
        except PromptCompilerError as exc:
            raise AntigravityPromptExecutionError(
                str(exc), code="prompt_compile_error", stage="compile", technical_error=str(exc)
            ) from exc

        timeout_sec = int(profile.get("timeout_seconds") or DEFAULT_ANTIGRAVITY_TIMEOUT_SECONDS)
        model = profile.get("model")
        clean_model = str(model).split("\t")[0].strip() if model else None
        command = build_antigravity_command(executable, model=clean_model, timeout_sec=timeout_sec)
        task_package = render_task_package(
            instruction_bundle, cleaned_input, image_name=image_file_name(validated_image)
        )

        logger.info("[Antigravity] Executing %s (model=%s, timeout=%s)", executable, clean_model, timeout_sec)
        try:
            with agent_run_workspace(
                "cmv-agy-", image_path=validated_image, task_package=task_package
            ) as workspace:
                completed = run_command(
                    command,
                    timeout=timeout_sec,
                    cwd=workspace.root,
                    on_output_chunk=on_output_chunk,
                )
        except CLIIntegrationError as exc:
            logger.error("[Antigravity] CLI execution error: %s", exc)
            raise AntigravityPromptExecutionError(
                f"Antigravity CLI execution failed: {exc}",
                code=exc.code,
                stage="execution",
                technical_error=str(exc),
            ) from exc
        except OSError as exc:
            raise AntigravityPromptExecutionError(
                f"Cannot prepare the Antigravity run folder: {exc}",
                code="host_workspace_error",
                stage="host",
                technical_error=str(exc),
            ) from exc

        raw_output = completed.stdout.strip()
        logger.info("[Antigravity] Completed in %dms with code %d", completed.elapsed_ms, completed.returncode)
        if completed.stderr:
            logger.warning("[Antigravity] STDERR: %s", completed.stderr)

        if completed.returncode != 0 and not raw_output:
            raise AntigravityPromptExecutionError(
                f"Antigravity CLI exited with code {completed.returncode}: {completed.stderr}",
                code="cli_error",
                stage="execution",
                technical_error=completed.stderr,
            )

        raw_sha256 = hashlib.sha256(raw_output.encode("utf-8")).hexdigest()
        cleaned_output, was_unwrapped = unwrap_markdown_fence(raw_output)
        normalizations = ("unwrapped_markdown_code_block",) if was_unwrapped else ()

        try:
            parsed_result = parse_prompt_result(cleaned_output)
        except (PromptContractError, PromptCompilerError) as exc:
            logger.error("[Antigravity] Output parsing failed: %s. Raw output: %s", exc, raw_output[:300])
            raise AntigravityPromptExecutionError(
                f"Failed to parse Antigravity output into PromptResult: {exc}",
                code="contract_violation",
                stage="normalization",
                technical_error=raw_output,
            ) from exc

        return AntigravityPromptExecutionResult(
            result=parsed_result,
            bundle=instruction_bundle,
            latency_ms=completed.elapsed_ms,
            raw_response_sha256=raw_sha256,
            response_normalizations=normalizations,
        )


__all__ = [
    "AntigravityPromptExecutionError",
    "AntigravityPromptExecutionResult",
    "AntigravityPromptExecutor",
    "build_antigravity_command",
]
