from __future__ import annotations

import test_create_continuity as continuity

from app import database as db
from app.comfyui import create_sessions, simple_routes
from app.comfyui.create_agent import (
    CreateAgentError,
    CreateAgentResult,
    build_user_input,
)
from app.main import app

# The Create task runtime (fake agent, fake ComfyUI) from the continuity tests.
create_runtime = continuity.create_runtime
wait_for = continuity.wait_for


def _finish(client, task_id):
    wait_for(lambda: client.get(f"/api/simple/tasks/{task_id}").json["is_complete"])
    app.config["CREATE_TASK_SERVICE"].wait(timeout=5)


def test_runs_share_a_named_session_and_the_agent_sees_earlier_runs(create_runtime, monkeypatch):
    _, _, release, _, _ = create_runtime
    release.set()
    seen = []

    def agent(**kwargs):
        seen.append(kwargs.get("session_history"))
        return CreateAgentResult(f"Prompt {len(seen)}.", "", None, "", "OpenCode", "opencode", "m", False, 1, 10)

    monkeypatch.setattr(simple_routes, "run_create_agent", agent)
    monkeypatch.setattr(create_sessions, "title_namer", lambda request: "Night café")
    client = app.test_client()
    for index, text in enumerate(["a night café by the sea, warm light", "the same café in the rain"]):
        response = client.post("/api/simple/tasks", json={
            "task_id": f"session-run-{index}", "request": text, "mode": "text", "session_id": "chat-1"})
        assert response.status_code == 202
        _finish(client, f"session-run-{index}")

    assert seen[0] == []
    assert seen[1] == [{"mode": "text", "request": "a night café by the sea, warm light", "prompt": "Prompt 1."}]
    wait_for(lambda: create_sessions.get_session("chat-1")["title"] == "Night café")
    listed = client.get("/api/simple/sessions").json["sessions"]
    assert [(item["id"], item["runs"], item["title_state"]) for item in listed] == [("chat-1", 2, "agent")]

    data = client.get("/api/simple/sessions/chat-1").json
    assert [run["prompt"] for run in data["runs"]] == ["Prompt 1.", "Prompt 2."]
    assert all(run["status"] == "completed" and len(run["outputs"]) == 1 for run in data["runs"])
    assert data["runs"][0]["created_at"].endswith("+00:00")

    # A deleted image stays in the log as a placeholder.
    deleted = data["runs"][0]["outputs"][0]["id"]
    conn = db.get_conn()
    conn.execute("DELETE FROM images WHERE id=?", (deleted,))
    conn.commit()
    conn.close()
    outputs = client.get("/api/simple/sessions/chat-1").json["runs"][0]["outputs"]
    assert outputs == [{"id": deleted, "deleted": True}]


def test_provider_errors_are_kept_in_the_session_log(create_runtime, monkeypatch):
    def fail(**kwargs):
        raise CreateAgentError("The agent 'OpenCode' failed: rate limited", code="provider_error", status=502,
                               technical_error="429 Too Many Requests")

    monkeypatch.setattr(simple_routes, "run_create_agent", fail)
    monkeypatch.setattr(create_sessions, "title_namer", lambda request: None)
    client = app.test_client()
    client.post("/api/simple/tasks", json={"task_id": "failing", "request": "a garden", "session_id": "chat-2"})
    _finish(client, "failing")
    data = client.get("/api/simple/sessions/chat-2").json
    assert data["session"]["title"] == "A garden"
    assert data["session"]["title_state"] == "fallback"
    run = data["runs"][0]
    assert run["status"] == "failed"
    assert run["error"]["error"].endswith("rate limited")
    assert run["error"]["technical_error"] == "429 Too Many Requests"


def test_rename_and_delete_a_session(create_runtime, monkeypatch):
    monkeypatch.setattr(create_sessions, "title_namer", lambda request: None)
    create_sessions.touch_session("chat-3", request="", mode="reference", name_async=False)
    client = app.test_client()
    assert client.get("/api/simple/sessions/chat-3").json["session"]["title"] == "Recreate a photo"
    renamed = client.patch("/api/simple/sessions/chat-3", json={"title": "  Garden set  "}).json["session"]
    assert (renamed["title"], renamed["title_state"]) == ("Garden set", "manual")
    # A late name from the model never overwrites the user's name.
    create_sessions._set_title("chat-3", "Model name", state="agent", only_from=("fallback",))
    assert create_sessions.get_session("chat-3")["title"] == "Garden set"
    assert client.delete("/api/simple/sessions/chat-3").status_code == 200
    assert client.get("/api/simple/sessions/chat-3").status_code == 404
    assert client.get("/api/simple/sessions/bad id!").status_code == 400


def test_session_history_reaches_the_agent_input():
    text = build_user_input(
        mode="text", request="now at night", previous_prompt=None, image_state="none", dimensions=None,
        session_history=[{"mode": "text", "request": "a café", "prompt": "A café by the sea."}],
    )
    assert "SESSION SO FAR" in text
    assert "1. [text] request: a café\n   prompt written: A café by the sea." in text
    assert text.index("SESSION SO FAR") < text.index("USER REQUEST")
    assert "SESSION SO FAR" not in build_user_input(
        mode="text", request="x", previous_prompt=None, image_state="none", dimensions=None)


def test_titles():
    assert create_sessions.fallback_title("  \n a very long request about a night café by the sea with fog", "text") \
        == "A very long request about a night café by the…"
    assert create_sessions.fallback_title("", "edit") == "Photo edit"
    assert create_sessions._parse_title('{"title": "\\"Rainy café.\\""}') == "Rainy café"
    assert create_sessions._parse_title("Sure! Here it is") == "Sure! Here it is"
