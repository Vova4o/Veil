from __future__ import annotations

import json

from app.comfyui.simple_profiles import (
    APPROVED_PROFILES,
    load_simple_workflow_json,
    serialize_approved_profile,
)
from app.main import app


def test_load_simple_workflow():
    flow = load_simple_workflow_json("krea2_moody_v40")
    assert isinstance(flow, dict)
    assert flow


def test_serialize_approved_profile_has_no_quality_presets():
    serialized = serialize_approved_profile(APPROVED_PROFILES["krea2_moody_v40"])
    assert serialized["id"] == "krea2_moody_v40"
    assert "quality_presets" not in serialized
    assert serialized["modes"] == ["text", "reference", "variation", "edit"]
    assert "aspect_ratios" in serialized
    assert "health" in serialized


def test_simple_mode_routes(monkeypatch):
    from app.comfyui import simple_routes

    monkeypatch.setattr(simple_routes.agents, "default_profile", lambda store, *, wants_image: None)
    client = app.test_client()

    resp = client.get("/create")
    assert resp.status_code == 200
    assert b"studio-workspace" in resp.data
    assert b"model-select-trigger" not in resp.data

    resp_editor = client.get("/editor")
    assert resp_editor.status_code == 200
    assert b"studio-workspace" in resp_editor.data

    data = json.loads(client.get("/api/simple/bootstrap").data)
    assert [item["id"] for item in data["profiles"]] == ["krea2_moody_v40"]
    assert data["default_profile_id"] == "krea2_moody_v40"
    assert data["agent_status"]["available"] is False
    assert data["agent_status"]["instruction_files"]

    status = client.get("/api/simple/profiles/krea2_moody_v40/status")
    assert status.status_code == 200
    assert "health" in json.loads(status.data)


def test_legacy_assistant_chat_is_gone():
    client = app.test_client()
    assert client.post("/api/simple/assistant/chat", json={"message": "x"}).status_code in {404, 405}
