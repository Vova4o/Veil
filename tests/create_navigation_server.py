"""Isolated HTTP fixture for the browser navigation smoke (no inference or user data)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from flask import jsonify
from waitress import create_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_create_continuity import create_runtime

from app.comfyui import desktop
from app.main import app

monkeypatch = pytest.MonkeyPatch()
root = Path(tempfile.mkdtemp(prefix="cmv-create-navigation-"))
monkeypatch.setattr(desktop, "desktop_data_dir", lambda: root / "no-desktop")
fixture = create_runtime.__wrapped__(root, monkeypatch)
backend, started, release, output, png = next(fixture)
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
    return jsonify({"queued": len(backend.queued), "agent_started": started.is_set()})


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
