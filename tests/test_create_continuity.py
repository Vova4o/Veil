from __future__ import annotations

import base64
import io
import threading
import time

import pytest
from PIL import Image

from app import database as db
from app.comfyui import simple_routes
from app.comfyui.create_agent import CreateAgentResult
from app.config_store import ConfigStore
from app.main import app


@pytest.fixture
def create_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "meta.db"))
    db.init_db()
    store = ConfigStore(tmp_path / "config.json")
    output = tmp_path / "output"
    output.mkdir()
    config = store.load()
    config["comfyui"]["output_dir"] = str(output)
    store.save(config)
    store.add_source(output, recursive=False)
    monkeypatch.setitem(app.config, "CONFIG_STORE", store)
    monkeypatch.setitem(app.config, "CONFIG_FILE", str(store.path))
    for key in ("THUMBNAIL_FOLDER", "PREVIEW_FOLDER", "CUTOUT_FOLDER"):
        monkeypatch.setitem(app.config, key, str(tmp_path / key))
    monkeypatch.setitem(app.config, "CREATE_TASK_SERVICE", None)
    monkeypatch.delitem(app.config, "CREATE_TASK_SERVICE")
    monkeypatch.setattr(simple_routes, "_ai_profile_store", lambda: None)
    buffer = io.BytesIO()
    Image.new("RGB", (32, 48), "orange").save(buffer, format="PNG")
    png = buffer.getvalue()
    agent_release = threading.Event()
    agent_started = threading.Event()

    def agent(**kwargs):
        agent_started.set()
        kwargs["on_progress"]({"phase": "writing", "text": "A quiet garden."})
        assert agent_release.wait(30)
        return CreateAgentResult("A quiet garden.", "", None, "", "OpenCode", "opencode", "m", True, 1, 10)

    class Backend:
        kind = "local"

        def __init__(self):
            self.queued = []
            self.cancelled = []
            self.startup_started = threading.Event()
            self.startup_release = threading.Event()
            self.startup_release.set()
            self.startup_ok = True
            self.startup_calls = 0

        def client(self):
            return self

        def ensure_ready(self, timeout):
            self.startup_calls += 1
            self.startup_started.set()
            assert self.startup_release.wait(30)
            return self.startup_ok

        def not_ready_payload(self):
            return {"error": "Startup timed out.", "code": "comfyui_connection_failed"}

        def profile_health(self, profile):
            return {"status": "ready", "modes_ready": dict.fromkeys(profile.modes, True)}

        def monitor(self, client):
            return None

        def run_details(self, prompt_id):
            return None

        def queue_prompt(self, workflow, **kwargs):
            prompt_id = str(len(self.queued) + 1)
            self.queued.append(workflow)
            folder = output / "CMV" / "Create"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"image_{prompt_id}.png").write_bytes(png)
            return {"prompt_id": prompt_id}

        def upload_image(self, filename, data, **kwargs):
            return {"name": filename, "subfolder": "cmv"}

        def get_job(self, prompt_id):
            return {"status": "completed", "outputs": {"13": {"images": [
                {"filename": f"image_{prompt_id}.png", "subfolder": "CMV/Create", "type": "output"}
            ]}}}

        def download_output(self, output_ref):
            return png

        def cancel_prompt(self, prompt_id):
            self.cancelled.append(prompt_id)
            return True

    backend = Backend()
    backend.generation_release = threading.Event()
    backend.generation_release.set()
    original_get_job = backend.get_job
    def get_job(prompt_id):
        if not backend.generation_release.is_set():
            return {"status": "running"}
        return original_get_job(prompt_id)
    backend.get_job = get_job
    monkeypatch.setattr(simple_routes, "run_create_agent", agent)
    monkeypatch.setattr(simple_routes, "_backend", lambda kind=None: backend)
    try:
        yield backend, agent_started, agent_release, output, png
    finally:
        agent_release.set()
        backend.startup_release.set()
        service = app.config.get("CREATE_TASK_SERVICE")
        if service:
            service.wait(timeout=5)


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_reference_task_finishes_and_imports_batch_without_create_page(create_runtime):
    backend, started, release, output, png = create_runtime
    # Existing files from ComfyUI must remain visible too, including subfolders.
    other = output / "other"
    other.mkdir()
    (other / "outside-cmv.png").write_bytes(png)
    request = {"task_id": "reference-task", "mode": "reference", "request": "a garden",
               "reference_image": "data:image/png;base64," + base64.b64encode(png).decode(), "batch_size": 2}
    response = app.test_client().post("/api/simple/tasks", json=request)
    assert response.status_code == 202
    assert started.wait(2)
    # The tab is gone: there are no status GETs to advance the pipeline or import results.
    app.test_client().get("/library")
    app.test_client().get("/")
    release.set()
    wait_for(lambda: len(backend.queued) == 2)
    wait_for(lambda: db.get_images_page(None).total == 3)
    app.config["CREATE_TASK_SERVICE"].wait(timeout=5)
    data = app.test_client().get("/api/simple/tasks/reference-task?include_request=1").json
    assert data["status"] == "completed"
    assert len(data["outputs"]) == 2
    assert len(data["run_ids"]) == 2
    assert data["request"]["reference_image"] == request["reference_image"]
    assert data["plan"]["positive_prompt"] == "A quiet garden."
    folder = next(item for item in db.get_folders() if item.path == str(output))
    assert folder.recursive
    assert len(db.get_images_page(folder.id).images) == 3
    # A lost receipt/retry with the same task ID must not launch another agent or batch.
    assert app.test_client().post("/api/simple/tasks", json=request).status_code == 202
    assert len(backend.queued) == 2


def test_stop_during_agent_does_not_queue_generation_after_its_answer(create_runtime):
    backend, started, release, _, _ = create_runtime
    response = app.test_client().post("/api/simple/tasks", json={
        "task_id": "stopped-task", "request": "a garden", "mode": "text"})
    assert response.status_code == 202
    assert started.wait(2)
    assert app.test_client().post("/api/simple/tasks/stopped-task/cancel").json["status"] == "cancelled"
    release.set()
    app.config["CREATE_TASK_SERVICE"].wait(timeout=5)
    assert backend.queued == []


def test_local_startup_overlaps_agent_and_keeps_prompt_before_startup_failure(create_runtime):
    backend, started, release, _, _ = create_runtime
    backend.startup_release.clear()
    backend.startup_ok = False
    client = app.test_client()
    assert client.post("/api/simple/tasks", json={
        "task_id": "cold-start", "request": "a garden", "mode": "text"}).status_code == 202
    assert started.wait(2)
    assert backend.startup_started.wait(2), "ComfyUI startup waits for the agent"
    release.set()
    wait_for(lambda: client.get("/api/simple/tasks/cold-start").json.get("plan"))
    preparing = client.get("/api/simple/tasks/cold-start").json
    assert preparing["plan"]["positive_prompt"] == "A quiet garden."
    assert preparing["runtime_progress"]["state"] == "starting"
    assert backend.queued == []
    backend.startup_release.set()
    app.config["CREATE_TASK_SERVICE"].wait(timeout=5)
    failed = client.get("/api/simple/tasks/cold-start").json
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "comfyui_connection_failed"
    assert failed["plan"] == preparing["plan"]
    assert backend.startup_calls == 1


def test_agent_failure_does_not_wait_for_parallel_comfyui_startup(create_runtime, monkeypatch):
    from app.comfyui.create_agent import CreateAgentError
    backend, _, _, _, _ = create_runtime
    backend.startup_release.clear()

    def fail(**kwargs):
        assert backend.startup_started.wait(2)
        raise CreateAgentError("Agent failed.", code="agent_failed", status=502)

    monkeypatch.setattr(simple_routes, "run_create_agent", fail)
    client = app.test_client()
    client.post("/api/simple/tasks", json={"task_id": "agent-failure", "request": "garden"})
    wait_for(lambda: client.get("/api/simple/tasks/agent-failure").json["is_complete"])
    assert client.get("/api/simple/tasks/agent-failure").json["error"]["stage"] == "agent"
    assert backend.queued == []


def test_stop_while_local_startup_waits_keeps_prompt_and_never_queues(create_runtime):
    backend, started, release, _, _ = create_runtime
    backend.startup_release.clear()
    client = app.test_client()
    client.post("/api/simple/tasks", json={"task_id": "stop-startup", "request": "garden"})
    assert started.wait(2)
    release.set()
    wait_for(lambda: client.get("/api/simple/tasks/stop-startup").json.get("plan"))
    stopped = client.post("/api/simple/tasks/stop-startup/cancel").json
    assert stopped["status"] == "cancelled"
    assert stopped["plan"]["positive_prompt"] == "A quiet garden."
    backend.startup_release.set()
    app.config["CREATE_TASK_SERVICE"].wait(timeout=5)
    assert backend.queued == []


def test_ready_local_runtime_waits_for_agent_before_queueing(create_runtime):
    backend, started, release, _, _ = create_runtime
    client = app.test_client()
    client.post("/api/simple/tasks", json={"task_id": "ready-first", "request": "garden"})
    assert started.wait(2)
    wait_for(lambda: client.get("/api/simple/tasks/ready-first").json.get("runtime_progress", {}).get("state") == "ready")
    assert backend.queued == []
    assert client.get("/api/simple/tasks/ready-first").json["phase"] == "agent"
    release.set()
    app.config["CREATE_TASK_SERVICE"].wait(timeout=5)
    assert len(backend.queued) == 1
    assert backend.startup_calls == 1


def test_task_id_cannot_be_reused_for_another_request(create_runtime):
    _, started, release, _, _ = create_runtime
    request = {"task_id": "same-task", "request": "a garden", "mode": "text"}
    assert app.test_client().post("/api/simple/tasks", json=request).status_code == 202
    assert started.wait(2)
    response = app.test_client().post("/api/simple/tasks", json={**request, "request": "a lake"})
    assert response.status_code == 409
    release.set()


def test_prepared_variation_runs_without_an_agent_or_an_open_page(create_runtime):
    backend, started, _, _, png = create_runtime
    response = app.test_client().post("/api/simple/tasks", json={
        "task_id": "variation-task", "use_agent": False, "mode": "variation", "prompt": "A quiet garden.",
        "reference_image": "data:image/png;base64," + base64.b64encode(png).decode(), "denoise": 0.65,
    })
    assert response.status_code == 202
    app.config["CREATE_TASK_SERVICE"].wait(timeout=5)
    assert not started.is_set()
    assert len(backend.queued) == 1
    assert app.test_client().get("/api/simple/tasks/variation-task").json["status"] == "completed"


def test_backend_selection_is_pinned_while_the_agent_runs(create_runtime, monkeypatch):
    backend, started, release, _, _ = create_runtime
    other = type(backend)()
    other.kind = "colab"
    selected = [backend]
    monkeypatch.setattr(simple_routes, "_backend", lambda kind=None: backend if kind == "local" else selected[0])
    assert app.test_client().post("/api/simple/tasks", json={
        "task_id": "pinned-task", "mode": "text", "request": "a garden"}).status_code == 202
    assert started.wait(2)
    selected[0] = other
    release.set()
    app.config["CREATE_TASK_SERVICE"].wait(timeout=5)
    assert len(backend.queued) == 1
    assert other.queued == []
    assert app.test_client().get("/api/simple/tasks/pinned-task").json["backend"] == "local"


def test_stop_racing_with_queue_receipt_cancels_that_run_and_the_rest_of_batch(create_runtime, monkeypatch):
    backend, _, _, _, _ = create_runtime
    submitting = threading.Event()
    receipt = threading.Event()
    original_queue = backend.queue_prompt
    def queue(*args, **kwargs):
        submitting.set()
        assert receipt.wait(5)
        return original_queue(*args, **kwargs)
    monkeypatch.setattr(backend, "queue_prompt", queue)
    assert app.test_client().post("/api/simple/tasks", json={
        "task_id": "queue-race", "use_agent": False, "prompt": "a garden", "batch_size": 2}).status_code == 202
    assert submitting.wait(2)
    try:
        assert app.test_client().post("/api/simple/tasks/queue-race/cancel").json["status"] == "cancelled"
    finally:
        receipt.set()
    app.config["CREATE_TASK_SERVICE"].wait(timeout=5)
    assert len(backend.queued) == 1
    assert backend.cancelled == ["1"]
    assert app.test_client().get("/api/simple/tasks/queue-race").json["run_ids"]


def test_startup_backfills_previous_create_outputs_without_a_new_generation(create_runtime, monkeypatch):
    from app.main import configure_runtime
    from app.paths import build_runtime_paths

    backend, _, _, output, png = create_runtime
    folder = output / "CMV" / "Create"
    folder.mkdir(parents=True)
    (folder / "previous.png").write_bytes(png)
    paths = build_runtime_paths({"COMFY_META_DATA_DIR": str(output.parent),
                                 "COMFY_META_CACHE_DIR": str(output.parent / "cache")})
    for key in paths.flask_config():
        monkeypatch.setitem(app.config, key, app.config.get(key))
    configure_runtime(paths)
    assert db.get_images_page(None).total == 1
    assert backend.queued == []


def test_startup_preserves_an_explicitly_disabled_output_source(create_runtime):
    from app.comfyui.create_outputs import ensure_output_source

    _, _, _, output, _ = create_runtime
    store = app.config["CONFIG_STORE"]
    store.update_source(output, enabled=False)
    with app.app_context():
        ensure_output_source(store)
    assert not store.sources()[0].enabled
