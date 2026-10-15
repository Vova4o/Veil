"""Slow environment checks run once when the app starts, so pages answer from caches.

Each check fills the cache its owner keeps (model catalogs, CLI probes, Colab, ComfyUI).
A request that arrives while a check still runs waits for it instead of repeating the work;
a request with ``refresh=1`` always asks again.

Only the checks in ``SPLASH_CHECKS`` hold the desktop splash; the others (Colab, ComfyUI and
its launch) keep running in the background after the app opens.
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

from flask import Flask

logger = logging.getLogger(__name__)

# Long enough for the slowest CLI listing; a request never waits forever on a stuck check.
WAIT_SECONDS = 120.0
SPLASH_CHECKS = frozenset({"models", "cli"})
_pending: dict[str, threading.Event] = {}
_lock = threading.Lock()


def wait(name: str, timeout: float = WAIT_SECONDS) -> None:
    """Block until the startup check ``name`` finished; returns at once when it never ran."""
    with _lock:
        done = _pending.get(name)
    if done is not None and not done.is_set():
        done.wait(timeout)


def running(name: str) -> bool:
    with _lock:
        done = _pending.get(name)
    return done is not None and not done.is_set()


def snapshot() -> dict[str, object]:
    """The splash stays while ``running`` is not empty; ``background`` never holds it."""
    with _lock:
        pending = [name for name, done in _pending.items() if not done.is_set()]
    blocking = [name for name in pending if name in SPLASH_CHECKS]
    return {
        "done": not blocking,
        "running": blocking,
        "background": [name for name in pending if name not in SPLASH_CHECKS],
    }


def _run(app: Flask, name: str, check: Callable[[], None], done: threading.Event) -> None:
    try:
        with app.app_context():
            check()
    except Exception:  # a failed check only leaves its cache cold
        logger.warning("Startup check %s failed", name, exc_info=True)
    finally:
        done.set()


def _checks() -> dict[str, Callable[[], None]]:
    from .ai.routes import warm_cli_integrations, warm_models
    from .comfyui.routes import launch_local_comfyui, warm_comfyui
    from .remote.routes import warm_colab, warm_colab_usage

    return {
        "models": warm_models,
        "cli": warm_cli_integrations,
        "colab": warm_colab,
        "colab-usage": warm_colab_usage,
        "comfyui": warm_comfyui,
        "comfyui-launch": launch_local_comfyui,
    }


def start(app: Flask) -> None:
    """Run every check side by side in the background; MEDIASTUDIO_STARTUP_CHECKS=0 turns them off."""
    if os.environ.get("MEDIASTUDIO_STARTUP_CHECKS", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    checks = _checks()
    # Every check is registered before any runs, so one can wait for another (usage after status).
    events = {name: threading.Event() for name in checks}
    with _lock:
        _pending.update(events)
    for name, check in checks.items():
        threading.Thread(
            target=_run, args=(app, name, check, events[name]), name=f"mediastudio-startup-{name}", daemon=True,
        ).start()


__all__ = ["SPLASH_CHECKS", "WAIT_SECONDS", "running", "snapshot", "start", "wait"]
