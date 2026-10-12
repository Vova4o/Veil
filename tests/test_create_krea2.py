from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from app.comfyui import manager, simple_routes
from app.comfyui.node_packs import (
    MARKER_NAME,
    NodePackInstallError,
    install_node_pack,
    installed_node_packs,
)
from app.comfyui.simple_profiles import (
    APPROVED_PROFILES,
    EDIT_SETTING_TYPES,
    check_profile_health,
    compile_edit_workflow,
    compile_simple_workflow,
    resolve_dimensions,
    resolve_edit_profile,
)
from app.main import app

PROFILE = APPROVED_PROFILES["krea2_moody_v40"]
EDIT_NODES = ["Krea2EditModelPatch", "Krea2EditGroundedEncode"]


def _inventory(*, online=True, node_types=EDIT_NODES, missing_files=()):
    models: dict[str, list[str]] = {}
    for dep in PROFILE.required_resources:
        if dep.filename not in missing_files:
            models.setdefault(dep.folder, []).append(dep.filename)
    return SimpleNamespace(online=online, node_types=list(node_types), models=models)


def _png_data_url(width=640, height=960) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 80, 60)).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def test_krea2_profile_is_the_only_create_model():
    assert list(APPROVED_PROFILES) == ["krea2_moody_v40"]
    assert PROFILE.max_batch == 4
    assert PROFILE.modes == ("text", "reference", "edit")
    assert PROFILE.sda_modes == ("text",)
    assert [pack.id for pack in PROFILE.custom_nodes] == ["comfyui-krea2edit"]
    for item in PROFILE.aspect_ratios:
        pixels = item["width"] * item["height"]
        assert 0.95e6 <= pixels <= 1.1e6
        assert item["width"] % 32 == 0 and item["height"] % 32 == 0


def test_resolve_dimensions_keeps_source_ratio_at_one_megapixel():
    assert resolve_dimensions(PROFILE, "3:4") == (896, 1152)
    width, height = resolve_dimensions(PROFILE, "source", (832, 1216))
    assert width % 32 == 0 and height % 32 == 0
    assert abs(width / height - 832 / 1216) < 0.03
    assert 0.95e6 <= width * height <= 1.1e6
    # Extreme panoramas are clamped instead of producing unusable strips.
    wide_w, wide_h = resolve_dimensions(PROFILE, "source", (5000, 500))
    assert wide_w / wide_h <= 2.6
    # Without a source the first preset is used.
    assert resolve_dimensions(PROFILE, "source") == (1024, 1024)


@pytest.mark.parametrize(("mode", "strength"), [("text", 1.0), ("reference", 0.0)])
def test_txt2img_sda_gate_follows_mode(mode, strength):
    workflow = compile_simple_workflow(
        PROFILE, positive_prompt="A quiet street", seed=7, mode=mode, dimensions=(896, 1152)
    )
    assert workflow["4"]["inputs"]["text"] == "A quiet street"
    assert workflow["10"]["inputs"]["strength_model"] == strength
    assert workflow["11"]["inputs"]["noise_seed"] == 7
    assert workflow["12"]["inputs"]["noise_seed"] == 7
    assert workflow["11"]["inputs"]["end_at_step"] == 2
    assert workflow["12"]["inputs"]["start_at_step"] == 2
    assert workflow["6"]["inputs"] == {"width": 896, "height": 1152, "batch_size": 1}
    assert workflow["11"]["inputs"]["sampler_name"] == "dpmpp_2m_sde_gpu"


def test_edit_workflow_binds_instruction_source_and_size():
    workflow = compile_edit_workflow(
        PROFILE,
        instruction="  Recolor the dress to burgundy.  ",
        source_image="cmv/source.png",
        dimensions=(832, 1216),
        seed=11,
    )
    assert workflow["4"]["inputs"]["prompt"] == "Recolor the dress to burgundy."
    assert workflow["5"]["inputs"]["prompt"] == ""
    assert workflow["15"]["inputs"]["image"] == "cmv/source.png"
    assert workflow["17"]["inputs"]["width"] == 832
    assert workflow["17"]["inputs"]["height"] == 1216
    assert workflow["7"]["inputs"]["seed"] == 11
    assert workflow["14"]["inputs"]["lora_name"] == "krea2_identity_edit_v1_2_r64.safetensors"
    assert "30" not in workflow
    assert "source_latent_b" not in workflow["18"]["inputs"]
    assert "image_b" not in workflow["4"]["inputs"]


def _edit_knobs(workflow):
    return {
        "cfg": workflow["7"]["inputs"]["cfg"],
        "steps": workflow["7"]["inputs"]["steps"],
        "ref_boost": workflow["18"]["inputs"]["ref_boost"],
        "ref_boost_a": workflow["18"]["inputs"]["ref_boost_a"],
        "grounding_px": workflow["4"]["inputs"]["grounding_px"],
    }


def test_edit_profiles_cover_every_knob_and_photo_count():
    ids = [item.id for item in PROFILE.edit_profiles]
    assert ids == ["light", "replace", "body", "remove", "heavy", "pose", "donor", "donor_detail"]
    for item in PROFILE.edit_profiles:
        assert set(item.settings) == set(EDIT_SETTING_TYPES), item.id
        assert item.seconds > 0
    assert resolve_edit_profile(PROFILE).id == "light"
    assert resolve_edit_profile(PROFILE, references=2).id == "donor"


@pytest.mark.parametrize("edit_profile", ["light", "replace", "body", "remove", "heavy", "pose"])
def test_edit_profile_sets_graph_knobs(edit_profile):
    item = resolve_edit_profile(PROFILE, edit_profile)
    workflow = compile_edit_workflow(
        PROFILE, instruction="x", source_image="cmv/s.png", dimensions=(896, 1152), seed=1, edit_profile=edit_profile
    )
    assert _edit_knobs(workflow) == item.settings
    # The negative grounded encode must see the same image at the same grounding size.
    assert workflow["5"]["inputs"]["grounding_px"] == item.settings["grounding_px"]


def test_default_edit_profile_matches_calibrated_graph():
    workflow = compile_edit_workflow(PROFILE, instruction="x", source_image="cmv/s.png", dimensions=(896, 1152))
    assert _edit_knobs(workflow) == {"cfg": 1.0, "steps": 8, "ref_boost": 4.0, "ref_boost_a": 1.0, "grounding_px": 768}


def test_edit_negative_goes_to_grounded_negative_encode():
    workflow = compile_edit_workflow(
        PROFILE,
        instruction="She is topless, with both arms bare.",
        negative_instruction="  a cream knitted sweater with long sleeves  ",
        source_image="cmv/s.png",
        dimensions=(896, 1152),
        edit_profile="remove",
    )
    assert workflow["5"]["inputs"]["prompt"] == "a cream knitted sweater with long sleeves"
    assert workflow["5"]["inputs"]["image"] == ["16", 0]
    assert workflow["7"]["inputs"]["negative"] == ["5", 0]
    assert workflow["7"]["inputs"]["cfg"] == 2.5


def test_edit_with_subject_wires_second_reference():
    workflow = compile_edit_workflow(
        PROFILE,
        instruction="Copy the tattoo from the second image.",
        source_image="cmv/scene.png",
        subject_image="cmv/donor.png",
        dimensions=(896, 1152),
        seed=3,
        edit_profile="donor_detail",
    )
    assert workflow["30"]["inputs"]["image"] == "cmv/donor.png"
    assert workflow["31"]["inputs"]["image"] == ["30", 0]
    assert workflow["32"]["inputs"]["pixels"] == ["31", 0]
    patch = workflow["18"]["inputs"]
    assert patch["source_latent_b"] == ["32", 0]
    assert patch["source_image_b"] == ["31", 0]
    # Scene stays the first reference: the main inputs keep the source photo.
    assert patch["source_image"] == ["16", 0]
    assert workflow["15"]["inputs"]["image"] == "cmv/scene.png"
    assert workflow["4"]["inputs"]["image_b"] == ["31", 0]
    assert workflow["5"]["inputs"]["image_b"] == ["31", 0]
    assert (patch["ref_boost"], patch["ref_boost_a"]) == (2.0, 4.0)
    # Every link points at a node that exists in the compiled graph.
    for node in workflow.values():
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                assert value[0] in workflow


def test_edit_profile_must_match_photo_count():
    with pytest.raises(ValueError):
        resolve_edit_profile(PROFILE, "donor")
    with pytest.raises(ValueError):
        resolve_edit_profile(PROFILE, "heavy", references=2)
    with pytest.raises(ValueError):
        resolve_edit_profile(PROFILE, "nope")


def test_health_blocks_only_edit_when_nodes_are_missing():
    health = check_profile_health(PROFILE, _inventory(node_types=[]))
    assert health["status"] == "ready"
    assert health["modes_ready"] == {"text": True, "reference": True, "edit": False}
    assert health["missing_nodes"][0]["id"] == "comfyui-krea2edit"
    assert health["installable"] is True
    assert health["restart_required"] is False

    pending = check_profile_health(PROFILE, _inventory(node_types=[]), installed_node_packs={"comfyui-krea2edit"})
    assert pending["restart_required"] is True
    assert pending["installable"] is False

    ready = check_profile_health(PROFILE, _inventory())
    assert ready["modes_ready"]["edit"] is True
    assert ready["missing_nodes"] == []


def test_health_treats_offline_nodes_as_unknown():
    health = check_profile_health(PROFILE, _inventory(online=False, node_types=[]))
    assert health["nodes_known"] is False
    assert health["modes_ready"]["edit"] is False
    assert health["modes_ready"]["text"] is True


def test_status_refresh_drops_the_cached_inventory(create_env, monkeypatch):
    from app.comfyui import workflow_inventory

    dropped = []
    monkeypatch.setattr(workflow_inventory, "invalidate_runtime_inventory", lambda: dropped.append(True))
    resp = app.test_client().get("/api/simple/models/krea2_moody_v40/status?refresh=1")
    assert resp.status_code == 200
    assert resp.get_json()["health"]["status"] == "ready"
    assert dropped == [True]


def test_offline_health_trusts_node_packs_on_disk():
    # ComfyUI loads a pack folder when it starts, so offline is not "restart" or "missing".
    on_disk = check_profile_health(
        PROFILE, _inventory(online=False, node_types=[]), installed_node_packs={"comfyui-krea2edit"}
    )
    assert on_disk["status"] == "ready"
    assert on_disk["comfyui_online"] is False
    assert on_disk["missing_nodes"] == []
    assert on_disk["modes_ready"]["edit"] is True
    assert on_disk["restart_required"] is False

    absent = check_profile_health(PROFILE, _inventory(online=False, node_types=[]), installed_node_packs=set())
    assert absent["nodes_known"] is True
    assert absent["missing_nodes"][0]["installed_on_disk"] is False
    assert absent["modes_ready"]["edit"] is False
    assert absent["restart_required"] is False
    assert absent["installable"] is True


def test_health_reports_missing_models_with_download_links():
    health = check_profile_health(PROFILE, _inventory(missing_files=("krea2_turbo_sda_v1.0_comfy.safetensors",)))
    assert health["status"] == "not_installed"
    assert health["installable"] is True
    assert health["modes_ready"] == {"text": False, "reference": False, "edit": False}


def _pack_archive(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _pack(entry: bytes):
    return replace(PROFILE.custom_nodes[0], verify_sha256=hashlib.sha256(entry).hexdigest())


def test_install_node_pack_verifies_and_writes_marker(tmp_path):
    entry = b"NODE_CLASS_MAPPINGS = {}\n"
    archive = _pack_archive({
        "repo-abc/__init__.py": entry,
        "repo-abc/workflows/example.json": b"{}",
        "repo-abc/tests/test_x.py": b"",
        "repo-abc/node.zip": b"zip",
    })
    pack = _pack(entry)
    result = install_node_pack(pack, tmp_path, download=lambda url: archive)

    target = tmp_path / pack.folder
    assert result["status"] == "installed"
    assert (target / "__init__.py").read_bytes() == entry
    assert (target / "workflows" / "example.json").is_file()
    assert not (target / "tests").exists() and not (target / "node.zip").exists()
    assert json.loads((target / MARKER_NAME).read_text(encoding="utf-8"))["commit"] == pack.commit
    assert installed_node_packs([pack], tmp_path) == {pack.id}
    # A second call never overwrites an existing folder.
    assert install_node_pack(pack, tmp_path, download=lambda url: b"")["status"] == "already_present"


def test_install_node_pack_rejects_checksum_mismatch(tmp_path):
    archive = _pack_archive({"repo-abc/__init__.py": b"tampered"})
    with pytest.raises(NodePackInstallError) as excinfo:
        install_node_pack(_pack(b"reviewed"), tmp_path, download=lambda url: archive)
    assert excinfo.value.code == "checksum_mismatch"
    assert list(tmp_path.iterdir()) == []


def test_install_node_pack_rejects_path_traversal(tmp_path):
    entry = b"ok"
    archive = _pack_archive({"repo-abc/__init__.py": entry, "repo-abc/../../evil.py": b"x"})
    with pytest.raises(NodePackInstallError) as excinfo:
        install_node_pack(_pack(entry), tmp_path, download=lambda url: archive)
    assert excinfo.value.code == "unsafe_archive"
    assert list(tmp_path.iterdir()) == []


class _FakeClient:
    def __init__(self):
        self.queued: list[dict] = []
        self.uploads: list[tuple[str, int]] = []

    def upload_image(self, filename, data, *, subfolder="cmv", overwrite=False):
        self.uploads.append((filename, len(data)))
        return {"name": filename, "subfolder": subfolder, "type": "input"}

    def queue_prompt(self, workflow, *, client_id, extra_data=None):
        self.queued.append({"workflow": workflow, "extra": extra_data})
        return {"prompt_id": f"p{len(self.queued)}"}


class _FakeStore:
    def __init__(self):
        self.drafts: list[dict] = []
        self.runs: list[str] = []

    def create_draft(self, **kwargs):
        self.drafts.append(kwargs)
        return SimpleNamespace(id=len(self.drafts))

    def create_run(self, *, draft_id, prompt_id, client_id):
        self.runs.append(prompt_id)
        return SimpleNamespace(id=100 + len(self.runs))


@pytest.fixture
def create_env(monkeypatch):
    fake_client, fake_store = _FakeClient(), _FakeStore()
    monkeypatch.setattr(simple_routes, "_inventory_or_none", lambda: _inventory())
    monkeypatch.setattr(simple_routes, "_custom_nodes_root", lambda: None)
    monkeypatch.setattr(simple_routes, "_client", lambda: fake_client)
    monkeypatch.setattr(simple_routes, "_workflow_store", lambda: fake_store)
    monkeypatch.setattr(manager, "ensure_comfyui_online", lambda store, timeout=60.0: True)
    return fake_client, fake_store


def test_generate_text_mode_queues_one_prompt_per_image(create_env):
    fake_client, fake_store = create_env
    resp = app.test_client().post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40",
        "mode": "text",
        "prompt": "A lighthouse in fog",
        "improve_with_ai": False,
        "aspect_ratio": "9:16",
        "batch_size": 3,
        "seed": 500,
    })
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data["run_ids"] == [101, 102, 103]
    assert data["run_id"] == 101
    assert (data["width"], data["height"]) == (768, 1344)
    seeds = [item["workflow"]["11"]["inputs"]["noise_seed"] for item in fake_client.queued]
    assert seeds == [500, 501, 502]
    assert all(item["workflow"]["10"]["inputs"]["strength_model"] == 1.0 for item in fake_client.queued)
    assert fake_client.queued[0]["extra"]["extra_pnginfo"]["cmv_create_mode"] == "text"
    assert fake_store.drafts[0]["values"]["seeds"] == [500, 501, 502]


class _RemoteBackend:
    """A backend without a local ComfyUI: readiness, health, client and monitor come from it."""

    kind = "colab"

    def __init__(self, client, *, ready=True):
        self._client, self._ready = client, ready

    def client(self):
        return self._client

    def monitor(self, client):
        return None

    def ensure_ready(self, timeout):
        return self._ready

    def profile_health(self, profile):
        return check_profile_health(profile, _inventory())


@pytest.fixture
def remote_env(monkeypatch):
    fake_client, fake_store = _FakeClient(), _FakeStore()
    monkeypatch.setattr(simple_routes, "_workflow_store", lambda: fake_store)
    monkeypatch.setattr(manager, "ensure_comfyui_online", lambda *args, **kwargs: pytest.fail("local ComfyUI was started"))
    return fake_client, fake_store


def test_generate_runs_on_the_chosen_backend(remote_env, monkeypatch):
    fake_client, fake_store = remote_env
    monkeypatch.setattr(simple_routes, "_backend", lambda: _RemoteBackend(fake_client))
    resp = app.test_client().post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40", "mode": "text", "prompt": "A lighthouse in fog",
        "improve_with_ai": False, "batch_size": 2, "seed": 7,
    })
    assert resp.status_code == 200, resp.data
    assert len(fake_client.queued) == 2
    assert fake_store.runs == ["p1", "p2"]


def test_generate_reports_a_backend_that_is_not_ready(remote_env, monkeypatch):
    fake_client, _ = remote_env
    monkeypatch.setattr(simple_routes, "_backend", lambda: _RemoteBackend(fake_client, ready=False))
    resp = app.test_client().post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40", "mode": "text", "prompt": "x", "improve_with_ai": False,
    })
    assert resp.status_code == 503
    assert resp.get_json()["code"] == "comfyui_connection_failed"
    assert fake_client.queued == []


def test_generate_batch_is_capped_at_profile_maximum(create_env):
    fake_client, _ = create_env
    resp = app.test_client().post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40", "prompt": "x", "improve_with_ai": False, "batch_size": 9, "seed": 1,
    })
    assert resp.status_code == 200
    assert len(fake_client.queued) == 4


def test_generate_edit_uploads_source_and_uses_its_ratio(create_env):
    fake_client, _ = create_env
    resp = app.test_client().post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40",
        "mode": "edit",
        "prompt": "Change the dress to deep burgundy red.",
        "reference_image": _png_data_url(640, 960),
        "aspect_ratio": "source",
        "batch_size": 2,
        "seed": 9,
    })
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert len(fake_client.uploads) == 1
    assert len(fake_client.queued) == 2
    workflow = fake_client.queued[0]["workflow"]
    assert workflow["15"]["inputs"]["image"].startswith("cmv/cmv_source_")
    assert workflow["4"]["inputs"]["prompt"] == "Change the dress to deep burgundy red."
    assert abs(data["width"] / data["height"] - 640 / 960) < 0.03
    assert [item["workflow"]["7"]["inputs"]["seed"] for item in fake_client.queued] == [9, 10]


def test_generate_edit_with_donor_and_profile(create_env):
    fake_client, fake_store = create_env
    resp = app.test_client().post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40",
        "mode": "edit",
        "prompt": "Replace the woman with the woman from the second image.",
        "reference_image": _png_data_url(640, 960),
        "subject_image": _png_data_url(512, 768),
        "aspect_ratio": "source",
        "seed": 4,
    })
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data["edit_profile"] == "donor"
    assert data["seconds_per_image"] > 0
    assert [name.split("_")[1] for name, _size in fake_client.uploads] == ["source", "subject"]
    workflow = fake_client.queued[0]["workflow"]
    assert workflow["30"]["inputs"]["image"].startswith("cmv/cmv_subject_")
    assert fake_client.queued[0]["extra"]["extra_pnginfo"]["cmv_edit_profile"] == "donor"
    assert fake_store.drafts[0]["values"]["edit_profile"] == "donor"
    assert fake_store.drafts[0]["values"]["subject_image"].startswith("cmv/cmv_subject_")


def test_generate_edit_heavy_profile_raises_cfg(create_env):
    fake_client, _ = create_env
    resp = app.test_client().post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40",
        "mode": "edit",
        "edit_profile": "heavy",
        "prompt": "Change her face.",
        "reference_image": _png_data_url(),
    })
    assert resp.status_code == 200, resp.data
    assert fake_client.queued[0]["workflow"]["7"]["inputs"]["cfg"] == 2.5
    assert fake_client.queued[0]["workflow"]["5"]["inputs"]["prompt"] == ""
    assert len(fake_client.uploads) == 1


def test_generate_edit_remove_profile_carries_negative(create_env):
    fake_client, fake_store = create_env
    resp = app.test_client().post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40",
        "mode": "edit",
        "edit_profile": "remove",
        "prompt": "She is topless, with both arms bare from the shoulders to the wrists.",
        "negative_prompt": "a cream knitted sweater with long sleeves",
        "reference_image": _png_data_url(),
    })
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["negative_prompt"] == "a cream knitted sweater with long sleeves"
    workflow = fake_client.queued[0]["workflow"]
    assert workflow["5"]["inputs"]["prompt"] == "a cream knitted sweater with long sleeves"
    assert fake_client.queued[0]["extra"]["extra_pnginfo"]["cmv_negative_prompt"].startswith("a cream")
    assert fake_store.drafts[0]["values"]["negative_prompt"].startswith("a cream")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"mode": "edit", "edit_profile": "donor"}, "edit_profile_invalid"),
        ({"mode": "edit", "edit_profile": "bogus"}, "edit_profile_invalid"),
        ({"mode": "edit", "subject_image": "data:image/png;base64,AAAA"}, "subject_invalid"),
        ({"mode": "reference", "edit_profile": "light"}, "edit_only"),
    ],
)
def test_generate_rejects_bad_edit_options(create_env, payload, code):
    fake_client, _ = create_env
    resp = app.test_client().post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40", "prompt": "x", "reference_image": _png_data_url(), **payload,
    })
    assert resp.status_code == 400
    assert resp.get_json()["code"] == code
    assert fake_client.uploads == [] and fake_client.queued == []


def test_generate_edit_requires_image_and_instruction(create_env):
    client = app.test_client()
    no_image = client.post("/api/simple/generate", json={"profile_id": "krea2_moody_v40", "mode": "edit", "prompt": "x"})
    assert no_image.status_code == 400
    assert no_image.get_json()["code"] == "image_required"
    no_text = client.post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40", "mode": "edit", "prompt": " ", "reference_image": _png_data_url(),
    })
    assert no_text.status_code == 400
    assert no_text.get_json()["code"] == "instruction_required"


def test_generate_edit_reports_restart_when_nodes_not_loaded(create_env, monkeypatch, tmp_path):
    (tmp_path / "comfyui-krea2edit").mkdir()
    monkeypatch.setattr(simple_routes, "_inventory_or_none", lambda: _inventory(node_types=[]))
    monkeypatch.setattr(simple_routes, "_custom_nodes_root", lambda: tmp_path)
    resp = app.test_client().post("/api/simple/generate", json={
        "profile_id": "krea2_moody_v40", "mode": "edit", "prompt": "x", "reference_image": _png_data_url(),
    })
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "restart_required"


def test_install_route_installs_missing_node_pack(create_env, monkeypatch, tmp_path):
    monkeypatch.setattr(simple_routes, "_inventory_or_none", lambda: _inventory(node_types=[]))
    monkeypatch.setattr(simple_routes, "_custom_nodes_root", lambda: tmp_path)

    class _Downloads:
        def resolve_model_root(self):
            return tmp_path

    monkeypatch.setattr(simple_routes, "_download_service", lambda: _Downloads())
    calls = []

    def fake_install(pack, root):
        calls.append((pack.id, root))
        (root / pack.folder).mkdir()
        return {"id": pack.id, "status": "installed"}

    monkeypatch.setattr(simple_routes, "install_node_pack", fake_install)
    resp = app.test_client().post("/api/simple/models/krea2_moody_v40/install")
    assert resp.status_code == 200
    data = resp.get_json()
    assert calls == [("comfyui-krea2edit", tmp_path)]
    assert data["restart_required"] is True
    assert data["node_packs"][0]["status"] == "installed"
    assert data["manual_install"] == []
