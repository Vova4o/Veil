from __future__ import annotations

import threading
import time

from app import startup_checks
from app.ai import catalog_cache
from app.main import app


def test_requests_wait_for_a_running_check(monkeypatch):
    release = threading.Event()
    finished = []

    def slow():
        release.wait(5)
        finished.append("slow")

    def broken():
        raise RuntimeError("no CLI")

    monkeypatch.setattr(startup_checks, "_pending", {})
    monkeypatch.setattr(startup_checks, "_checks", lambda: {"slow": slow, "broken": broken})
    monkeypatch.delenv("MEDIASTUDIO_STARTUP_CHECKS", raising=False)
    startup_checks.start(app)
    assert startup_checks.running("slow")
    threading.Timer(0.1, release.set).start()
    startup_checks.wait("slow", timeout=5)
    assert finished == ["slow"]
    startup_checks.wait("broken", timeout=5)
    assert not startup_checks.running("broken")
    startup_checks.wait("never-started", timeout=5)


def test_checks_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(startup_checks, "_pending", {})
    monkeypatch.setattr(startup_checks, "_checks", lambda: {"x": lambda: None})
    monkeypatch.setenv("MEDIASTUDIO_STARTUP_CHECKS", "0")
    startup_checks.start(app)
    assert startup_checks._pending == {}


def test_models_listed_by_this_process_do_not_expire(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_cache, "cache_directory", lambda: tmp_path)
    monkeypatch.setattr(catalog_cache, "memory", {})
    executable = tmp_path / "tool.exe"
    executable.write_text("x")
    catalog_cache.save("codex", str(executable), [{"id": "gpt-6"}])
    later = time.time() + catalog_cache.TTL_SECONDS * 10
    monkeypatch.setattr(catalog_cache.time, "time", lambda: later)
    assert catalog_cache.load("codex", str(executable)) == [{"id": "gpt-6"}]
    # A card a previous run left on disk still expires.
    catalog_cache.memory.clear()
    assert catalog_cache.load("codex", str(executable)) is None


def test_startup_route_lists_running_checks(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(startup_checks, "_pending", {})
    monkeypatch.setattr(startup_checks, "_checks", lambda: {
        "models": lambda: release.wait(5), "comfyui-launch": lambda: release.wait(5),
    })
    monkeypatch.delenv("MEDIASTUDIO_STARTUP_CHECKS", raising=False)
    startup_checks.start(app)
    client = app.test_client()
    assert client.get("/api/startup").get_json() == {
        "done": False, "running": ["models"], "background": ["comfyui-launch"],
    }
    release.set()
    startup_checks.wait("models", timeout=5)
    startup_checks.wait("comfyui-launch", timeout=5)
    assert client.get("/api/startup").get_json() == {"done": True, "running": [], "background": []}


def test_background_checks_do_not_hold_the_splash(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(startup_checks, "_pending", {})
    monkeypatch.setattr(startup_checks, "_checks", lambda: {"colab": lambda: release.wait(5)})
    monkeypatch.delenv("MEDIASTUDIO_STARTUP_CHECKS", raising=False)
    startup_checks.start(app)
    assert startup_checks.snapshot() == {"done": True, "running": [], "background": ["colab"]}
    release.set()
    startup_checks.wait("colab", timeout=5)
