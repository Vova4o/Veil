import sys
from contextlib import contextmanager

import pytest

from app.ai import catalog_cache, model_catalog
from app.ai.cli import CLIIntegrationError
from app.ai.execution.claude_code import build_claude_command
from app.ai.execution.codex import build_codex_command


def test_native_modality_metadata_wins_and_missing_metadata_stays_unknown():
    models = model_catalog.parse_native_models("codex", [
        {"model": "text-model", "inputModalities": ["text"], "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
        {"model": "image-model", "inputModalities": ["text", "image"]},
        {"model": "future-model"},
        {"model": "gpt-oss-proxy", "inputModalities": ["image"]},
        {"model": "hidden", "hidden": True},
    ])
    assert [item["vision"] for item in models] == [False, True, None, True]
    assert [item["vision_source"] for item in models] == ["model", "model", "unknown", "model"]
    assert models[0]["variants"] == ["high"]


def test_claude_catalog_uses_its_aliases_and_marks_family_inference():
    models = model_catalog.parse_native_models("claude", [
        {"value": "default", "displayName": "Default", "description": "Opus 4.6 · Most capable"},
        {"value": "sonnet", "supportedEffortLevels": ["low", "high", "high", "bad level"]},
        {"value": "provider/gpt-oss-120b"}, {"value": "custom-model"},
    ])
    assert [item["id"] for item in models] == ["default", "sonnet", "provider/gpt-oss-120b", "custom-model"]
    assert [item["vision"] for item in models] == [True, True, False, None]
    assert models[0]["vision_source"] == "family" and models[1]["variants"] == ["low", "high"]


class FakeSession:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []

    def send(self, value):
        self.sent.append(value)

    def receive(self, matches):
        reply = next(self.replies)
        assert matches(reply)
        return reply


def test_codex_catalog_handshake_pagination_and_no_inference(monkeypatch):
    session = FakeSession([
        {"id": 1, "result": {}},
        {"id": 2, "result": {"data": [{"model": "first", "inputModalities": ["text"]}], "nextCursor": "page2"}},
        {"id": 3, "result": {"data": [{"model": "second", "inputModalities": ["image"]}], "nextCursor": None}},
    ])
    @contextmanager
    def factory(args):
        assert args == ["codex", "app-server"]
        yield session
    monkeypatch.setattr(model_catalog, "_catalog_session", factory)
    assert [item["id"] for item in model_catalog._codex_models("codex")] == ["first", "second"]
    assert [item["method"] for item in session.sent] == ["initialize", "initialized", "model/list", "model/list"]
    assert session.sent[-1]["params"]["cursor"] == "page2"


def test_claude_catalog_initializes_without_a_prompt_hooks_or_mcp(monkeypatch):
    session = FakeSession([{"type": "control_response", "response": {
        "request_id": "models", "subtype": "success", "response": {"models": [{"value": "sonnet"}]},
    }}])
    @contextmanager
    def factory(args):
        assert args[args.index("--settings") + 1] == '{"disableAllHooks":true}'
        assert args[args.index("--mcp-config") + 1] == '{"mcpServers":{}}'
        assert "--no-session-persistence" in args
        yield session
    monkeypatch.setattr(model_catalog, "_catalog_session", factory)
    assert model_catalog._claude_models("claude")[0]["id"] == "sonnet"
    assert session.sent == [{"type": "control_request", "request_id": "models", "request": {"subtype": "initialize"}}]


def test_catalog_cache_refresh_and_missing_installation(monkeypatch):
    model_catalog._cache.clear()
    calls = []
    monkeypatch.setattr(model_catalog, "find_executable", lambda host: "codex")
    monkeypatch.setattr(model_catalog, "_codex_models", lambda exe: calls.append(exe) or [{"id": "m"}])
    model_catalog.native_cli_models("codex")
    model_catalog.native_cli_models("codex")
    assert len(calls) == 1
    model_catalog.native_cli_models("codex", refresh=True)
    assert len(calls) == 2
    monkeypatch.setattr(model_catalog, "find_executable", lambda host: None)
    assert model_catalog.native_cli_models("codex") == (None, [])
    model_catalog._cache.clear()


def test_catalog_survives_process_cache_reset(tmp_path, monkeypatch):
    calls = []
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"version-one")
    monkeypatch.setattr(model_catalog, "find_executable", lambda host: str(executable))
    cards = [{"id": "m", "vision": None, "variants": ["low", "high"]}]
    monkeypatch.setattr(model_catalog, "_codex_models", lambda exe: calls.append(exe) or cards)
    model_catalog.native_cli_models("codex")
    model_catalog._cache.clear()  # a fresh server process, with the same profile
    assert model_catalog.native_cli_models("codex")[1] == cards
    assert len(calls) == 1


def test_catalog_invalidates_after_cli_replacement(tmp_path, monkeypatch):
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"one")
    monkeypatch.setattr(model_catalog, "find_executable", lambda host: str(executable))
    cards = [[{"id": "sonnet", "vision": True}], [{"id": "opus", "vision": True}]]
    monkeypatch.setattr(model_catalog, "_claude_models", lambda exe: cards.pop(0))
    assert model_catalog.native_cli_models("claude")[1][0]["id"] == "sonnet"
    executable.write_bytes(b"version-two")
    assert model_catalog.native_cli_models("claude")[1][0]["id"] == "opus"


def test_incomplete_opencode_cache_is_ignored(monkeypatch):
    from app.ai import agents, opencode_host

    catalog_cache.save("opencode", "opencode.exe", [{"id": "broken"}])
    catalog_cache.memory.clear()
    card = opencode_host.OpenCodeModel("opencode/test", "opencode", "Test", True, True, ())
    monkeypatch.setattr(opencode_host, "list_models", lambda exe: [card])
    assert agents.opencode_models("opencode.exe") == [card]


def test_expired_catalog_and_failed_refresh_do_not_destroy_last_good_file(monkeypatch):
    calls = []
    clock = [1000.0]
    monkeypatch.setattr(catalog_cache.time, "time", lambda: clock[0])
    monkeypatch.setattr(model_catalog, "find_executable", lambda host: "codex")
    monkeypatch.setattr(model_catalog, "_codex_models", lambda exe: calls.append(exe) or [{"id": "first"}])
    model_catalog.native_cli_models("codex")
    original = next(catalog_cache.cache_directory().glob("*.json")).read_text()
    def offline(executable):
        raise CLIIntegrationError("offline")
    monkeypatch.setattr(model_catalog, "_codex_models", offline)
    with pytest.raises(CLIIntegrationError):
        model_catalog.native_cli_models("codex", refresh=True)
    assert next(catalog_cache.cache_directory().glob("*.json")).read_text() == original
    model_catalog._cache.clear()
    assert model_catalog.native_cli_models("codex")[1][0]["id"] == "first"
    clock[0] += 601
    monkeypatch.setattr(model_catalog, "_codex_models", lambda exe: calls.append(exe) or [{"id": "second"}])
    assert model_catalog.native_cli_models("codex")[1][0]["id"] == "second"
    assert len(calls) == 2


@pytest.mark.parametrize("host", ["opencode", "antigravity"])
def test_create_host_catalogs_are_reused_after_restart(host, monkeypatch):
    from types import SimpleNamespace

    from app.ai import agents, opencode_host

    calls = []
    if host == "opencode":
        card = opencode_host.OpenCodeModel("opencode/test", "opencode", "Test", None, True, ("high",))
        monkeypatch.setattr(opencode_host, "list_models", lambda exe: calls.append(exe) or [card])
        read = lambda: agents.opencode_models("opencode.exe")
        assert read() == [card]
    else:
        monkeypatch.setattr(agents, "find_executable", lambda host: "agy.exe")
        monkeypatch.setattr(agents, "run_command", lambda *args, **kwargs: calls.append(args) or SimpleNamespace(
            returncode=0, stdout="gemini-3-pro-high\tGemini 3 Pro (High)\ngemini-3-pro-low\tGemini 3 Pro (Low)", stderr=""))
        read = lambda: agents.antigravity_models()[1]
        assert read()
    catalog_cache.memory.clear()
    assert read()
    assert len(calls) == 1


def test_catalog_process_timeout_is_bounded_and_process_is_stopped():
    with model_catalog._catalog_session([sys.executable, "-u", "-c", "import time; time.sleep(60)"], timeout=0.1) as session:
        with pytest.raises(CLIIntegrationError) as error:
            session.receive(lambda value: True)
        assert error.value.code == "timeout"
    assert session.process.poll() is not None


def test_reasoning_effort_goes_to_the_native_cli_option():
    codex = build_codex_command("codex", model="m", image_name=None, reasoning_effort="high")
    assert codex[codex.index("-c") + 1] == 'model_reasoning_effort="high"'
    claude = build_claude_command("claude", model="sonnet", with_image=False, reasoning_effort="low")
    assert claude[claude.index("--effort") + 1] == "low"
