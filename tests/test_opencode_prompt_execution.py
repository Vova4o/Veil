from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ai.cli import CommandResult
from app.ai.execution import (
    OpenCodePromptExecutionError,
    OpenCodePromptExecutionResult,
    OpenCodePromptExecutor,
)
from app.ai.opencode_smoke import (
    resolve_opencode_profile,
    run_opencode_smoke_scenario,
)
from app.ai.prompting import (
    PromptCompiler,
    PromptFamily,
    PromptModifier,
    PromptOperation,
    PromptResult,
    PromptScenario,
    PromptTask,
)
from app.ai.providers import AIProviderStore
from app.ai.secrets import SecretStoreStatus
from app.ai.smoke import SmokeRunnerError


class MemorySecretStore:
    def status(self) -> SecretStoreStatus:
        return SecretStoreStatus(
            available=True,
            backend="tests.MemorySecretStore",
            message="Test credential store",
        )

    def get(self, _profile_id: str) -> str | None:
        return None

    def set(self, _profile_id: str, _value: str) -> None:
        return None

    def delete(self, _profile_id: str) -> None:
        return None


def opencode_profile(**overrides) -> dict:
    profile = {
        "id": "opencode-profile",
        "kind": "cli",
        "name": "OpenCode Smoke",
        "model": "provider/model",
        "timeout_seconds": 60,
        "multimodal": True,
        "cli_type": "opencode",
        "executable": "C:/tools/opencode.cmd",
    }
    profile.update(overrides)
    return profile


def portrait_task() -> PromptTask:
    return PromptTask(
        family=PromptFamily.FLUX,
        operation=PromptOperation.GENERATE,
        scenario=PromptScenario.PORTRAIT,
        modifiers=(PromptModifier.SAFE,),
    )


class OpenCodePromptExecutorTest(unittest.TestCase):
    def test_execute_raw_supports_a_strict_non_prompt_contract(self) -> None:
        raw_result = '{"schema_version":"1","subjects":[]}'
        event_output = json.dumps({"part": {"text": raw_result}})
        captured = {}

        def fake_run_command(args, *, timeout, cwd=None, **_kwargs):
            workspace = Path(cwd)
            captured["task"] = (workspace / "mediastudio-task.md").read_text(
                encoding="utf-8"
            )
            return CommandResult(
                returncode=0,
                stdout=event_output,
                stderr="",
                elapsed_ms=14,
            )

        with (
            patch(
                "app.ai.execution.opencode.find_executable",
                return_value="C:/tools/opencode.cmd",
            ),
            patch(
                "app.ai.execution.opencode.run_command",
                side_effect=fake_run_command,
            ),
        ):
            executed = OpenCodePromptExecutor().execute_raw(
                profile=opencode_profile(multimodal=False),
                task_package="# SceneSpec\nReturn the strict SceneSpec object.",
            )

        self.assertEqual(executed.text, raw_result)
        self.assertEqual(executed.latency_ms, 14)
        self.assertIn("Return the strict SceneSpec object.", captured["task"])

    def test_execute_uses_isolated_tool_denied_agent_and_task_attachment(self) -> None:
        raw_result = json.dumps({
            "schema_version": "1",
            "positive_prompt": "studio portrait, warm side light",
            "negative_prompt": "watermark",
        })
        event_output = json.dumps({"part": {"text": raw_result}})
        captured: dict = {}

        def fake_run_command(args, *, timeout, cwd=None, **_kwargs):
            workspace = Path(cwd)
            captured["args"] = list(args)
            captured["timeout"] = timeout
            captured["workspace_exists"] = workspace.is_dir()
            captured["config"] = json.loads(
                (workspace / "opencode.json").read_text(encoding="utf-8")
            )
            captured["task"] = (workspace / "mediastudio-task.md").read_text(
                encoding="utf-8"
            )
            file_idx = args.index("--file")
            captured["task_arg_exists"] = Path(args[file_idx + 1]).is_file()
            return CommandResult(
                returncode=0,
                stdout=event_output,
                stderr="",
                elapsed_ms=41,
            )

        with (
            patch(
                "app.ai.execution.opencode.find_executable",
                return_value="C:/OpenCode/resources/opencode-cli.exe",
            ),
            patch(
                "app.ai.execution.opencode.run_command",
                side_effect=fake_run_command,
            ),
        ):
            executed = OpenCodePromptExecutor().execute(
                profile=opencode_profile(multimodal=False),
                task=portrait_task(),
                user_input="Create a calm studio portrait.",
            )

        self.assertEqual(executed.transport, "opencode")
        self.assertEqual(executed.latency_ms, 41)
        self.assertEqual(
            executed.result.positive_prompt,
            "studio portrait, warm side light",
        )
        self.assertEqual(executed.response_normalizations, ())
        self.assertRegex(executed.raw_response_sha256, r"^[0-9a-f]{64}$")
        self.assertTrue(captured["workspace_exists"])
        self.assertTrue(captured["task_arg_exists"])
        self.assertEqual(
            captured["config"]["agent"]["mediastudio-prompt-smoke"]["permission"],
            {"*": "deny"},
        )
        self.assertEqual(captured["config"]["share"], "disabled")
        self.assertIn("INSTRUCTION PRECEDENCE", captured["task"])
        self.assertIn("USER TASK INPUT", captured["task"])
        self.assertIn("Create a calm studio portrait.", captured["task"])
        self.assertIn("--standalone", captured["args"])
        self.assertIn("mediastudio-prompt-smoke", captured["args"])
        self.assertIn("provider/model", captured["args"])
        self.assertEqual(captured["timeout"], 300)

    def test_zen_models_run_on_the_stock_agent_through_the_desktop_cli(self) -> None:
        raw_result = json.dumps({"positive_prompt": "a garden cottage", "negative_prompt": ""})
        events = "\n".join([
            json.dumps({"type": "text", "part": {"text": "Reading the task."}}),
            json.dumps({"type": "text", "part": {"text": raw_result}}),
        ])
        captured: dict = {}

        def fake_run_command(args, *, timeout, cwd=None, **kwargs):
            captured["args"] = list(args)
            captured["kwargs"] = kwargs
            captured["config"] = json.loads((Path(cwd) / "opencode.json").read_text(encoding="utf-8"))
            captured["config_home_exists"] = Path(kwargs["env_overrides"]["XDG_CONFIG_HOME"]).is_dir()
            return CommandResult(returncode=0, stdout=events, stderr="", elapsed_ms=30)

        with (
            patch(
                "app.ai.execution.opencode.find_executable",
                return_value="C:/OpenCode/resources/opencode-cli.exe",
            ),
            patch("app.ai.opencode_host.read_version", return_value="2.0.24"),
            patch("app.ai.execution.opencode.run_command", side_effect=fake_run_command),
        ):
            executed = OpenCodePromptExecutor().execute(
                profile=opencode_profile(model="opencode/muse-spark-1.3-contributor-free"),
                task=portrait_task(),
                user_input="Create a portrait.",
            )

        # The stock agent spoke before its answer; the final JSON part still parses.
        self.assertEqual(executed.result.positive_prompt, "a garden cottage")
        self.assertEqual(executed.agent, "plan")
        args = captured["args"]
        self.assertEqual(args[1:3], ["run", "--standalone"])
        self.assertEqual(args[args.index("--agent") + 1], "plan")
        self.assertNotIn("--pure", args)
        self.assertNotIn("agent", captured["config"])
        self.assertEqual(captured["config"]["share"], "disabled")
        self.assertTrue(captured["config_home_exists"])

    def test_free_tier_refusal_has_its_own_code(self) -> None:
        refusal = json.dumps({"type": "error", "error": {"name": "APIError", "data": {
            "message": "OpenCode's free tier can only be used from within OpenCode"}}})
        with (
            patch("app.ai.execution.opencode.find_executable", return_value="C:/tools/opencode.cmd"),
            patch("app.ai.opencode_host.read_version", return_value="1.18.34"),
            patch(
                "app.ai.execution.opencode.run_command",
                return_value=CommandResult(returncode=1, stdout=refusal, stderr="", elapsed_ms=9),
            ),
            self.assertRaises(OpenCodePromptExecutionError) as caught,
        ):
            OpenCodePromptExecutor().execute(
                profile=opencode_profile(model="opencode/muse-free", multimodal=False),
                task=portrait_task(),
                user_input="Create a portrait.",
            )
        self.assertEqual(caught.exception.code, "free_tier_refused")

    def test_legacy_timeout_is_extended_but_explicit_values_are_preserved(self) -> None:
        self.assertEqual(
            OpenCodePromptExecutor._resolve_timeout(opencode_profile()),
            300,
        )
        self.assertEqual(
            OpenCodePromptExecutor._resolve_timeout(
                opencode_profile(timeout_seconds=420)
            ),
            420,
        )

    def test_invalid_profile_is_rejected_before_cli_lookup(self) -> None:
        with patch("app.ai.execution.opencode.find_executable") as executable:
            with self.assertRaises(OpenCodePromptExecutionError) as caught:
                OpenCodePromptExecutor().execute(
                    profile=opencode_profile(kind="openai_compatible"),
                    task=portrait_task(),
                    user_input="Create a portrait.",
                )
        self.assertEqual(caught.exception.code, "incompatible_profile")
        self.assertEqual(caught.exception.stage, "input")
        executable.assert_not_called()

    def test_single_markdown_json_fence_is_normalized_at_host_boundary(self) -> None:
        output = json.dumps({
            "part": {
                "text": (
                    '```json\n'
                    '{"schema_version":"1","positive_prompt":"portrait",'
                    '"negative_prompt":""}\n'
                    '```'
                )
            }
        })
        with (
            patch(
                "app.ai.execution.opencode.find_executable",
                return_value="C:/tools/opencode.cmd",
            ),
            patch(
                "app.ai.execution.opencode.run_command",
                return_value=CommandResult(
                    returncode=0,
                    stdout=output,
                    stderr="",
                    elapsed_ms=20,
                ),
            ),
        ):
            executed = OpenCodePromptExecutor().execute(
                profile=opencode_profile(multimodal=False),
                task=portrait_task(),
                user_input="Create a portrait.",
            )
        self.assertEqual(executed.result.positive_prompt, "portrait")
        self.assertEqual(
            executed.response_normalizations,
            ("markdown_json_fence_removed",),
        )
        self.assertEqual(
            executed.metadata()["response_normalizations"],
            ["markdown_json_fence_removed"],
        )

    def test_markdown_fence_with_commentary_remains_a_contract_failure(self) -> None:
        output = json.dumps({
            "part": {
                "text": (
                    'Here is the result:\n```json\n'
                    '{"schema_version":"1","positive_prompt":"portrait",'
                    '"negative_prompt":""}\n```'
                )
            }
        })
        with (
            patch(
                "app.ai.execution.opencode.find_executable",
                return_value="C:/tools/opencode.cmd",
            ),
            patch(
                "app.ai.execution.opencode.run_command",
                return_value=CommandResult(
                    returncode=0,
                    stdout=output,
                    stderr="",
                    elapsed_ms=20,
                ),
            ),self.assertRaises(OpenCodePromptExecutionError) as caught
        ):
            OpenCodePromptExecutor().execute(
                profile=opencode_profile(multimodal=False),
                task=portrait_task(),
                user_input="Create a portrait.",
            )
        self.assertEqual(caught.exception.code, "markdown_wrapped_json")
        self.assertEqual(caught.exception.stage, "contract")

    def test_nonzero_cli_result_preserves_authentication_category(self) -> None:
        with (
            patch(
                "app.ai.execution.opencode.find_executable",
                return_value="C:/tools/opencode.cmd",
            ),
            patch(
                "app.ai.execution.opencode.run_command",
                return_value=CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="Authentication required: run opencode auth login",
                    elapsed_ms=12,
                ),
            ),self.assertRaises(OpenCodePromptExecutionError) as caught
        ):
            OpenCodePromptExecutor().execute(
                profile=opencode_profile(multimodal=False),
                task=portrait_task(),
                user_input="Create a portrait.",
            )
        self.assertEqual(caught.exception.code, "cli_authentication")
        self.assertEqual(caught.exception.stage, "host")


class OpenCodeSmokeRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = AIProviderStore(
            Path(self.temp_dir.name) / "config.json",
            secret_store=MemorySecretStore(),
        )

    def test_resolver_uses_the_default_or_the_picked_opencode_model(self) -> None:
        def card(model_id, variant=None):
            return {"id": f"opencode:{model_id}", "model": model_id, "cli_type": "opencode", "kind": "cli",
                    "model_card": True, "multimodal": model_id != "provider/text", "variant": variant}

        with (
            patch("app.ai.opencode_smoke.agents.opencode_profile", side_effect=card),
            patch("app.ai.opencode_smoke.agents.implicit_opencode_profile", return_value=None),
        ):
            with self.assertRaisesRegex(SmokeRunnerError, "no free model"):
                resolve_opencode_profile(self.store, selector=None, requires_image=False)
            self.store.set_default_model({"host": "opencode", "model": "provider/model", "variant": "high"})
            text = resolve_opencode_profile(self.store, selector=None, requires_image=False)
            picked = resolve_opencode_profile(self.store, selector="provider/other#low", requires_image=True)
            with self.assertRaisesRegex(SmokeRunnerError, "does not see photos"):
                resolve_opencode_profile(self.store, selector="provider/text", requires_image=True)

        self.assertEqual((text.profile["model"], text.profile["variant"]), ("provider/model", "high"))
        self.assertEqual(text.selected_by, "default model")
        self.assertEqual((picked.profile["model"], picked.profile["variant"]), ("provider/other", "low"))
        self.assertEqual(picked.selected_by, "explicit model")

    def test_runner_uses_fake_host_executor_without_network(self) -> None:
        profile = opencode_profile(multimodal=False)

        class FakeExecutor:
            def execute(self, **kwargs):
                bundle = PromptCompiler().compile(kwargs["task"])
                return OpenCodePromptExecutionResult(
                    result=PromptResult(
                        positive_prompt="studio portrait",
                        negative_prompt="",
                    ),
                    bundle=bundle,
                    latency_ms=9,
                    raw_response_sha256="3" * 64,
                )

        report = run_opencode_smoke_scenario(
            scenario_id="flux-portrait-generate",
            profile=profile,
            executor=FakeExecutor(),
            checkpoint_profile="host-checkpoint",
        )

        self.assertFalse(report.failed)
        self.assertEqual(report.execution.transport, "opencode")
        self.assertEqual(
            report.execution.bundle.task.checkpoint_profile,
            "host-checkpoint",
        )
        serialized = report.to_dict()
        self.assertEqual(serialized["execution"]["transport"], "opencode")
        self.assertNotIn("api_key", serialized["profile"])


if __name__ == "__main__":
    unittest.main()
