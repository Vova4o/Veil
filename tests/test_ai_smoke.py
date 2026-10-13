from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.ai import agents
from app.ai.execution import DirectPromptExecutionResult
from app.ai.prompting import PromptCompiler, PromptResult
from app.ai.providers import AIProviderStore
from app.ai.secrets import SecretStoreStatus
from app.ai.smoke import (
    SCENARIOS,
    ResolvedSmokeProfile,
    SmokeRunnerError,
    evaluate_smoke_checks,
    load_smoke_image,
    main,
    resolve_smoke_profile,
    run_smoke_scenario,
    scenario_by_id,
)
from app.ai.transport import TEST_PNG_BASE64


class MemorySecretStore:
    def __init__(self):
        self.values: dict[str, str] = {}

    def status(self) -> SecretStoreStatus:
        return SecretStoreStatus(
            available=True,
            backend="tests.MemorySecretStore",
            message="Test credential store",
        )

    def get(self, profile_id: str) -> str | None:
        return self.values.get(profile_id)

    def set(self, profile_id: str, value: str) -> None:
        self.values[profile_id] = value

    def delete(self, profile_id: str) -> None:
        self.values.pop(profile_id, None)


def provider_payload(provider_id: str) -> dict:
    return {
        "id": provider_id,
        "name": provider_id.title(),
        "base_url": "https://provider.example/v1",
        "api_key_source": "system",
        "api_key": f"secret-{provider_id}",
    }


# The models.dev cards the provider's /models list is matched with.
CARDS = {
    "text-model": {"id": "text-model", "name": "Text model", "vision": False},
    "vision-model": {"id": "vision-model", "name": "Vision model", "vision": True},
}


class AISmokeRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.secrets = MemorySecretStore()
        self.store = AIProviderStore(self.config_path, secret_store=self.secrets)
        self.store.create(provider_payload("acme"))
        agents._provider_models_cache.clear()
        self.addCleanup(agents._provider_models_cache.clear)
        for patcher in (
            patch("app.ai.agents.list_openai_compatible_models", return_value=list(CARDS)),
            patch("app.ai.agents.provider_catalog.model_cards", return_value=CARDS),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_registry_contains_text_and_multimodal_scenarios(self) -> None:
        self.assertEqual(len(SCENARIOS), 4)
        self.assertFalse(SCENARIOS["flux-portrait-generate"].requires_image)
        self.assertTrue(SCENARIOS["flux-graphic-text-reconstruct"].requires_image)
        self.assertEqual(
            scenario_by_id("pony-portrait-generate").task.family.value,
            "pony",
        )
        with self.assertRaises(SmokeRunnerError):
            scenario_by_id("not-a-scenario")

    def test_model_resolution_uses_the_default_api_model(self) -> None:
        with self.assertRaisesRegex(SmokeRunnerError, "not an API provider model"):
            resolve_smoke_profile(self.store, selector=None, requires_image=False)
        self.store.set_default_model({"host": "api:acme", "model": "vision-model"})

        resolved = resolve_smoke_profile(self.store, selector=None, requires_image=True)

        self.assertEqual(resolved.profile["model"], "vision-model")
        self.assertEqual(resolved.profile["base_url"], "https://provider.example/v1")
        self.assertEqual(resolved.api_key, "secret-acme")
        self.assertEqual(resolved.selected_by, "default model")

    def test_model_can_be_selected_as_provider_and_model(self) -> None:
        resolved = resolve_smoke_profile(self.store, selector="acme/text-model", requires_image=False)
        self.assertEqual(resolved.profile["model"], "text-model")
        self.assertEqual(resolved.selected_by, "explicit model")
        with self.assertRaisesRegex(SmokeRunnerError, "no model"):
            resolve_smoke_profile(self.store, selector="acme/missing", requires_image=False)

    def test_multimodal_scenario_rejects_text_only_model(self) -> None:
        with self.assertRaisesRegex(SmokeRunnerError, "does not see photos"):
            resolve_smoke_profile(self.store, selector="acme/text-model", requires_image=True)

    def test_image_loader_builds_local_data_url_and_hash(self) -> None:
        image_path = Path(self.temp_dir.name) / "test.png"
        payload = base64.b64decode(TEST_PNG_BASE64)
        image_path.write_bytes(payload)

        loaded = load_smoke_image(image_path)

        self.assertEqual(loaded.path, image_path.resolve())
        self.assertEqual(loaded.byte_count, len(payload))
        self.assertTrue(loaded.data_url.startswith("data:image/png;base64,"))
        self.assertRegex(loaded.sha256, r"^[0-9a-f]{64}$")

    def test_pony_checks_detect_required_tokens(self) -> None:
        scenario = SCENARIOS["pony-portrait-generate"]
        bundle = PromptCompiler().compile(scenario.task)
        execution = DirectPromptExecutionResult(
            result=PromptResult(
                positive_prompt=(
                    "score_9, score_8_up, score_7_up, score_6_up, "
                    "score_5_up, score_4_up, source_anime, rating_safe, 1girl"
                ),
                negative_prompt="",
            ),
            bundle=bundle,
            latency_ms=12,
            raw_response_sha256="0" * 64,
        )

        checks = evaluate_smoke_checks(
            scenario,
            execution,
            used_default_input=True,
            image=None,
        )

        self.assertTrue(checks)
        self.assertEqual({check.status for check in checks}, {"pass"})

    def test_custom_typography_input_skips_default_literal_check(self) -> None:
        scenario = SCENARIOS["sdxl-graphic-text-generate"]
        bundle = PromptCompiler().compile(scenario.task)
        execution = DirectPromptExecutionResult(
            result=PromptResult(
                positive_prompt="minimalist cover with custom title",
                negative_prompt="",
            ),
            bundle=bundle,
            latency_ms=7,
            raw_response_sha256="1" * 64,
        )

        checks = evaluate_smoke_checks(
            scenario,
            execution,
            used_default_input=False,
            image=None,
        )
        literal = next(
            check
            for check in checks
            if check.check_id == "default_visible_text_preserved"
        )
        self.assertEqual(literal.status, "warn")
        self.assertFalse(any(check.status == "fail" for check in checks))

    def test_run_scenario_accepts_pre_resolved_profile_without_network(self) -> None:
        scenario = SCENARIOS["flux-portrait-generate"]
        profile = {
            "id": "profile-id",
            "kind": "openai_compatible",
            "name": "Fake",
            "base_url": "https://provider.example/v1",
            "model": "fake-model",
            "timeout_seconds": 30,
            "multimodal": False,
            "extra_body": {},
        }
        resolved = ResolvedSmokeProfile(
            profile=profile,
            api_key="secret",
            selected_by="test",
        )

        class FakeExecutor:
            def execute(self, **kwargs):
                bundle = PromptCompiler().compile(kwargs["task"])
                return DirectPromptExecutionResult(
                    result=PromptResult(
                        positive_prompt="studio portrait",
                        negative_prompt="",
                    ),
                    bundle=bundle,
                    latency_ms=5,
                    raw_response_sha256="2" * 64,
                )

        report = run_smoke_scenario(
            store=Mock(),
            scenario=scenario,
            selector=None,
            resolved_profile=resolved,
            executor=FakeExecutor(),
            checkpoint_profile="test-checkpoint",
        )

        self.assertFalse(report.failed)
        self.assertTrue(report.used_default_input)
        self.assertEqual(
            report.execution.bundle.task.checkpoint_profile,
            "test-checkpoint",
        )
        serialized = report.to_dict()
        self.assertNotIn("api_key", serialized["profile"])
        self.assertEqual(serialized["profile"]["model"], "fake-model")

    def test_list_command_requires_no_profile_or_network(self) -> None:
        self.assertEqual(main(["--no-color", "list"]), 0)


if __name__ == "__main__":
    unittest.main()
