"""Isolated HTTP fixture for the browser navigation smoke (no inference or user data)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from flask import jsonify, request
from waitress import create_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_create_continuity import create_runtime

from app.comfyui import desktop, simple_routes
from app.comfyui.create_agent import CreateAgentError
from app.main import app

monkeypatch = pytest.MonkeyPatch()
root = Path(tempfile.mkdtemp(prefix="cmv-create-navigation-"))
monkeypatch.setattr(desktop, "desktop_data_dir", lambda: root / "no-desktop")
fixture = create_runtime.__wrapped__(root, monkeypatch)
backend, started, release, output, png = next(fixture)
normal_agent = simple_routes.run_create_agent
backend.generation_release.clear()
(root / "reference.png").write_bytes(png)


@app.route("/__smoke/agent", methods=["POST"])
def release_agent():
    release.set()
    return jsonify({"ok": True})


@app.route("/__smoke/generation", methods=["POST"])
def release_generation():
    backend.generation_release.set()
    return jsonify({"ok": True})


@app.route("/__smoke/state")
def smoke_state():
    return jsonify({"queued": len(backend.queued), "agent_started": started.is_set(),
                    "startup_started": backend.startup_started.is_set()})


@app.route("/__smoke/startup", methods=["POST"])
def control_startup():
    action = request.json["action"]
    if action == "hold":
        monkeypatch.setattr(simple_routes, "run_create_agent", normal_agent)
        started.clear()
        release.clear()
        backend.startup_started.clear()
        backend.startup_release.clear()
        backend.startup_ok = False
    else:
        backend.startup_release.set()
    return jsonify({"ok": True})


@app.route("/__smoke/agent-error", methods=["POST"])
def fail_next_agent():
    def fail(**kwargs):
        raise CreateAgentError(
            "Antigravity returned an invalid structured result.", code="contract_violation", status=502,
            technical_error="schema_version must be a string.",
        )
    monkeypatch.setattr(simple_routes, "run_create_agent", fail)
    return jsonify({"ok": True})


server = create_server(app, host="127.0.0.1", port=0, threads=8)
print(f"SMOKE_READY {server.effective_port} {root}", flush=True)
try:
    server.run()
finally:
    release.set()
    backend.generation_release.set()
    server.close()
    try:
        next(fixture)
    except StopIteration:
        pass
    monkeypatch.undo()
