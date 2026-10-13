import json
import sys
from pathlib import Path

import pytest

from app.ai.cli import CommandResult, run_command
from app.ai.execution import claude_code as claude_module
from app.ai.execution.adapters import (
    AntigravityAgentHostAdapter,
    ClaudeCodeAgentHostAdapter,
)
from app.ai.execution.agent_cli import unwrap_markdown_fence
from app.ai.execution.antigravity import (
    AntigravityPromptExecutionError,
    AntigravityPromptExecutor,
    parse_antigravity_json_output,
)
from app.ai.execution.claude_code import (
    ClaudeCodePromptExecutionError,
    ClaudeCodePromptExecutor,
)
from app.ai.execution.router import ExecutionRouter
from app.ai.prompting import PromptFamily, PromptOperation, PromptScenario, PromptTask

LONG_INPUT_CHARS = 60_000
VISION_IMAGE = Path(__file__).resolve().parents[1] / "app" / "ai" / "assets" / "cmv-vision-test-garden.jpg"


def _task() -> PromptTask:
    return PromptTask(
        operation=PromptOperation.GENERATE,
        family=PromptFamily.FLUX,
        scenario=PromptScenario.SINGLE_CHARACTER,
    )


def _claude_envelope(result: str, **extra) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result, **extra})


class _RecordingRunner:
    """Stand-in for run_command that records the call and inspects the run folder."""

    def __init__(self, stdout: str):
        self.stdout = stdout
        self.calls = []

    def __call__(self, args, **kwargs):
        cwd = Path(kwargs["cwd"])
        self.calls.append({
            "args": list(args),
            "kwargs": kwargs,
            "files": sorted(path.name for path in cwd.iterdir()),
        })
        return CommandResult(returncode=0, stdout=self.stdout, stderr="", elapsed_ms=12)


def test_claude_code_executor_profile_validation():
    executor = ClaudeCodePromptExecutor()
    with pytest.raises(ClaudeCodePromptExecutionError) as exc_info:
        executor.execute(
            profile={"kind": "openai_compatible"},
            task=_task(),
            user_input="test prompt",
        )
    assert exc_info.value.code == "invalid_profile"


def test_antigravity_executor_profile_validation():
    executor = AntigravityPromptExecutor()
    with pytest.raises(AntigravityPromptExecutionError) as exc_info:
        executor.execute(
            profile={"kind": "cli", "cli_type": "opencode"},
            task=_task(),
            user_input="test prompt",
        )
    assert exc_info.value.code == "invalid_profile"


def test_router_selects_claude_and_antigravity():
    router = ExecutionRouter()

    for cli_type in ("claude", "claude_code"):
        adapter = router._select_adapter({"id": "p1", "kind": "cli", "cli_type": cli_type})
        assert isinstance(adapter, ClaudeCodeAgentHostAdapter)

    antigravity_adapter = router._select_adapter({"id": "p2", "kind": "cli", "cli_type": "antigravity"})
    assert isinstance(antigravity_adapter, AntigravityAgentHostAdapter)


def test_claude_adapter_accepts_image_files():
    assert ClaudeCodeAgentHostAdapter.capabilities.supports_images is True
    assert ClaudeCodeAgentHostAdapter.capabilities.image_inputs == ("file_path",)


def test_run_command_delivers_long_input_through_stdin(monkeypatch):
    monkeypatch.setenv("CMV_TEST_REMOVED", "1")
    script = (
        "import os, sys; data = sys.stdin.read(); "
        "print(len(data), os.environ.get('CMV_TEST_REMOVED', 'gone'), os.environ['CMV_TEST_SET'])"
    )
    result = run_command(
        [sys.executable, "-c", script],
        timeout=30,
        input_text="x" * LONG_INPUT_CHARS,
        env_overrides={"CMV_TEST_REMOVED": None, "CMV_TEST_SET": "yes"},
    )
    assert result.returncode == 0
    assert result.stdout.split() == [str(LONG_INPUT_CHARS), "gone", "yes"]


def test_claude_executor_sends_long_bundle_through_stdin(monkeypatch):
    runner = _RecordingRunner(
        _claude_envelope('```json\n{"schema_version": "1", "positive_prompt": "a red fox", "negative_prompt": ""}\n```')
    )
    monkeypatch.setattr(claude_module, "run_command", runner)
    monkeypatch.setattr(claude_module, "find_executable", lambda *_: "C:/tools/claude.cmd")
    long_input = "describe the scene " * 3_000

    executed = ClaudeCodePromptExecutor().execute(
        profile={"kind": "cli", "cli_type": "claude", "model": "sonnet", "timeout_seconds": 120},
        task=_task(),
        user_input=long_input,
    )

    call = runner.calls[0]
    command_line_chars = sum(len(part) for part in call["args"])
    assert command_line_chars < 8_000
    assert call["args"][:2] == ["C:/tools/claude.cmd", "--print"]
    assert call["args"][call["args"].index("--tools") + 1] == ""
    assert call["args"][call["args"].index("--model") + 1] == "sonnet"
    assert long_input.strip() in call["kwargs"]["input_text"]
    assert "COMPILED TASK" in call["kwargs"]["input_text"]
    assert call["kwargs"]["env_overrides"] == {"CLAUDECODE": None}
    assert call["kwargs"]["timeout"] == 120
    assert call["files"] == []
    assert executed.result.positive_prompt == "a red fox"
    assert executed.response_normalizations == ("unwrapped_markdown_code_block",)


def test_claude_executor_puts_image_in_run_folder_and_enables_read(monkeypatch):
    runner = _RecordingRunner(
        _claude_envelope('{"schema_version": "1", "positive_prompt": "a garden", "negative_prompt": ""}')
    )
    monkeypatch.setattr(claude_module, "run_command", runner)
    monkeypatch.setattr(claude_module, "find_executable", lambda *_: "C:/tools/claude.cmd")

    executed = ClaudeCodePromptExecutor().execute(
        profile={"kind": "cli", "cli_type": "claude"},
        task=_task(),
        user_input="describe this photo",
        image_path=VISION_IMAGE,
    )

    call = runner.calls[0]
    assert call["files"] == ["input.jpg"]
    tools = call["args"][call["args"].index("--tools") + 1]
    allowed = call["args"][call["args"].index("--allowedTools") + 1]
    assert (tools, allowed) == ("Read", "Read")
    assert "`input.jpg`" in call["kwargs"]["input_text"]
    assert executed.result.positive_prompt == "a garden"


def test_claude_executor_uses_profile_executable(monkeypatch):
    seen = []

    def fake_find(cli_type, requested=None):
        seen.append((cli_type, requested))
        return requested

    monkeypatch.setattr(claude_module, "find_executable", fake_find)
    monkeypatch.setattr(
        claude_module,
        "run_command",
        _RecordingRunner(_claude_envelope('{"schema_version": "1", "positive_prompt": "x"}')),
    )
    ClaudeCodePromptExecutor().execute(
        profile={"kind": "cli", "cli_type": "claude", "executable": "D:/bin/claude.cmd"},
        task=_task(),
        user_input="hello",
    )
    assert seen == [("claude", "D:/bin/claude.cmd")]


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ("Invalid API key · Please run /login", "cli_authentication"),
        ("Overloaded", "provider_error"),
    ],
)
def test_claude_executor_reports_error_envelopes(monkeypatch, result, code):
    envelope = json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": result})
    monkeypatch.setattr(claude_module, "run_command", _RecordingRunner(envelope))
    monkeypatch.setattr(claude_module, "find_executable", lambda *_: "C:/tools/claude.cmd")
    with pytest.raises(ClaudeCodePromptExecutionError) as exc_info:
        ClaudeCodePromptExecutor().execute(
            profile={"kind": "cli", "cli_type": "claude"},
            task=_task(),
            user_input="hello",
        )
    assert exc_info.value.code == code


def test_claude_executor_rejects_non_json_stdout(monkeypatch):
    monkeypatch.setattr(claude_module, "run_command", _RecordingRunner("plain text"))
    monkeypatch.setattr(claude_module, "find_executable", lambda *_: "C:/tools/claude.cmd")
    with pytest.raises(ClaudeCodePromptExecutionError) as exc_info:
        ClaudeCodePromptExecutor().execute(
            profile={"kind": "cli", "cli_type": "claude"},
            task=_task(),
            user_input="hello",
        )
    assert exc_info.value.code == "incompatible_format"


@pytest.mark.parametrize(
    ("text", "expected", "unwrapped"),
    [
        ('{"a": 1}', '{"a": 1}', False),
        ('```json\n{"a": 1}\n```', '{"a": 1}', True),
        ('Here it is:\n```\nprompt text\n```\nDone.', "prompt text", True),
    ],
)
def test_unwrap_markdown_fence(text, expected, unwrapped):
    assert unwrap_markdown_fence(text) == (expected, unwrapped)


def test_claude_vision_connection_test_reads_image_from_run_folder(monkeypatch):
    from app.ai import cli as cli_module

    calls = []

    def fake_run(args, **kwargs):
        calls.append({"args": list(args), "kwargs": kwargs, "files": sorted(p.name for p in Path(kwargs["cwd"]).iterdir())})
        return CommandResult(returncode=0, stdout=_claude_envelope("CMV_OK"), stderr="", elapsed_ms=9)

    monkeypatch.setattr(cli_module, "run_command", fake_run)
    monkeypatch.setattr(cli_module, "find_executable", lambda *_: "C:/tools/claude.cmd")
    result = cli_module.run_cli_test(
        {"cli_type": "claude", "model": "sonnet", "timeout_seconds": 60}, multimodal=True
    )

    call = calls[0]
    assert call["files"] == ["input.jpg"]
    assert call["args"][call["args"].index("--tools") + 1] == "Read"
    assert call["kwargs"]["env_overrides"] == {"CLAUDECODE": None}
    assert result["response_preview"] == "CMV_OK"


def test_antigravity_executor_writes_task_file_and_image(monkeypatch):
    from app.ai.execution import antigravity as agy_module

    seen = {}

    def fake_run(args, **kwargs):
        cwd = Path(kwargs["cwd"])
        seen["args"] = list(args)
        seen["files"] = sorted(path.name for path in cwd.iterdir())
        seen["task"] = (cwd / "cmv-task.md").read_text(encoding="utf-8")
        seen["schema"] = json.loads((cwd / "cmv-result.schema.json").read_text(encoding="utf-8"))
        return CommandResult(
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "structured_output": {
                "schema_version": "1", "positive_prompt": "a garden path", "negative_prompt": "",
                "edit_profile": None, "note": "",
            }, "response": "unreliable transcript with toolAction/toolSummary"}),
            stderr="",
            elapsed_ms=7,
        )

    monkeypatch.setattr(agy_module, "run_command", fake_run)
    monkeypatch.setattr(agy_module, "find_executable", lambda *_: "C:/tools/agy.exe")
    long_input = "describe the garden " * 3_000

    executed = AntigravityPromptExecutor().execute(
        profile={"kind": "cli", "cli_type": "antigravity", "model": "gemini-3-pro\tGemini 3 Pro", "timeout_seconds": 90},
        task=_task(),
        user_input=long_input,
        image_path=VISION_IMAGE,
    )

    assert sum(len(part) for part in seen["args"]) < 8_000
    assert seen["files"] == ["cmv-result.schema.json", "cmv-task.md", "input.jpg"]
    assert long_input.strip() in seen["task"]
    assert "`input.jpg`" in seen["task"]
    assert seen["args"][seen["args"].index("--model") + 1] == "gemini-3-pro"
    assert seen["args"][seen["args"].index("--print-timeout") + 1] == "90s"
    assert seen["args"][seen["args"].index("--output-format") + 1] == "json"
    assert seen["args"][seen["args"].index("--json-schema") + 1] == "cmv-result.schema.json"
    assert set(seen["schema"]["required"]) == {"schema_version", "positive_prompt", "negative_prompt", "edit_profile", "note"}
    assert seen["schema"]["additionalProperties"] is False
    assert executed.result.positive_prompt == "a garden path"
    assert executed.response_normalizations == ("extracted_antigravity_structured_output",)


@pytest.mark.parametrize(("payload", "code"), [
    ("not JSON", "incompatible_format"),
    ("[]", "incompatible_format"),
    ('{"status":"ERROR","response":"provider failed","structured_output":{"positive_prompt":"x"}}', "provider_error"),
    ('{"status":"SUCCESS","response":"{\\"positive_prompt\\":\\"x\\"}"}', "incompatible_format"),
    ('{"status":"SUCCESS","structured_output":"x"}', "incompatible_format"),
])
def test_antigravity_requires_successful_schema_checked_output(payload, code):
    with pytest.raises(AntigravityPromptExecutionError) as caught:
        parse_antigravity_json_output(payload)
    assert caught.value.code == code


@pytest.mark.parametrize(("returncode", "structured", "code"), [
    (1, {"positive_prompt": "A cup."}, "cli_error"),
    (0, {"schema_version": 1, "positive_prompt": "A cup."}, "contract_violation"),
    (0, {"positive_prompt": "A cup.", "toolSummary": "done"}, "contract_violation"),
])
def test_antigravity_rejects_failed_processes_and_invalid_structured_results(monkeypatch, returncode, structured, code):
    from app.ai.execution import antigravity as agy_module

    monkeypatch.setattr(agy_module, "find_executable", lambda *_: "C:/tools/agy.exe")
    monkeypatch.setattr(agy_module, "run_command", lambda *_, **__: CommandResult(
        returncode=returncode, stdout=json.dumps({"status": "SUCCESS", "structured_output": structured}),
        stderr="", elapsed_ms=1,
    ))
    with pytest.raises(AntigravityPromptExecutionError) as caught:
        AntigravityPromptExecutor().execute(
            profile={"kind": "cli", "cli_type": "antigravity"}, task=_task(), user_input="A cup.",
        )
    assert caught.value.code == code
    assert caught.value.technical_error
