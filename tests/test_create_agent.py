from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.ai import agents, opencode_host
from app.ai.cli import CommandResult
from app.ai.execution import codex as codex_module
from app.ai.execution.adapters import CodexAgentHostAdapter
from app.ai.execution.codex import (
    LAST_MESSAGE_FILE,
    CodexPromptExecutionError,
    CodexPromptExecutor,
    build_codex_command,
    codex_model,
)
from app.ai.execution.router import ExecutionRouter
from app.ai.prompting import PromptResult
from app.ai.providers import AIProviderError
from app.comfyui import create_agent, simple_routes
from app.comfyui.create_agent import (
    CREATE_CONTENT_DIR,
    CreateAgentError,
    agent_image_bytes,
    build_create_bundle,
    run_create_agent,
    wants_intimate_module,
    wants_original_module,
)
from app.comfyui.simple_profiles import APPROVED_PROFILES
from app.main import app

PROFILE = APPROVED_PROFILES["krea2_moody_v40"]


def _png(width=640, height=960) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 80, 60)).save(buffer, format="PNG")
    return buffer.getvalue()


def _data_url(width=640, height=960) -> str:
    return "data:image/png;base64," + base64.b64encode(_png(width, height)).decode("ascii")


VISION = {"id": "vision", "name": "Vision agent", "kind": "cli", "cli_type": "claude", "model": "m",
          "roles": ["vision"], "multimodal": True, "timeout_seconds": 60}
TEXT = {"id": "text", "name": "Text agent", "kind": "cli", "cli_type": "opencode", "model": "m",
        "roles": ["text"], "multimodal": False, "timeout_seconds": 60}


@pytest.fixture(autouse=True)
def _no_local_opencode(monkeypatch, request):
    """Keep the zero-setup OpenCode fallback away from the machine's real installs."""
    if not request.node.name.startswith(("test_implicit_opencode", "test_default_")):
        monkeypatch.setattr(agents, "implicit_opencode_profile", lambda: None)


class FakeStore:
    """Integrations with one default model (an execution profile), or none."""

    def __init__(self, default=None, *, ref=None, disconnected=()):
        self.default = default
        self.ref = ref or ({"host": "fake", "model": default["model"], "variant": None} if default else None)
        self.disconnected = set(disconnected)

    def default_model(self):
        return self.ref

    def disconnected_tools(self):
        return self.disconnected

    def providers(self):
        return []

    def visibility(self):
        return {}

    def get(self, provider_id):
        raise AIProviderError("This provider is not connected.", code="provider_not_found")

    def resolve_api_key(self, profile):
        return None


@pytest.fixture(autouse=True)
def _default_model_from_the_fake_store(monkeypatch):
    real_resolve = agents.resolve

    def resolve(store, ref):
        if isinstance(store, FakeStore) and ref.get("host") == "fake":
            return store.default
        return real_resolve(store, ref)

    monkeypatch.setattr(agents, "resolve", resolve)


class FakeRouter:
    def __init__(self, result: PromptResult, image_inputs=("file_path",)):
        self.result = result
        self.image_inputs = image_inputs
        self.calls = []

    def capabilities_for(self, profile):
        return SimpleNamespace(image_inputs=self.image_inputs)

    def execute(self, **kwargs):
        image_path = kwargs.get("image_path")
        kwargs["image_size"] = Image.open(image_path).size if image_path else None
        self.calls.append(kwargs)
        return SimpleNamespace(result=self.result, adapter_id="fake", job_id=7, metadata={"latency_ms": 42})


# --- instruction ---------------------------------------------------------------


def test_instruction_has_no_child_like_or_t5_rules():
    text = "\n".join(path.read_text(encoding="utf-8") for path in CREATE_CONTENT_DIR.glob("*.md")).casefold()
    # Rules of the original instruction.md that were removed on purpose (T5 legacy).
    for phrase in ("base block (first 500 tokens)", "t5-xxl native"):
        assert phrase not in text, phrase
    assert "clearly adults" in text
    assert "qwen3-vl" in text


def test_bundle_modules_follow_the_mode():
    text = build_create_bundle(PROFILE, "text")
    assert [section.section_id for section in text.sections] == [
        "krea2_base", "mode_text", "composition", "wardrobe", "create_krea2",
    ]
    edit = build_create_bundle(PROFILE, "edit", intimate=True)
    assert [section.section_id for section in edit.sections] == [
        "krea2_base", "mode_edit", "edit_profiles", "wardrobe", "intimate", "create_krea2",
    ]
    ref = build_create_bundle(PROFILE, "reference")
    assert [section.section_id for section in ref.sections] == [
        "krea2_base", "mode_reference", "composition", "wardrobe", "intimate", "create_krea2",
    ]
    rendered = edit.render()
    assert rendered.startswith("CMV CREATE TASK")
    assert "mode: edit" in rendered
    assert "family: flux" not in rendered
    assert "- light:" in rendered and "- donor:" not in rendered
    donor = build_create_bundle(PROFILE, "edit", references=2).render()
    assert "- donor:" in donor and "- donor_detail:" in donor
    assert "- light:" not in donor and "- heavy:" not in donor
    assert "Photometric & Lighting Harmonization" in donor
    assert "Packshot, Catalog & White-Background" in donor


def test_variation_bundle_describes_a_vae_conditioned_result_without_edit_instructions():
    bundle = build_create_bundle(PROFILE, "variation")
    assert "mode: variation" in bundle.render()
    sections = {item.section_id: item.content for item in bundle.sections}
    assert "composition" in sections and "mode_variation" in sections
    assert "edit_profiles" not in sections and "mode_reference" not in sections
    assert "VAE-encoded starting latent" in sections["mode_variation"]


@pytest.mark.parametrize(("text", "expected"), [
    ("сделай хард", True),
    ("раздень её до белья", True),
    ("make her topless", True),
    ("переоденем её в микробикини", True),
    ("девушка в купальнике", True),
    ("dressed in a microbikini", True),
    ("a cat on a windowsill at dawn", False),
    ("ночная кофейня у моря", False),
])
def test_intimate_module_trigger(text, expected):
    assert wants_intimate_module(text) is expected


@pytest.mark.parametrize(("text", "expected"), [
    ("original", True),
    ("[ORIGINAL] describe the photo", True),
    ("оригинал", True),
    ("оригинальное фото", True),
    ("1:1", True),
    ("опиши 1 : 1, поменяй цвет блузки", True),
    ("one-to-one", True),
    ("one to one", True),
    ("один в один", True),
    ("один-в-один", True),
    ("как на фото", True),
    ("как на исходнике", True),
    ("как есть", True),
    ("as-is", True),
    ("без приукрас", True),
    ("без прикрас", True),
    ("без приукрашивания", True),
    ("без идеализации", True),
    ("без ретуши", True),
    ("no beautification", True),
    ("without idealization", True),
    ("no retouching", True),
    (None, False),
    ("", False),
    ("originality", False),
    ("unoriginal", False),
    ("11:1", False),
    ("1:10", False),
    ("1:1:1", False),
    ("1:1.5", False),
    ("0.1:1", False),
    ("оригинальность", False),
    ("как обычно", False),
    ("change the blouse color", False),
])
def test_original_module_trigger(text, expected):
    assert wants_original_module(text) is expected


@pytest.mark.parametrize(("mode", "references", "edit_profile"), [
    ("text", 0, None),
    ("reference", 1, None),
    ("variation", 1, None),
    ("edit", 1, "light"),
    ("edit", 2, "donor_detail"),
])
def test_original_request_reaches_the_agent_in_every_create_route(mode, references, edit_profile):
    router = FakeRouter(PromptResult(positive_prompt="The requested result.", edit_profile=edit_profile))
    request = "original, change the blouse color to blue"
    result = run_create_agent(
        profile=PROFILE, store=FakeStore(VISION), mode=mode, request=request,
        image=_png() if references else None, subject=_png() if references == 2 else None, router=router,
    )
    call = router.calls[0]
    sections = {item.section_id: item.content for item in call["bundle"].sections}
    assert "original" in sections
    assert "without idealizing" in sections["original"]
    assert "mode_" + mode in sections
    assert call["bundle"].sections[-1].section_id == "create_krea2"
    assert request in call["user_input"]
    assert result.edit_profile == edit_profile


def test_original_is_a_current_request_modifier_not_inherited_prompt_text():
    router = FakeRouter(PromptResult(positive_prompt="A blue blouse."))
    run_create_agent(
        profile=PROFILE, store=FakeStore(TEXT), mode="text", request="change the blouse color to blue",
        previous_prompt="An original painting with a woman in a red blouse.", router=router,
    )
    call = router.calls[0]
    assert "original" not in {item.section_id for item in call["bundle"].sections}
    assert "PREVIOUS PROMPT" in call["user_input"]


# --- agent profile -----------------------------------------------------------------


def test_default_model_runs_and_a_photo_task_prefers_a_free_vision_model(monkeypatch):
    implicit = {**VISION, "id": agents.IMPLICIT_OPENCODE_PROFILE_ID, "model_card": True}
    monkeypatch.setattr(agents, "implicit_opencode_profile", lambda: implicit)
    assert agents.default_profile(FakeStore(VISION), wants_image=True)["id"] == "vision"
    assert agents.default_profile(FakeStore(TEXT), wants_image=False)["id"] == "text"
    # A text-only default model hands photo tasks to the free OpenCode vision model.
    assert agents.default_profile(FakeStore(TEXT), wants_image=True)["id"] == agents.IMPLICIT_OPENCODE_PROFILE_ID
    assert agents.default_profile(FakeStore(), wants_image=False)["id"] == agents.IMPLICIT_OPENCODE_PROFILE_ID
    monkeypatch.setattr(agents, "implicit_opencode_profile", lambda: None)
    # Without a free model the photo task still runs the default model, blind.
    assert agents.default_profile(FakeStore(TEXT), wants_image=True)["id"] == "text"
    assert agents.default_profile(FakeStore(), wants_image=True) is None


def test_default_model_that_no_longer_exists_falls_back(monkeypatch):
    implicit = {**VISION, "id": agents.IMPLICIT_OPENCODE_PROFILE_ID, "model_card": True}
    monkeypatch.setattr(agents, "implicit_opencode_profile", lambda: implicit)
    store = FakeStore(ref={"host": "codex", "model": "gone"}, disconnected={"codex"})
    assert agents.default_profile(store, wants_image=False)["id"] == agents.IMPLICIT_OPENCODE_PROFILE_ID


def _free_models():
    model = opencode_host.OpenCodeModel
    return [
        model("opencode/big-pickle", "opencode", "Big Pickle", vision=False, free=True),
        model("opencode/claude-opus-5", "opencode", "Claude Opus 5", vision=True, free=False),
        model("opencode/exo-free", "opencode", "Exo Free", vision=True, free=True),
        model("opencode/muse-spark-1.3-contributor-free", "opencode", "Muse Spark 1.3 Free", vision=True, free=True),
    ]


def test_implicit_opencode_runs_a_free_vision_model_when_nothing_is_configured(monkeypatch):
    installation = opencode_host.OpenCodeInstallation("desktop", "C:/OpenCode/opencode-cli.exe", "2.0.24", 2)
    monkeypatch.setattr(opencode_host, "find_installations", lambda: [installation])
    monkeypatch.setattr(opencode_host, "list_models", lambda executable, provider=None: _free_models())
    agents._free_model_cache.clear()
    profile = agents.default_profile(FakeStore(), wants_image=True)
    assert profile["id"] == agents.IMPLICIT_OPENCODE_PROFILE_ID
    assert profile["model"] == "opencode/muse-spark-1.3-contributor-free"
    assert profile["multimodal"] is True and "vision" in profile["roles"]
    assert profile["executable"] == installation.executable
    # A default model that sees photos always wins over the fallback.
    assert agents.default_profile(FakeStore(VISION), wants_image=True)["id"] == "vision"


def test_implicit_opencode_needs_an_installation_and_a_free_model(monkeypatch):
    installation = opencode_host.OpenCodeInstallation("cli", "C:/npm/opencode.cmd", "1.18.34", 1)
    monkeypatch.setattr(opencode_host, "find_installations", list)
    assert agents.implicit_opencode_profile() is None
    monkeypatch.setattr(opencode_host, "find_installations", lambda: [installation])
    monkeypatch.setattr(opencode_host, "list_models", lambda executable, provider=None: [_free_models()[1]])
    agents._free_model_cache.clear()
    assert agents.implicit_opencode_profile() is None


# --- runs ----------------------------------------------------------------------------


def test_reference_mode_needs_an_agent_that_sees_photos():
    router = FakeRouter(PromptResult(positive_prompt="x"))
    with pytest.raises(CreateAgentError) as exc_info:
        run_create_agent(profile=PROFILE, store=FakeStore(TEXT), mode="reference", request="",
                         image=_png(), router=router)
    assert exc_info.value.code == "vision_required"
    assert router.calls == []


def test_no_agent_configured():
    with pytest.raises(CreateAgentError) as exc_info:
        run_create_agent(profile=PROFILE, store=FakeStore(), mode="text", request="a cat",
                         router=FakeRouter(PromptResult(positive_prompt="x")))
    assert exc_info.value.code == "no_ai_model"


def test_text_run_passes_bundle_request_and_previous_prompt():
    router = FakeRouter(PromptResult(positive_prompt="A cat on a sill.", edit_profile="light", note=""))
    result = run_create_agent(profile=PROFILE, store=FakeStore(TEXT), mode="text", request="кот на окне",
                              previous_prompt="A dog.", dimensions=(896, 1152), router=router)
    call = router.calls[0]
    assert call["bundle"].task.operation.value == "generate"
    assert "кот на окне" in call["user_input"] and "PREVIOUS PROMPT" in call["user_input"]
    assert "896x1152" in call["user_input"]
    assert call["profile"]["timeout_seconds"] >= create_agent.MIN_AGENT_TIMEOUT_SECONDS
    assert "image_path" not in call
    # Edit profiles only exist in edit mode.
    assert result.edit_profile is None
    assert result.prompt == "A cat on a sill."
    assert result.to_dict()["agent"]["name"] == "Text agent"


def test_edit_run_uses_agent_profile_and_negative():
    router = FakeRouter(PromptResult(positive_prompt="She is topless.", negative_prompt="sweater",
                                     edit_profile="remove", note="Try 2-4 images."))
    result = run_create_agent(profile=PROFILE, store=FakeStore(VISION), mode="edit",
                              request="сними свитер полностью", image=_png(), router=router)
    call = router.calls[0]
    assert call["image_size"] == (640, 960)
    assert "intimate" in [item.section_id for item in call["bundle"].sections]
    assert (result.edit_profile, result.negative_prompt, result.note) == ("remove", "sweater", "Try 2-4 images.")
    assert result.saw_image is True


def test_edit_run_with_donor_sends_side_by_side_sheet_and_retains_profile():
    router = FakeRouter(PromptResult(positive_prompt="Put the person from the second image.", edit_profile="donor"))
    result = run_create_agent(profile=PROFILE, store=FakeStore(VISION), mode="edit", request="swap",
                              image=_png(), subject=_png(500, 500), router=router)
    width, height = router.calls[0]["image_size"]
    assert width > height  # two photos side by side
    assert "side-by-side" in router.calls[0]["user_input"]
    assert result.edit_profile == "donor"


@pytest.mark.parametrize(("selected", "second_photo"), [
    ("light", True), ("donor", False), ("unknown", False), (None, False), (None, True),
])
def test_agent_cannot_silently_substitute_an_incompatible_edit_profile(selected, second_photo):
    router = FakeRouter(PromptResult(positive_prompt="Recolor the cup.", edit_profile=selected))
    with pytest.raises(CreateAgentError) as caught:
        run_create_agent(profile=PROFILE, store=FakeStore(VISION), mode="edit", request="Recolor the cup.",
                         image=_png(), subject=_png() if second_photo else None, router=router)
    assert caught.value.code == "edit_profile_invalid"
    assert caught.value.status == 502


def test_edit_task_reports_a_frame_change():
    router = FakeRouter(PromptResult(positive_prompt="Change the sweater to red.", edit_profile="light"))
    run_create_agent(profile=PROFILE, store=FakeStore(VISION), mode="edit", request="красный свитер",
                     image=_png(640, 960), dimensions=(1344, 768), router=router)
    text = router.calls[0]["user_input"]
    assert "PHOTO 1 SIZE: 640x960 pixels" in text
    assert "FRAME CHANGE: the output is wider than photo 1" in text and "strips at the sides" in text
    assert "## Frame change" in router.calls[0]["bundle"].render()


def test_edit_task_in_the_photo_frame_has_no_frame_change():
    router = FakeRouter(PromptResult(positive_prompt="Change the sweater to red.", edit_profile="light"))
    run_create_agent(profile=PROFILE, store=FakeStore(VISION), mode="edit", request="красный свитер",
                     image=_png(640, 960), dimensions=(832, 1248), router=router)
    text = router.calls[0]["user_input"]
    assert "PHOTO 1 SIZE: 640x960 pixels" in text
    assert "FRAME CHANGE" not in text


def test_edit_without_vision_agent_writes_command_blind():
    router = FakeRouter(PromptResult(positive_prompt="Make the dress red.", edit_profile="light"))
    result = run_create_agent(profile=PROFILE, store=FakeStore(TEXT), mode="edit", request="красное платье",
                              image=_png(), router=router)
    assert "image_path" not in router.calls[0]
    assert "cannot see it" in router.calls[0]["user_input"]
    assert result.saw_image is False


def test_direct_agents_get_a_data_url():
    router = FakeRouter(PromptResult(positive_prompt="x"), image_inputs=("data_url",))
    run_create_agent(profile=PROFILE, store=FakeStore(VISION), mode="reference", request="", image=_png(),
                     router=router)
    assert router.calls[0]["image_data_url"].startswith("data:image/jpeg;base64,")


def test_agent_image_is_bounded():
    data = agent_image_bytes(_png(4000, 3000))
    assert max(Image.open(io.BytesIO(data)).size) <= create_agent.AGENT_IMAGE_MAX_SIDE


def test_agent_route_returns_prompt_and_profile_timing(monkeypatch):
    router = FakeRouter(PromptResult(positive_prompt="Pose change.", edit_profile="pose", note="~7 min"))
    monkeypatch.setattr(create_agent, "ExecutionRouter", lambda: router)
    monkeypatch.setattr(simple_routes, "_ai_provider_store", lambda: FakeStore(VISION))
    response = app.test_client().post("/api/simple/agent", json={
        "mode": "edit", "request": "поверни голову к окну", "reference_image": _data_url(), "aspect_ratio": "source",
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data["positive_prompt"] == "Pose change."
    assert data["edit_profile"] == "pose"
    assert data["seconds_per_image"] == 400
    assert data["agent"]["saw_image"] is True
    assert data["width"] % 32 == 0


def test_agent_route_reports_an_unknown_provider(monkeypatch):
    monkeypatch.setattr(simple_routes, "_ai_provider_store", lambda: FakeStore(TEXT))
    response = app.test_client().post("/api/simple/agent", json={
        "mode": "text", "request": "a cat", "agent_host": "api:missing", "agent_model": "m",
    })
    assert response.status_code == 404
    assert response.get_json()["code"] == "provider_not_found"
    invalid = app.test_client().post("/api/simple/agent", json={
        "mode": "text", "request": "a cat", "agent_host": "../etc", "agent_model": "m",
    })
    assert invalid.status_code == 400


def test_agent_route_reports_missing_agent(monkeypatch):
    monkeypatch.setattr(simple_routes, "_ai_provider_store", lambda: FakeStore())
    response = app.test_client().post("/api/simple/agent", json={"mode": "text", "request": "a cat"})
    assert response.status_code == 409
    assert response.get_json()["code"] == "no_ai_model"


# --- streamed progress ---------------------------------------------------------------


def _opencode_events(*events):
    return [json.dumps(event) + "\n" for event in events]


def test_opencode_progress_shows_phases_and_the_prompt_not_the_json():
    seen = []
    read = create_agent.progress_reader({"kind": "cli", "cli_type": "opencode"}, seen.append)
    answer = json.dumps({"positive_prompt": "A lantern-lit night market.", "negative_prompt": ""})
    for line in _opencode_events(
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "reasoning", "part": {"type": "reasoning", "text": ""}},
        {"type": "text", "part": {"type": "text", "text": f"```json\n{answer}\n```"}},
    ) + ["not json\n"]:
        read(line)
    assert [event["phase"] for event in seen] == ["thinking", "thinking", "writing"]
    assert seen[-1]["text"] == "A lantern-lit night market."
    assert "log" not in seen[1]


def test_codex_progress_skips_the_echoed_task_and_keeps_the_journal():
    seen = []
    read = create_agent.progress_reader({"kind": "cli", "cli_type": "codex"}, seen.append)
    journal = [
        "OpenAI Codex v0.160.0", "--------", "model: gpt-x", "--------",
        "user", "CMV CREATE TASK", "SECTION FAMILY_BASE: secret instruction text", "",
        "thinking", "**Planning the frame**",
        "mcp: memory-agent/recall started",
        "codex", '{"positive_prompt": "x"', "}",
        "tokens used", "38 612",
    ]
    for line in journal:
        read(line + "\n")
    logs = [event.get("log") for event in seen if event.get("log")]
    assert logs == ["**Planning the frame**", "mcp: memory-agent/recall started"]
    assert not any("secret instruction" in str(event) or "gpt-x" in str(event) for event in seen)
    assert seen[-1] == {"phase": "writing"}
    assert create_agent.progress_reader({"kind": "cli", "cli_type": "claude"}, seen.append) is None
    assert create_agent.progress_reader({"kind": "openai_compatible"}, seen.append) is None


@pytest.mark.parametrize("answer", [
    '{"status":"SUCCESS","response":"private transcript","structured_output":{"positive_prompt":"A cup."}}',
    '{\n  "status": "SUCCESS",\n  "structured_output": {"positive_prompt": "A cup."}\n}',
    '```json\n{"positive_prompt":"A cup."}\n```',
])
def test_antigravity_progress_hides_the_final_transport_json(answer):
    seen = []
    read = create_agent.progress_reader({"kind": "cli", "cli_type": "antigravity"}, seen.append)
    read("Reading the attached photo.\n")
    for line in answer.splitlines():
        read(line + "\n")
    assert seen == [{"phase": "working", "log": "Reading the attached photo."}, {"phase": "writing"}]


def test_run_reports_start_and_forwards_host_lines():
    seen = []

    class StreamingRouter(FakeRouter):
        def execute(self, **kwargs):
            for line in _opencode_events({"type": "step_start"}):
                kwargs["on_output_chunk"](line)
            return super().execute(**kwargs)

    router = StreamingRouter(PromptResult(positive_prompt="A cat."))
    run_create_agent(profile=PROFILE, store=FakeStore(TEXT), mode="text", request="a cat",
                     router=router, on_progress=seen.append)
    assert seen[0] == {"phase": "started", "agent": "Text agent", "model": "m", "sees_images": False}
    assert seen[1] == {"phase": "thinking"}
    # Without a listener nothing is parsed.
    plain = FakeRouter(PromptResult(positive_prompt="A cat."))
    run_create_agent(profile=PROFILE, store=FakeStore(TEXT), mode="text", request="a cat", router=plain)
    assert plain.calls[0]["on_output_chunk"] is None


def _sse_events(response):
    return [
        json.loads(line[len("data: "):])
        for line in response.get_data(as_text=True).splitlines()
        if line.startswith("data: ")
    ]


def test_agent_route_streams_progress_then_the_result(monkeypatch):
    def fake_run(**kwargs):
        kwargs["on_progress"]({"phase": "started", "agent": "Text agent"})
        kwargs["on_progress"]({"phase": "writing", "text": "A cat."})
        return create_agent.CreateAgentResult(
            prompt="A cat.", negative_prompt="", edit_profile=None, note="", agent_name="Text agent",
            adapter_id="opencode", model="m", saw_image=False, job_id=3, latency_ms=10,
        )

    monkeypatch.setattr(simple_routes, "run_create_agent", fake_run)
    monkeypatch.setattr(simple_routes, "_ai_provider_store", lambda: FakeStore(TEXT))
    response = app.test_client().post("/api/simple/agent?stream=1", json={"mode": "text", "request": "a cat"})
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    events = _sse_events(response)
    assert [event["event"] for event in events] == ["progress", "progress", "result"]
    assert events[1]["text"] == "A cat."
    assert events[-1]["positive_prompt"] == "A cat."
    assert events[-1]["mode"] == "text" and events[-1]["width"] > 0


def test_agent_route_streams_errors_with_their_status(monkeypatch):
    monkeypatch.setattr(simple_routes, "_ai_provider_store", lambda: FakeStore())
    response = app.test_client().post("/api/simple/agent?stream=1", json={"mode": "text", "request": "a cat"})
    events = _sse_events(response)
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "no_ai_model"
    assert events[-1]["status"] == 409
    # Validation still answers with plain JSON before any stream starts.
    invalid = app.test_client().post("/api/simple/agent?stream=1", json={"mode": "edit", "request": "x"})
    assert invalid.status_code == 400 and invalid.get_json()["code"] == "image_required"


# --- Codex ---------------------------------------------------------------------------


def test_codex_command_reads_stdin_and_attaches_image_before_flags():
    command = build_codex_command("codex", model=None, image_name="input.jpg")
    assert command[:4] == ["codex", "exec", "--image", "input.jpg"]
    assert command[-1] == "-"
    assert "--sandbox" in command and command[command.index("--sandbox") + 1] == "read-only"
    assert "--model" not in command
    assert build_codex_command("codex", model="gpt-x", image_name=None)[-3:] == ["--model", "gpt-x", "-"]
    assert codex_model({"model": "default"}) is None
    assert codex_model({"model": "gpt-x"}) == "gpt-x"


def test_router_selects_codex():
    profile = {"kind": "cli", "cli_type": "codex", "model": "default"}
    assert ExecutionRouter().capabilities_for(profile) is CodexAgentHostAdapter.capabilities


def test_codex_executor_reads_last_message_file(monkeypatch, tmp_path):
    image = tmp_path / "photo.jpg"
    image.write_bytes(agent_image_bytes(_png()))
    calls = []

    def fake_run(args, **kwargs):
        cwd = Path(kwargs["cwd"])
        calls.append({"args": args, "files": sorted(p.name for p in cwd.iterdir()), "stdin": kwargs["input_text"]})
        (cwd / LAST_MESSAGE_FILE).write_text(json.dumps({"schema_version": "1", "positive_prompt": "OK"}))
        return CommandResult(returncode=0, stdout="progress log", stderr="", elapsed_ms=9)

    monkeypatch.setattr(codex_module, "run_command", fake_run)
    monkeypatch.setattr(codex_module, "find_executable", lambda *_: "codex")
    bundle = build_create_bundle(PROFILE, "reference")
    result = CodexPromptExecutor().execute(
        profile={"kind": "cli", "cli_type": "codex", "model": "default"},
        task=bundle.task, user_input="MODE: reference", image_path=image, bundle=bundle,
    )
    assert result.result.positive_prompt == "OK"
    assert calls[0]["files"] == ["input.jpg"]
    assert "attached to this message" in calls[0]["stdin"]
    assert len(" ".join(calls[0]["args"])) < 2000


def test_codex_executor_without_reply_reports_error(monkeypatch):
    monkeypatch.setattr(codex_module, "run_command",
                        lambda args, **kw: CommandResult(returncode=1, stdout="", stderr="Not logged in", elapsed_ms=1))
    monkeypatch.setattr(codex_module, "find_executable", lambda *_: "codex")
    bundle = build_create_bundle(PROFILE, "text")
    with pytest.raises(CodexPromptExecutionError) as exc_info:
        CodexPromptExecutor().execute(profile={"kind": "cli", "cli_type": "codex", "model": "m"},
                                      task=bundle.task, user_input="x", bundle=bundle)
    assert exc_info.value.code == "cli_authentication"


# --- reference images from links ------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: bytes, url: str = "https://example.com/pics/dress.png", length: str | None = None):
        self.data = data
        self.url = url
        self.headers = {"Content-Length": length} if length else {}

    def read(self, limit):
        return self.data[:limit]

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_image_returns_a_data_url(monkeypatch):
    monkeypatch.setattr(simple_routes.urllib.request, "urlopen", lambda request, timeout: _FakeResponse(_png(40, 60)))
    response = app.test_client().post("/api/simple/fetch-image", json={"url": "https://example.com/pics/dress.png"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["data_url"].startswith("data:image/png;base64,")
    assert (data["name"], data["width"], data["height"]) == ("dress.png", 40, 60)


def test_fetch_image_rejects_bad_links_pages_and_huge_files(monkeypatch):
    client = app.test_client()
    for url in ("file:///C:/Windows/win.ini", "ftp://example.com/a.png", "not a link"):
        response = client.post("/api/simple/fetch-image", json={"url": url})
        assert response.status_code == 400 and response.get_json()["code"] == "url_invalid"
    monkeypatch.setattr(simple_routes.urllib.request, "urlopen", lambda request, timeout: _FakeResponse(b"<html></html>"))
    page = client.post("/api/simple/fetch-image", json={"url": "https://example.com/"})
    assert page.status_code == 415 and page.get_json()["code"] == "not_an_image"
    big = str(simple_routes.MAX_SOURCE_IMAGE_BYTES + 1)
    monkeypatch.setattr(simple_routes.urllib.request, "urlopen", lambda request, timeout: _FakeResponse(b"x", length=big))
    huge = client.post("/api/simple/fetch-image", json={"url": "https://example.com/big.png"})
    assert huge.status_code == 413 and huge.get_json()["code"] == "image_too_large"


def test_variation_agent_describes_the_photo_with_its_own_mode(monkeypatch):
    router = FakeRouter(PromptResult(positive_prompt="A woman in a grey sweater on a rocky coast."))
    monkeypatch.setattr(create_agent, "ExecutionRouter", lambda: router)
    monkeypatch.setattr(simple_routes, "_ai_provider_store", lambda: FakeStore(VISION))
    response = app.test_client().post("/api/simple/agent", json={
        "mode": "variation", "request": "", "reference_image": _data_url(), "aspect_ratio": "source",
    })
    assert response.status_code == 200, response.data
    data = response.get_json()
    assert data["mode"] == "variation"
    assert "MODE: variation" in router.calls[0]["user_input"]
    assert "faithful reconstruction" in router.calls[0]["user_input"]
    # The noise level is the user's choice; the agent is never told about it.
    assert "denoise" not in router.calls[0]["user_input"].casefold()
    assert data["width"] % 16 == 0


def test_variation_needs_a_vision_agent():
    router = FakeRouter(PromptResult(positive_prompt="A cup on a table."))
    with pytest.raises(CreateAgentError) as caught:
        run_create_agent(profile=PROFILE, store=FakeStore(TEXT), mode="variation", request="",
                         image=_png(), router=router)
    assert caught.value.code == "vision_required"


# --- agent picked in Create --------------------------------------------------------------


def _cards():
    model = opencode_host.OpenCodeModel
    return [
        model("opencode/exo-free", "opencode", "Exo Free", vision=True, free=True, variants=("high",)),
        model("opencode/ling-3.1-flash-free", "opencode", "Ling 3.1 Flash Free", vision=False, free=True),
    ]


@pytest.fixture
def opencode_cards(monkeypatch):
    installation = opencode_host.OpenCodeInstallation("desktop", "C:/OpenCode/opencode-cli.exe", "2.0.27", 2)
    monkeypatch.setattr(opencode_host, "find_installations", lambda: [installation])
    monkeypatch.setattr(opencode_host, "list_models", lambda executable, provider=None: _cards())
    monkeypatch.setattr(agents, "native_cli_models", lambda host, refresh=False: (None, []))
    monkeypatch.setattr(agents, "antigravity_models", lambda refresh=False: (None, []))
    agents._models_cache.clear()
    yield installation
    agents._models_cache.clear()


def test_a_picked_opencode_model_runs_with_its_card_and_reasoning_level(opencode_cards, monkeypatch):
    profile = agents.opencode_profile("opencode/exo-free", "high")
    assert profile["model"] == "opencode/exo-free#high"
    assert profile["multimodal"] is True and profile["executable"] == opencode_cards.executable
    assert profile["name"] == "OpenCode · Exo Free · high"
    assert agents.opencode_profile("opencode/ling-3.1-flash-free")["multimodal"] is False
    with pytest.raises(agents.AgentError) as unknown:
        agents.opencode_profile("opencode/nope")
    assert unknown.value.code == "model_unavailable"
    with pytest.raises(agents.AgentError) as level:
        agents.opencode_profile("opencode/exo-free", "turbo")
    assert level.value.code == "variant_unavailable"


def test_the_agent_call_uses_the_model_picked_in_create(opencode_cards, monkeypatch):
    router = FakeRouter(PromptResult(positive_prompt="A rainy cafe."))
    monkeypatch.setattr(create_agent, "ExecutionRouter", lambda: router)
    monkeypatch.setattr(simple_routes, "_ai_provider_store", lambda: FakeStore(TEXT))
    response = app.test_client().post("/api/simple/agent", json={
        "mode": "text", "request": "a rainy cafe", "agent_model": "opencode/exo-free", "agent_variant": "high",
    })
    assert response.status_code == 200, response.data
    assert router.calls[0]["profile"]["model"] == "opencode/exo-free#high"
    # A text-only model cannot describe a photo.
    blind = app.test_client().post("/api/simple/agent", json={
        "mode": "reference", "reference_image": _data_url(), "agent_model": "opencode/ling-3.1-flash-free",
    })
    assert blind.status_code == 409 and blind.get_json()["code"] == "vision_required"


def test_the_model_catalog_lists_opencode_cards_and_antigravity_levels(opencode_cards, monkeypatch):
    agy_models = agents.parse_antigravity_models(AGY_MODELS)
    monkeypatch.setattr(agents, "antigravity_models", lambda refresh=False: ("C:/agy/agy.exe", agy_models))
    groups = {group["id"]: group for group in agents.catalog(FakeStore())["groups"]}
    assert list(groups) == ["opencode:opencode", "antigravity"]
    cards = {item["id"]: item for item in groups["opencode:opencode"]["models"]}
    assert cards["opencode/exo-free"]["vision"] is True and cards["opencode/exo-free"]["variants"] == ["high"]
    assert cards["opencode/ling-3.1-flash-free"]["vision"] is False
    assert all(card["visible"] for card in cards.values())
    agy_rows = {item["id"]: item for item in groups["antigravity"]["models"]}
    assert agy_rows["gpt-oss-120b"]["vision"] is False and agy_rows["gemini-3.8-flash"]["vision"] is True
    assert [item["level"] for item in agy_rows["gemini-3.8-flash"]["levels"]] == ["high", "low"]
    # A disconnected tool leaves the catalog.
    assert [group["id"] for group in agents.catalog(FakeStore(disconnected={"antigravity"}))["groups"]] == [
        "opencode:opencode"]


AGY_MODELS = (
    "Fetching available models...\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
    "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
)


def test_antigravity_models_group_their_reasoning_levels_and_know_text_only_families(monkeypatch):
    models = {item["id"]: item for item in agents.parse_antigravity_models(AGY_MODELS)}
    assert models["gemini-3.8-flash"]["name"] == "Gemini 3.8 Flash"
    assert [item["level"] for item in models["gemini-3.8-flash"]["levels"]] == ["high", "low"]
    assert models["gpt-oss-120b"]["vision"] is False
    monkeypatch.setattr(agents, "antigravity_models",
                        lambda refresh=False: ("C:/agy/agy.exe", list(models.values())))
    profile = agents.antigravity_profile("gemini-3.8-flash-low")
    assert (profile["model"], profile["multimodal"], profile["executable"]) == ("gemini-3.8-flash-low", True, "C:/agy/agy.exe")
    assert profile["name"] == "Antigravity · Gemini 3.8 Flash · low"
    assert agents.sees_images(agents.antigravity_profile("gpt-oss-120b-medium")) is False
    with pytest.raises(agents.AgentError):
        agents.antigravity_profile("gemini-9")
    # A base model without a level runs its medium level, else the first one.
    assert agents.antigravity_profile("gemini-3.8-flash", "high")["model"] == "gemini-3.8-flash-high"
    assert agents.antigravity_profile("gemini-3.8-flash")["model"] == "gemini-3.8-flash-high"
    # A run without a model card is judged by its model family.
    assert agents.sees_images({"kind": "cli", "cli_type": "antigravity", "model": "gpt-oss-120b-medium"}) is False
    assert agents.sees_images({"kind": "cli", "cli_type": "antigravity", "model": "gemini-3.8-flash-low"}) is True


def test_antigravity_models_see_photos_unless_text_only_and_duplicate_levels_are_removed():
    models = agents.parse_antigravity_models(AGY_MODELS + AGY_MODELS + "future-text-high\tFuture (High)\n")
    assert len(models[0]["levels"]) == 2
    # Antigravity models see photos; GPT-OSS is the only text-only family.
    assert models[-1]["vision"] is True
    assert agents.sees_images({"kind": "cli", "cli_type": "antigravity", "model": "future-text-high"}) is True


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_native_picked_model_and_effort_reach_executor_and_text_only_is_blocked(host, monkeypatch):
    models = [{"id": "m-vision", "name": "Vision", "vision": True, "variants": ["high"]},
              {"id": "m-text", "name": "Text", "vision": False, "variants": []}]
    monkeypatch.setattr(agents, "native_cli_models", lambda name, refresh=False: (name, models))
    router = FakeRouter(PromptResult(positive_prompt="A rainy cafe."))
    monkeypatch.setattr(create_agent, "ExecutionRouter", lambda: router)
    monkeypatch.setattr(simple_routes, "_ai_provider_store", lambda: FakeStore(TEXT))
    response = app.test_client().post("/api/simple/agent", json={
        "request": "a rainy cafe", "agent_host": host, "agent_model": "m-vision", "agent_variant": "high",
    })
    assert response.status_code == 200, response.data
    profile = router.calls[0]["profile"]
    assert (profile["cli_type"], profile["model"], profile["reasoning_effort"]) == (host, "m-vision", "high")
    blind = app.test_client().post("/api/simple/agent", json={
        "mode": "reference", "reference_image": _data_url(), "agent_host": host, "agent_model": "m-text",
    })
    assert blind.status_code == 409 and blind.get_json()["code"] == "vision_required"
    assert len(router.calls) == 1
    with pytest.raises(agents.AgentError) as missing:
        agents.native_profile(host, "not-listed")
    assert missing.value.code == "model_unavailable"
    with pytest.raises(agents.AgentError) as effort:
        agents.native_profile(host, "m-vision", "not-listed")
    assert effort.value.code == "variant_unavailable"


def test_model_catalog_isolates_host_errors_and_refreshes_every_catalog(opencode_cards, monkeypatch):
    calls = []

    def native(host, refresh=False):
        calls.append((host, refresh))
        if host == "claude":
            raise agents.CLIIntegrationError("Claude is offline")
        return host, [{"id": "gpt-5.5", "name": "GPT", "vision": True, "variants": ["high"]}]

    monkeypatch.setattr(agents, "native_cli_models", native)
    groups = {group["id"]: group for group in agents.catalog(FakeStore(), refresh=True)["groups"]}
    assert groups["opencode:opencode"]["models"] and groups["codex"]["models"]
    assert groups["claude"]["error"] == "Claude is offline" and groups["claude"]["models"] == []
    assert sorted(calls) == [("claude", True), ("codex", True)]
    # Pickers keep a failed host so its error stays visible.
    visible = {group["id"] for group in agents.catalog(FakeStore(), visible_only=True)["groups"]}
    assert "claude" in visible
