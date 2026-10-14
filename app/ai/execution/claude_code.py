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

# Saved profiles use the CLI catalog id "claude"; "claude_code" is the older adapter id.
CLAUDE_CLI_TYPES = {"claude", "claude_code"}
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 5 * 60
# Replaces the coding-agent system prompt. It travels on the command line, so it
# stays short and avoids characters that cmd.exe treats specially.
CLAUDE_SYSTEM_PROMPT = (
    "You execute MediaStudio prompt tasks. The task package arrives on standard input "
    "and is authoritative. Use no tools except reading the input image named in the task. "
    "Reply with exactly the output the task package requires and nothing else."
)


class ClaudeCodePromptExecutionError(RuntimeError):
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
class ClaudeCodePromptExecutionResult:
    result: PromptResult
    bundle: InstructionBundle
    latency_ms: int
    raw_response_sha256: str
    transport: str = "claude_code"
    agent: str = "claude-code"
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


def is_claude_profile(profile: dict[str, Any]) -> bool:
    return profile.get("kind") == "cli" and profile.get("cli_type") in CLAUDE_CLI_TYPES


def build_claude_command(
    executable: str, *, model: str | None, with_image: bool, reasoning_effort: str | None = None
) -> list[str]:
    command = [
        executable,
        "--print",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--system-prompt",
        CLAUDE_SYSTEM_PROMPT,
    ]
    if with_image:
        command.extend(["--tools", "Read", "--allowedTools", "Read"])
    else:
        command.extend(["--tools", ""])
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["--effort", reasoning_effort])
    return command


def parse_claude_json_output(stdout: str) -> str:
    """Extract the final reply from ``claude --print --output-format json``."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCodePromptExecutionError(
            "Claude Code did not return its JSON result envelope.",
            code="incompatible_format",
            stage="host",
            technical_error=stdout[:16_000] or str(exc),
        ) from exc
    if not isinstance(payload, dict):
        raise ClaudeCodePromptExecutionError(
            "Claude Code returned an unexpected JSON value.",
            code="incompatible_format",
            stage="host",
            technical_error=stdout[:16_000],
        )
    result = payload.get("result")
    if payload.get("is_error") or payload.get("subtype", "success") != "success":
        detail = result if isinstance(result, str) and result.strip() else stdout
        lowered = detail.casefold()
        code = (
            "cli_authentication"
            if any(marker in lowered for marker in ("login", "auth", "credential", "api key"))
            else "provider_error"
        )
        raise ClaudeCodePromptExecutionError(
            f"Claude Code reported an error: {detail[:500]}",
            code=code,
            stage="host",
            technical_error=detail[:16_000],
        )
    if not isinstance(result, str) or not result.strip():
        raise ClaudeCodePromptExecutionError(
            "Claude Code returned no reply text.",
            code="empty_result",
            stage="host",
            technical_error=stdout[:16_000],
        )
    return result.strip()


class ClaudeCodePromptExecutor:
    """Execute a PromptTask through the Claude Code CLI in print mode.

    The task package goes through stdin, so instruction length is not bound by
    the Windows command line. An input image is copied into a throwaway run
    folder and Claude Code reads it with its Read tool, the only tool enabled.
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
    ) -> ClaudeCodePromptExecutionResult:
        if not is_claude_profile(profile):
            raise ClaudeCodePromptExecutionError(
                "Profile must be a Claude Code CLI profile.",
                code="invalid_profile",
                stage="preflight",
            )
        try:
            cleaned_input = validate_user_input(user_input)
            validated_image = validate_image_path(image_path)
        except AgentCLIInputError as exc:
            raise ClaudeCodePromptExecutionError(str(exc), code=exc.code, stage="input") from exc

        executable = find_executable(
            "claude", profile.get("executable") or profile.get("custom_path")
        )
        if executable is None:
            raise ClaudeCodePromptExecutionError(
                "Claude Code CLI binary is not installed or not found on PATH.",
                code="claude_code_not_found",
                stage="preflight",
            )

        try:
            instruction_bundle = bundle or self.compiler.compile(task)
        except PromptCompilerError as exc:
            raise ClaudeCodePromptExecutionError(
                str(exc), code="prompt_compile_error", stage="compile", technical_error=str(exc)
            ) from exc

        timeout_sec = int(profile.get("timeout_seconds") or DEFAULT_CLAUDE_TIMEOUT_SECONDS)
        model = profile.get("model")
        command = build_claude_command(
            executable,
            model=model.strip() if isinstance(model, str) and model.strip() else None,
            with_image=validated_image is not None,
            reasoning_effort=profile.get("reasoning_effort"),
        )
        task_package = render_task_package(
            instruction_bundle, cleaned_input, image_name=image_file_name(validated_image)
        )
        try:
            with agent_run_workspace("mediastudio-claude-", image_path=validated_image) as workspace:
                completed = run_command(
                    command,
                    timeout=timeout_sec,
                    cwd=workspace.root,
                    input_text=task_package,
                    # A MediaStudio started from a Claude Code session inherits this marker,
                    # and Claude Code refuses to start nested sessions while it is set.
                    env_overrides={"CLAUDECODE": None},
                )
        except CLIIntegrationError as exc:
            raise ClaudeCodePromptExecutionError(
                f"Claude Code CLI execution failed: {exc}",
                code=exc.code,
                stage="execution",
                technical_error=str(exc),
            ) from exc
        except OSError as exc:
            raise ClaudeCodePromptExecutionError(
                f"Cannot prepare the Claude Code run folder: {exc}",
                code="host_workspace_error",
                stage="host",
                technical_error=str(exc),
            ) from exc

        raw_output = completed.stdout.strip()
        if completed.returncode != 0 and not raw_output:
            raise ClaudeCodePromptExecutionError(
                f"Claude Code CLI exited with code {completed.returncode}: {completed.stderr}",
                code="cli_error",
                stage="execution",
                technical_error=completed.stderr,
            )

        reply = parse_claude_json_output(raw_output)
        raw_sha256 = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        cleaned_reply, was_unwrapped = unwrap_markdown_fence(reply)
        normalizations = ("unwrapped_markdown_code_block",) if was_unwrapped else ()

        try:
            parsed_result = parse_prompt_result(cleaned_reply)
        except (PromptContractError, PromptCompilerError) as exc:
            raise ClaudeCodePromptExecutionError(
                f"Failed to parse Claude Code output into PromptResult: {exc}",
                code="contract_violation",
                stage="normalization",
                technical_error=reply[:16_000],
            ) from exc

        return ClaudeCodePromptExecutionResult(
            result=parsed_result,
            bundle=instruction_bundle,
            latency_ms=completed.elapsed_ms,
            raw_response_sha256=raw_sha256,
            response_normalizations=normalizations,
        )


__all__ = [
    "CLAUDE_CLI_TYPES",
    "ClaudeCodePromptExecutionError",
    "ClaudeCodePromptExecutionResult",
    "ClaudeCodePromptExecutor",
    "build_claude_command",
    "is_claude_profile",
    "parse_claude_json_output",
]
