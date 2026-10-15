from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.ai import agents
from app.ai.cli import (
    VISION_TEST_IMAGE,
    CLIIntegrationError,
    CommandResult,
    cli_catalog,
    detect_ide_installation,
    find_executable,
    list_cli_models,
    probe_cli,
    run_cli_test,
    run_command,
)
from app.ai.opencode_host import (
    OpenCodeInstallation,
    OpenCodeModel,
    generation_for,
    installation_kind,
    parse_verbose_models,
    parse_version,
    uses_stock_agent,
)
from app.ai.opencode_host import build_run_command as build_opencode_run_command
from app.ai.opencode_host import list_models as list_opencode_models
from app.ai.providers import AIProviderError, AIProviderStore
from app.ai.secrets import SecretStoreStatus
from app.ai.transport import (
    CONTENT_REJECTED_MESSAGE,
    TEST_PNG_BASE64,
    AIProviderRequestError,
    list_openai_compatible_models,
    run_openai_compatible_test,
)
from app.config_store import ConfigStore
from app.main import app


class MemorySecretStore:
    def __init__(self, *, available: bool = True):
        self.values: dict[str, str] = {}
        self.available = available

    def status(self) -> SecretStoreStatus:
        return SecretStoreStatus(
            available=self.available,
            backend="tests.MemorySecretStore" if self.available else None,
            message="Test credential store",
        )

    def _check(self) -> None:
        if not self.available:
            from app.ai.secrets import SecretStoreError

            raise SecretStoreError("Test credential store is unavailable")

    def get(self, profile_id: str) -> str | None:
        self._check()
        return self.values.get(profile_id)

    def set(self, profile_id: str, value: str) -> None:
        self._check()
        self.values[profile_id] = value

    def delete(self, profile_id: str) -> None:
        self._check()
        self.values.pop(profile_id, None)


def custom_payload(**overrides):
    payload = {
        "id": "myprovider",
        "name": "My provider",
        "base_url": "https://provider.example/v1",
        "api_key_source": "system",
        "api_key": "top-secret-test-key",
        "models": [{"id": "vision-model-1", "name": "Vision 1"}],
    }
    payload.update(overrides)
    return payload


def direct_profile(**overrides):
    """The execution profile of an API provider model, as app.ai.agents builds it."""
    profile = {
        "kind": "openai_compatible",
        "provider_id": "myprovider",
        "name": "Test provider · vision-model-1",
        "base_url": "https://provider.example/v1",
        "api_key_source": "system",
        "model": "vision-model-1",
        "timeout_seconds": 30,
        "multimodal": True,
        "extra_body": {"temperature": 0.2},
    }
    profile.update(overrides)
    return profile


class AIProviderStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.secrets = MemorySecretStore()
        self.store = AIProviderStore(self.config_path, secret_store=self.secrets)

    def test_system_key_is_never_persisted_or_returned(self):
        created = self.store.create(custom_payload())
        self.assertTrue(created["has_credentials"])
        self.assertNotIn("api_key", created)
        raw = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn("top-secret-test-key", raw)
        self.assertNotIn("api_key\"", raw)
        self.assertEqual(self.secrets.values["myprovider"], "top-secret-test-key")

        updated = self.store.update("myprovider", {"name": "Renamed provider", "api_key": ""})
        self.assertEqual(updated["name"], "Renamed provider")
        self.assertEqual(self.secrets.values["myprovider"], "top-secret-test-key")

        self.store.delete("myprovider")
        self.assertNotIn("myprovider", self.secrets.values)

    def test_templates_fill_the_address_and_take_their_id(self):
        created = self.store.create({"template": "openrouter", "api_key": "key"})
        self.assertEqual(created["id"], "openrouter")
        self.assertEqual(created["base_url"], "https://openrouter.ai/api/v1")
        second = self.store.create({"template": "openrouter", "api_key": "other"})
        self.assertEqual(second["id"], "openrouter-2")
        local = self.store.create({"template": "lmstudio"})
        self.assertEqual(local["api_key_source"], "none")
        self.assertTrue(local["has_credentials"])
        with self.assertRaises(AIProviderError):
            self.store.create({"template": "no-such-template", "api_key": "key"})

    def test_environment_and_no_key_providers_do_not_touch_keyring(self):
        with patch.dict(os.environ, {"MEDIASTUDIO_TEST_PROVIDER_KEY": "from-env"}):
            provider = self.store.create(custom_payload(
                api_key_source="environment", api_key_env="MEDIASTUDIO_TEST_PROVIDER_KEY", api_key="",
            ))
            self.assertTrue(provider["has_credentials"])
            profile = agents.provider_profile_base(self.store.get("myprovider"))
            self.assertEqual(self.store.resolve_api_key(profile), "from-env")
        local = self.store.create(custom_payload(
            id="lm", base_url="http://127.0.0.1:1234/v1", api_key_source="none", api_key="",
        ))
        self.assertTrue(local["has_credentials"])
        self.assertEqual(self.secrets.values, {})

    def test_invalid_ids_insecure_keys_and_duplicates_are_rejected(self):
        with self.assertRaises(AIProviderError):
            self.store.create(custom_payload(base_url="http://provider.example/v1"))
        with self.assertRaises(AIProviderError):
            self.store.create(custom_payload(id="Bad ID"))
        with self.assertRaises(AIProviderError):
            self.store.create(custom_payload(id="opencode"))
        with self.assertRaises(AIProviderError):
            self.store.create(custom_payload(models=[{"id": "a"}, {"id": "a"}]))
        self.store.create(custom_payload())
        with self.assertRaises(AIProviderError) as exists:
            self.store.create(custom_payload())
        self.assertEqual(exists.exception.code, "provider_exists")

    def test_disconnect_forgets_visibility_and_the_default_model(self):
        self.store.create(custom_payload())
        self.store.set_visibility({"api:myprovider|vision-model-1": False, "opencode|opencode/exo-free": False})
        self.store.set_default_model({"host": "api:myprovider", "model": "vision-model-1"})
        self.store.delete("myprovider")
        self.assertEqual(self.store.visibility(), {"opencode|opencode/exo-free": False})
        self.assertIsNone(self.store.default_model())

    def test_disconnected_tools_lose_the_default_model(self):
        self.store.set_default_model({"host": "codex", "model": "gpt-6", "variant": "high"})
        self.assertEqual(self.store.default_model(), {"host": "codex", "model": "gpt-6", "variant": "high"})
        self.store.set_tool_connected("codex", False)
        self.assertEqual(self.store.disconnected_tools(), {"codex"})
        self.assertIsNone(self.store.default_model())
        self.store.set_tool_connected("codex", True)
        self.assertEqual(self.store.disconnected_tools(), set())
        with self.assertRaises(AIProviderError):
            self.store.set_tool_connected("civitai", False)

    def test_visibility_overrides_validate_keys_and_reset_with_null(self):
        self.store.set_visibility({"claude|sonnet": False})
        self.assertEqual(self.store.visibility(), {"claude|sonnet": False})
        self.store.set_visibility({"claude|sonnet": None})
        self.assertEqual(self.store.visibility(), {})
        for key in ("sonnet", "unknown|model", "api:BAD|x"):
            with self.assertRaises(AIProviderError):
                self.store.set_visibility({key: True})

    def test_legacy_profiles_migrate_to_providers_and_a_default_model(self):
        legacy_id = "6b1f3f62-0000-4000-8000-000000000001"
        self.secrets.values[legacy_id] = "legacy-key"
        self.config_path.write_text(json.dumps({
            "version": 3,
            "sources": [],
            "ai": {
                "profiles": [
                    {"id": legacy_id, "kind": "openai_compatible", "name": "OpenRouter (Vision)",
                     "base_url": "https://openrouter.ai/api/v1/", "api_key_source": "system",
                     "model": "openai/gpt-5", "timeout_seconds": 60, "multimodal": True},
                    {"id": "second", "kind": "openai_compatible", "name": "OpenRouter (Text)",
                     "base_url": "https://openrouter.ai/api/v1", "api_key_source": "system",
                     "model": "anthropic/claude-sonnet-5", "timeout_seconds": 60},
                    {"id": "oc", "kind": "cli", "cli_type": "opencode", "name": "OpenCode (Vision)",
                     "model": "opencode/exo-free#high", "timeout_seconds": 60, "multimodal": True},
                ],
                "defaults": {"text_profile_id": "second", "multimodal_profile_id": "oc", "rating_auto_enabled": True},
            },
        }), encoding="utf-8")
        listing = self.store.list()
        self.assertEqual([item["id"] for item in listing["providers"]], ["openrouter"])
        provider = listing["providers"][0]
        self.assertEqual(provider["template"], "openrouter")
        self.assertEqual([item["id"] for item in provider["models"]], ["openai/gpt-5", "anthropic/claude-sonnet-5"])
        self.assertEqual(self.secrets.values, {"openrouter": "legacy-key"})
        self.assertEqual(listing["default_model"], {"host": "opencode", "model": "opencode/exo-free", "variant": "high"})
        self.assertTrue(self.store.rating_auto_enabled())
        ai = ConfigStore(self.config_path).load()["ai"]
        self.assertNotIn("profiles", ai)
        self.assertNotIn("text_profile_id", ai["defaults"])
        # Migration runs once: a disconnected provider does not come back.
        self.store.delete("openrouter")
        self.assertEqual(self.store.list()["providers"], [])


class AIRoutesTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.old_config = dict(app.config)
        self.addCleanup(self._restore_config)
        self.secrets = MemorySecretStore()
        app.config.update(
            TESTING=True,
            CONFIG_FILE=str(Path(self.temp_dir.name) / "config.json"),
            AI_SECRET_STORE=self.secrets,
        )
        self.client = app.test_client()
        agents._provider_models_cache.clear()
        self.addCleanup(agents._provider_models_cache.clear)

    def _restore_config(self):
        app.config.clear()
        app.config.update(self.old_config)

    def test_settings_page_has_providers_and_models_panes(self):
        page = self.client.get("/settings/ai")
        self.assertEqual(page.status_code, 200)
        for marker in (b">Integrations</h1>", b'data-pane="providers"', b'data-pane="models"', b'data-pane="sources"',
                       b'id="connected-providers"', b'id="popular-providers"', b'id="models-groups"',
                       b'id="default-model"', b'id="connect-dialog"', b'id="custom-dialog"', b"icons/providers.svg",
                       b"components/toast.css"):
            self.assertIn(marker, page.data)
        self.assertNotIn(b"profile", page.data.lower().replace(b"profile-dialog", b""))

    def test_provider_api_masks_secrets_and_lists_tools_and_templates(self):
        with patch("app.ai.routes.find_executable", return_value=None), \
                patch("app.ai.routes.opencode_host.resolve_executable", return_value="C:/OpenCode/opencode-cli.exe"):
            created = self.client.post("/api/ai/providers", json={"template": "openai", "api_key": "sk-test-secret"})
            self.assertEqual(created.status_code, 201)
            self.assertNotIn("api_key", created.get_json()["provider"])
            listed = self.client.get("/api/ai/providers").get_json()
        self.assertTrue(listed["secret_store"]["available"])
        self.assertEqual([item["id"] for item in listed["providers"]], ["openai"])
        tools = {item["type"]: item for item in listed["local_tools"]}
        self.assertTrue(tools["opencode"]["connected"])
        self.assertFalse(tools["codex"]["installed"])
        self.assertEqual(listed["templates"][0]["id"], "openai")
        self.assertNotIn("sk-test-secret", json.dumps(listed))

        duplicate = self.client.post("/api/ai/providers", json=custom_payload(id="openai"))
        self.assertEqual(duplicate.status_code, 409)
        removed = self.client.delete("/api/ai/providers/openai")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.secrets.values, {})
        self.assertEqual(self.client.delete("/api/ai/providers/openai").status_code, 404)

    def test_check_lists_models_with_the_stored_key(self):
        self.client.post("/api/ai/providers", json=custom_payload())
        with patch("app.ai.agents.list_openai_compatible_models", return_value=["a", "b"]) as listing:
            response = self.client.post("/api/ai/providers/myprovider/check")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "models": 3})
        listing.assert_called_once_with("https://provider.example/v1", api_key="top-secret-test-key")
        with patch("app.ai.agents.list_openai_compatible_models",
                   side_effect=AIProviderRequestError("Invalid API key", code="authentication")):
            failed = self.client.post("/api/ai/providers/myprovider/check")
        self.assertEqual(failed.status_code, 422)
        self.assertEqual(failed.get_json()["error"], "Invalid API key")

    def test_models_catalog_groups_hosts_and_applies_visibility(self):
        self.client.post("/api/ai/providers", json=custom_payload())
        installation = OpenCodeInstallation("desktop", "C:/OpenCode/opencode-cli.exe", "2.0.28", 2)
        cards = [
            OpenCodeModel("opencode/exo-free", "opencode", "Exo Free", vision=True, free=True),
            OpenCodeModel("google/gemini-new", "google", "Gemini New", vision=True, free=True, family="gemini",
                          released=int(time.time() * 1000)),
            OpenCodeModel("google/gemini-old", "google", "Gemini Old", vision=True, free=False, family="gemini",
                          released=int((time.time() - 400 * 86400) * 1000)),
            OpenCodeModel("google/lyria", "google", "Lyria", vision=True, free=True, media_outputs=("audio",)),
        ]
        with patch("app.ai.opencode_host.find_installations", return_value=[installation]), \
                patch("app.ai.agents.opencode_models", return_value=cards), \
                patch("app.ai.agents.native_cli_models", return_value=(None, [])), \
                patch("app.ai.agents.antigravity_models", return_value=(None, [])), \
                patch("app.ai.agents.list_openai_compatible_models", return_value=["vision-model-1", "other:free"]):
            catalog = self.client.get("/api/ai/models").get_json()
            groups = {group["id"]: group for group in catalog["groups"]}
            self.assertEqual(list(groups), ["opencode:opencode", "opencode:google", "api:myprovider"])
            google = {model["id"]: model for model in groups["opencode:google"]["models"]}
            self.assertTrue(google["google/gemini-new"]["visible"])
            self.assertFalse(google["google/gemini-old"]["visible"])
            # Media models stay hidden, and a zero price outside OpenCode Zen is not "free".
            self.assertFalse(google["google/lyria"]["visible"])
            self.assertFalse(google["google/gemini-new"]["free"])
            self.assertTrue(groups["opencode:opencode"]["models"][0]["free"])
            api = {model["id"]: model for model in groups["api:myprovider"]["models"]}
            self.assertEqual(api["vision-model-1"]["name"], "Vision 1")
            self.assertTrue(api["other:free"]["free"])

            changed = self.client.patch("/api/ai/models/visibility", json={"changes": {
                "opencode|google/gemini-old": True, "opencode|opencode/exo-free": False,
            }})
            self.assertEqual(changed.status_code, 200)
            visible = self.client.get("/api/ai/models?visible=1").get_json()
        shown = {group["id"]: [model["id"] for model in group["models"]] for group in visible["groups"]}
        self.assertNotIn("opencode:opencode", shown)
        self.assertEqual(shown["opencode:google"], ["google/gemini-new", "google/gemini-old"])
        bad = self.client.patch("/api/ai/models/visibility", json={"changes": {"nope": True}})
        self.assertEqual(bad.status_code, 400)

    def test_default_model_is_validated_against_the_host(self):
        with patch("app.ai.agents.native_cli_models", return_value=("codex", [
            {"id": "gpt-6", "name": "GPT-6", "vision": True, "variants": ["high"]},
        ])):
            saved = self.client.put("/api/ai/default-model", json={"model": {"host": "codex", "model": "gpt-6"}})
            missing = self.client.put("/api/ai/default-model", json={"model": {"host": "codex", "model": "gpt-0"}})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["default_model"], {"host": "codex", "model": "gpt-6", "variant": None})
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.get_json()["code"], "model_unavailable")
        cleared = self.client.put("/api/ai/default-model", json={"model": None})
        self.assertIsNone(cleared.get_json()["default_model"])

    def test_local_tools_can_be_disconnected(self):
        with patch("app.ai.routes.find_executable", return_value="C:/tools/codex.cmd"), \
                patch("app.ai.routes.opencode_host.resolve_executable", return_value=None):
            response = self.client.patch("/api/ai/local-tools/codex", json={"connected": False})
        tools = {item["type"]: item for item in response.get_json()["local_tools"]}
        self.assertTrue(tools["codex"]["installed"])
        self.assertFalse(tools["codex"]["connected"])
        self.assertEqual(self.client.patch("/api/ai/local-tools/codex", json={}).status_code, 400)

    def test_operations_run_the_picked_model_or_explain_the_missing_one(self):
        self.client.post("/api/ai/providers", json=custom_payload())
        with patch("app.ai.agents.implicit_opencode_profile", return_value=None):
            missing = self.client.post("/api/ai/enhance", json={"source": {"positive_prompt": "a cat"}})
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.get_json()["code"], "no_ai_model")
        with patch("app.ai.agents.list_openai_compatible_models", return_value=["text-model"]):
            unknown = self.client.post("/api/ai/enhance", json={
                "agent": {"host": "api:myprovider", "model": "not-listed"}, "source": {"positive_prompt": "a cat"},
            })
        self.assertEqual(unknown.status_code, 409)
        self.assertEqual(unknown.get_json()["code"], "model_unavailable")

    def test_cli_discovery_response_does_not_require_credentials(self):
        discovered = [{
            "type": "opencode",
            "label": "OpenCode",
            "installed": True,
            "executable": "C:/tools/opencode.cmd",
            "authentication": {"status": "available", "message": "Configured"},
        }]
        with patch("app.ai.routes.discover_cli_integrations", return_value=discovered):
            response = self.client.get("/api/ai/cli-integrations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["integrations"], discovered)

    def test_cli_catalog_route_skips_probing(self):
        response = self.client.get("/api/ai/cli-integrations?probe=0")
        self.assertEqual(response.status_code, 200)
        integrations = response.get_json()["integrations"]
        self.assertEqual(
            {item["type"] for item in integrations},
            {"opencode", "claude", "antigravity", "codex", "civitai"},
        )
        for item in integrations:
            self.assertNotIn("installed", item)
            self.assertNotIn("authentication", item)
        antigravity = next(
            item for item in integrations if item["type"] == "antigravity"
        )
        self.assertIn("install", antigravity)

    def test_single_cli_probe_route(self):
        probed = {
            "type": "claude",
            "label": "Claude Code",
            "installed": True,
            "executable": "C:/tools/claude.cmd",
            "authentication": {"status": "error", "message": "No access"},
        }
        with patch("app.ai.routes.probe_cli", return_value=probed) as probe:
            response = self.client.get("/api/ai/cli-integrations/claude")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["integration"], probed)
        probe.assert_called_once_with("claude")

    def test_single_cli_probe_route_rejects_unknown_type(self):
        response = self.client.get("/api/ai/cli-integrations/not-a-cli")
        self.assertEqual(response.status_code, 404)


class ProviderModelListTest(unittest.TestCase):
    def test_models_endpoint_reads_openai_and_gemini_shapes(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        payloads = [
            {"object": "list", "data": [{"id": "gpt-6"}, {"id": "gpt-6"}, {"id": ""}]},
            {"models": [{"name": "models/gemini-4"}]},
        ]
        requests = []

        def fake_open(request, *, timeout):
            requests.append(request)
            return Response(json.dumps(payloads[len(requests) - 1]).encode())

        with patch("app.ai.transport._open_url", side_effect=fake_open):
            self.assertEqual(list_openai_compatible_models("https://api.example/v1/", api_key="k"), ["gpt-6"])
            self.assertEqual(list_openai_compatible_models("https://api.example/v1", api_key=None), ["gemini-4"])
        self.assertEqual(requests[0].full_url, "https://api.example/v1/models")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer k")
        self.assertIsNone(requests[1].get_header("Authorization"))


class CLIDetectionTest(unittest.TestCase):
    def test_catalog_is_static_and_carries_install_metadata(self):
        catalog = cli_catalog()
        self.assertEqual(
            {item["type"] for item in catalog},
            {"opencode", "claude", "antigravity", "codex", "civitai"},
        )
        for item in catalog:
            self.assertNotIn("installed", item)
            self.assertNotIn("authentication", item)
        antigravity = next(
            item for item in catalog if item["type"] == "antigravity"
        )
        install = antigravity["install"]
        self.assertIn("winget", install["windows"]["command"])
        self.assertIn("brew", install["macos"]["command"])
        self.assertTrue(install["docs_url"].startswith("https://"))

    def test_command_timeout_terminates_the_process(self):
        started = time.monotonic()
        with self.assertRaises(CLIIntegrationError) as caught:
            run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=1,
            )
        self.assertEqual(caught.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 8)

    def test_command_streams_stdout_and_stderr_lines(self):
        out, err = [], []
        result = run_command(
            [sys.executable, "-c", "import sys; print('to out'); print('to err', file=sys.stderr)"],
            timeout=10,
            on_output_chunk=out.append,
            on_error_chunk=err.append,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual([line.strip() for line in out], ["to out"])
        self.assertEqual([line.strip() for line in err], ["to err"])

    def test_opencode_models_can_be_filtered_by_provider(self):
        payload = {"data": [
            {"id": "deepseek-chat", "providerID": "deepseek", "name": "DeepSeek Chat",
             "capabilities": {"input": ["text"]}},
            {"id": "deepseek-reasoner", "providerID": "deepseek", "name": "DeepSeek Reasoner",
             "capabilities": {"input": ["text"]}},
            {"id": "gpt-4o", "providerID": "openai", "name": "GPT-4o",
             "capabilities": {"input": ["text"]}},
        ]}
        command_result = CommandResult(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
            elapsed_ms=5,
        )
        with (
            patch("app.ai.opencode_host.resolve_executable", return_value="C:/OpenCode/opencode-cli.exe"),
            patch("app.ai.opencode_host.read_version", return_value="2.0.27"),
            patch("app.ai.cli.run_command", return_value=command_result) as command,
        ):
            result = list_cli_models("opencode", provider="deepseek")
        args = command.call_args_list[0].args[0]
        self.assertEqual(args[0], "C:/OpenCode/opencode-cli.exe")
        self.assertEqual(args[1:], ["api", "--standalone", "GET", "/api/model"])
        self.assertIn("XDG_CONFIG_HOME", command.call_args_list[0].kwargs["env_overrides"])
        self.assertEqual(result["providers"], ["deepseek"])
        self.assertEqual(result["requested_provider"], "deepseek")
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(result["generation"], 2)

    def test_opencode_verbose_models_carry_vision_and_free_flags(self):
        output = (
            "opencode/big-pickle\n"
            + json.dumps({
                "name": "Big Pickle",
                "cost": {"input": 0, "output": 0},
                "capabilities": {"input": {"text": True, "image": False}},
            }, indent=2)
            + "\nopencode/claude-opus-5\n"
            + json.dumps({
                "name": "Claude Opus 5",
                "cost": {"input": 5, "output": 25},
                "capabilities": {"input": {"text": True, "image": True}},
            }, indent=2)
            + "\n"
        )
        models = {item.id: item for item in parse_verbose_models(output)}
        self.assertEqual(set(models), {"opencode/big-pickle", "opencode/claude-opus-5"})
        self.assertEqual(models["opencode/claude-opus-5"].variants, ())
        self.assertEqual((models["opencode/big-pickle"].vision, models["opencode/big-pickle"].free), (False, True))
        self.assertEqual((models["opencode/claude-opus-5"].vision, models["opencode/claude-opus-5"].free), (True, False))

    def test_opencode_desktop_models_come_from_a_private_isolated_server(self):
        payload = {"data": [
            {"id": "muse-free", "providerID": "opencode", "name": "Muse Free",
             "capabilities": {"input": ["text", "image"]}, "cost": [{"input": 0, "output": 0}],
             "variants": [{"id": "low", "settings": {}}, {"id": "high", "settings": {}}, {"id": "bad level!"}]},
            {"id": "pickle", "providerID": "opencode", "name": "Pickle",
             "capabilities": {"input": ["text"]}, "cost": [{"input": 0, "output": 0}]},
            {"id": "pro", "providerID": "google", "name": "Pro", "enabled": False,
             "capabilities": {"input": ["text", "image"]}, "cost": [{"input": 1, "output": 2}]},
        ]}
        result = CommandResult(returncode=0, stdout=json.dumps(payload), stderr="", elapsed_ms=3)
        with patch("app.ai.cli.run_command", return_value=result) as command:
            models = list_opencode_models("C:/OpenCode/opencode-cli.exe", generation=2, provider="opencode")
        args = command.call_args.args[0]
        self.assertEqual(args[1:], ["api", "--standalone", "GET", "/api/model"])
        self.assertIn("XDG_CONFIG_HOME", command.call_args.kwargs["env_overrides"])
        self.assertEqual(
            [(item.id, item.vision, item.free) for item in models],
            [("opencode/muse-free", True, True), ("opencode/pickle", False, True)],
        )
        # Reasoning levels come from the card; malformed names are dropped.
        self.assertEqual([item.variants for item in models], [("low", "high"), ()])

    def test_opencode_installations_report_kind_and_generation(self):
        self.assertEqual(parse_version("opencode v2.0.24"), "2.0.24")
        self.assertEqual(parse_version("1.18.34"), "1.18.34")
        self.assertEqual(installation_kind("C:/x/resources/opencode-cli.exe"), "desktop")
        self.assertEqual(installation_kind("C:/npm/opencode.cmd"), "cli")
        self.assertEqual(generation_for("C:/x/opencode-cli.exe", "2.0.24"), 2)
        self.assertEqual(generation_for("C:/npm/opencode.cmd", "1.18.34"), 1)
        self.assertEqual(generation_for("C:/x/opencode-cli.exe", ""), 2)

    def test_opencode_run_commands_follow_each_dialect(self):
        files = [Path("C:/w/mediastudio-task.md"), Path("C:/w/input.jpg")]
        cmd = build_opencode_run_command(
            "opencode-cli.exe", generation=2, model="opencode/m", agent="plan",
            title="t", message="go", files=files,
        )
        self.assertEqual(cmd[:3], ["opencode-cli.exe", "run", "--standalone"])
        self.assertNotIn("--pure", cmd)
        self.assertEqual(cmd[-1], "go")
        self.assertEqual(cmd[cmd.index("--file") + 1], str(files[0]))
        self.assertTrue(uses_stock_agent("opencode/muse-spark-1.3-contributor-free"))
        self.assertFalse(uses_stock_agent("deepseek/deepseek-chat"))

    def test_opencode_model_provider_rejects_command_arguments(self):
        with patch("app.ai.cli.find_executable", return_value="C:/tools/opencode.cmd"):
            with self.assertRaises(CLIIntegrationError):
                list_cli_models("opencode", provider="deepseek --refresh")

    def test_provider_filter_is_rejected_for_other_clis(self):
        with patch("app.ai.cli.find_executable", return_value="C:/tools/claude.cmd"):
            with self.assertRaises(CLIIntegrationError):
                list_cli_models("claude", provider="anthropic")

    @unittest.skipUnless(os.name == "nt", "Windows-only fallback layout")
    def test_find_executable_falls_back_to_npm_shim_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            shim_dir = Path(temp_dir) / "npm"
            shim_dir.mkdir()
            shim = shim_dir / "opencode.cmd"
            shim.write_text("@echo off\n", encoding="utf-8")
            with (
                patch("app.ai.cli.shutil.which", return_value=None),
                patch.dict(os.environ, {"APPDATA": temp_dir}),
            ):
                found = find_executable("opencode")
        self.assertIsNotNone(found)
        self.assertTrue(found.endswith("opencode.cmd"))

    def test_ide_detection_via_path_shim(self):
        with patch(
            "app.ai.cli.shutil.which",
            side_effect=lambda name: (
                "C:/Tools/antigravity-ide.cmd" if name == "antigravity-ide" else None
            ),
        ):
            ide = detect_ide_installation("antigravity")
        self.assertTrue(ide["installed"])
        self.assertIn("antigravity-ide", ide["location"])

    def test_ide_detection_via_install_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ide_exe = Path(temp_dir) / "Antigravity IDE.exe"
            ide_exe.write_bytes(b"")
            with (
                patch("app.ai.cli.shutil.which", return_value=None),
                patch(
                    "app.ai.cli._ide_candidate_paths",
                    return_value=[ide_exe],
                ),
            ):
                ide = detect_ide_installation("antigravity")
        self.assertTrue(ide["installed"])
        self.assertIn("Antigravity IDE", ide["location"])

    def test_ide_detection_absent_for_other_integrations(self):
        ide = detect_ide_installation("opencode")
        self.assertFalse(ide["installed"])
        self.assertIsNone(ide["location"])

    def test_antigravity_probe_reports_ide_and_install_when_cli_missing(self):
        with (
            patch("app.ai.cli.find_executable", return_value=None),
            patch(
                "app.ai.cli.detect_ide_installation",
                return_value={
                    "installed": True,
                    "location": "C:/Programs/Antigravity IDE",
                    "label": "Antigravity IDE",
                },
            ),
        ):
            result = probe_cli("antigravity")
        self.assertFalse(result["installed"])
        self.assertTrue(result["ide"]["installed"])
        self.assertIn("Antigravity IDE", result["authentication"]["message"])
        self.assertIn("winget", result["install"]["windows"]["command"])

    def test_probe_without_ide_has_no_ide_payload(self):
        with (
            patch("app.ai.cli.find_executable", return_value=None),
            patch(
                "app.ai.cli.detect_ide_installation",
                return_value={"installed": False, "location": None, "label": "Antigravity IDE"},
            ),
        ):
            result = probe_cli("antigravity")
        self.assertFalse(result["installed"])
        self.assertNotIn("ide", result)
        self.assertIn("install", result)


    def test_multimodal_probe_is_a_valid_one_pixel_png(self):
        image = Image.open(io.BytesIO(base64.b64decode(TEST_PNG_BASE64)))
        image.load()
        self.assertEqual(image.size, (1, 1))

    def test_openai_compatible_test_parses_standard_response(self):
        profile = direct_profile()
        profile.update({"id": "test-id", "extra_body": {}})

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read(_maximum):
                return json.dumps({
                    "choices": [{"message": {"content": "MEDIASTUDIO_OK"}}]
                }).encode()

        with patch("app.ai.transport._open_url", return_value=FakeResponse()):
            result = run_openai_compatible_test(
                profile, api_key="test-key", multimodal=True
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["response_preview"], "MEDIASTUDIO_OK")

    def test_content_policy_errors_have_a_stable_user_message(self):
        profile = direct_profile()
        profile.update({"id": "test-id", "extra_body": {}})
        body = json.dumps({
            "error": {
                "code": "content_policy_violation",
                "message": "Image blocked by safety policy",
            }
        }).encode()
        error = urllib.error.HTTPError(
            "https://provider.example/v1/chat/completions",
            400,
            "Bad Request",
            {},
            io.BytesIO(body),
        )
        with patch("app.ai.transport._open_url", side_effect=error):
            with self.assertRaises(AIProviderRequestError) as caught:
                run_openai_compatible_test(
                    profile, api_key="test-key", multimodal=True
                )
        self.assertEqual(caught.exception.code, "content_rejected")
        self.assertEqual(str(caught.exception), CONTENT_REJECTED_MESSAGE)

    def test_opencode_json_events_are_parsed_without_reading_auth_files(self):
        profile = {
            "cli_type": "opencode",
            "executable": "C:/tools/opencode.cmd",
            "model": "provider/model",
            "timeout_seconds": 20,
        }
        output = json.dumps({"part": {"text": "MEDIASTUDIO_OK"}})
        with (
            patch("app.ai.cli.find_executable", return_value=profile["executable"]),
            patch("app.ai.cli.run_command", return_value=CommandResult(
                returncode=0,
                stdout=output,
                stderr="",
                elapsed_ms=15,
            )),
        ):
            result = run_cli_test(profile)
        self.assertEqual(result["response_preview"], "MEDIASTUDIO_OK")

    def test_opencode_multimodal_test_attaches_bundled_image_as_one_argument(self):
        profile = {
            "cli_type": "opencode",
            "executable": "C:/OpenCode/opencode-cli.exe",
            "model": "provider/model",
            "timeout_seconds": 20,
        }
        output = json.dumps({"part": {"text": "MEDIASTUDIO_OK"}})
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = list(args)
            captured["kwargs"] = kwargs
            file_idx = args.index("--file")
            image = args[file_idx + 1]
            captured["image_copied"] = Path(image).is_file()
            return CommandResult(returncode=0, stdout=output, stderr="", elapsed_ms=15)

        with (
            patch("app.ai.opencode_host.resolve_executable", return_value=profile["executable"]),
            patch("app.ai.opencode_host.read_version", return_value="2.0.27"),
            patch("app.ai.cli.run_command", side_effect=fake_run),
        ):
            result = run_cli_test(profile, multimodal=True)

        args = captured["args"]
        self.assertIn("--standalone", args)
        self.assertIn("--file", args)
        file_idx = args.index("--file")
        self.assertTrue(args[file_idx + 1].endswith("input.jpg"))
        self.assertTrue(captured["image_copied"])
        self.assertEqual(args[args.index("--agent") + 1], "plan")
        self.assertIn("XDG_CONFIG_HOME", captured["kwargs"]["env_overrides"])
        self.assertTrue(VISION_TEST_IMAGE.is_file())
        with Image.open(VISION_TEST_IMAGE) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertGreater(image.width, 1)
            self.assertGreater(image.height, 1)
        self.assertTrue(any("attached garden image" in arg for arg in args))
        self.assertEqual(result["response_preview"], "MEDIASTUDIO_OK")

    def test_opencode_free_tier_refusal_is_reported_plainly(self):
        profile = {
            "cli_type": "opencode",
            "executable": "C:/tools/opencode.cmd",
            "model": "opencode/muse-free",
            "timeout_seconds": 20,
        }
        refusal = json.dumps({"type": "error", "error": {"name": "APIError", "data": {
            "message": "OpenCode's free tier can only be used from within OpenCode"}}})
        with (
            patch("app.ai.opencode_host.resolve_executable", return_value=profile["executable"]),
            patch("app.ai.opencode_host.read_version", return_value="1.18.34"),
            patch("app.ai.cli.run_command", return_value=CommandResult(1, refusal, "", 5)),
            self.assertRaises(CLIIntegrationError) as caught,
        ):
            run_cli_test(profile)
        self.assertIn("stock OpenCode agent", str(caught.exception))


class AITransportSecurityTest(unittest.TestCase):
    def test_redact_with_empty_or_short_keys(self):
        from app.ai.transport import _redact
        # Test empty key
        self.assertEqual(_redact("my api_key is secret", ""), "my api_key is secret")
        self.assertEqual(_redact("my api_key is secret", "   "), "my api_key is secret")
        # Test short key (< 4 characters)
        self.assertEqual(_redact("abc", "a"), "abc")

    def test_redact_url_encoded_key(self):
        from app.ai.transport import _redact
        api_key = "secret/key+123="
        text = "https://provider.example/v1?key=secret%2Fkey%2B123%3D"
        redacted = _redact(text, api_key)
        self.assertNotIn("secret%2Fkey%2B123%3D", redacted)
        self.assertIn("[redacted]", redacted)

    def test_extract_error_truncation(self):
        from app.ai.transport import _extract_error
        api_key = "top-secret-key-123"
        # 2000 characters of raw HTML error containing the secret key inside the first 500 chars
        long_body = ("<p>Gateway Error</p>" * 5) + f" secret: {api_key} " + ("<p>Gateway Error</p>" * 100)
        message, code = _extract_error(long_body.encode("utf-8"), api_key)

        self.assertTrue(message.endswith(" [truncated]"))
        self.assertLessEqual(len(message), 1015) # 1000 + length of " [truncated]"
        self.assertNotIn(api_key, message)
        self.assertIn("[redacted]", message)

    def test_response_text_redacts_api_key(self):
        from app.ai.transport import _response_text
        api_key = "super-secret-key"
        invalid_payload = {
            "choices": [],  # incompatible format!
            "echoed_key": api_key,
        }
        with self.assertRaises(AIProviderRequestError) as caught:
            _response_text(invalid_payload, api_key)

        self.assertNotIn(api_key, caught.exception.technical_error)
        self.assertIn("[redacted]", caught.exception.technical_error)

    def test_run_openai_compatible_chat_truncation_on_invalid_json(self):
        from app.ai.transport import run_openai_compatible_chat
        api_key = "my-secret-key"
        profile = direct_profile()
        profile["timeout_seconds"] = 10

        # Simulate provider returning invalid JSON (massive HTML with the secret key inside the first 500 chars)
        massive_invalid_response = f"secret: {api_key} " + ("<html><body>Gateway Timeout</body></html>" * 50)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read(_maximum):
                return massive_invalid_response.encode("utf-8")

        with patch("app.ai.transport._open_url", return_value=FakeResponse()):
            with self.assertRaises(AIProviderRequestError) as caught:
                run_openai_compatible_chat(profile, api_key=api_key, messages=[{"role": "user", "content": "hello"}])

        tech_err = caught.exception.technical_error
        self.assertIsNotNone(tech_err)
        self.assertTrue(tech_err.endswith(" [truncated]"))
        self.assertLessEqual(len(tech_err), 1015)
        self.assertNotIn(api_key, tech_err)
        self.assertIn("[redacted]", tech_err)


if __name__ == "__main__":
    unittest.main()
