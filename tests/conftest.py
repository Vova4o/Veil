"""Suite hygiene: warn (never fail) on background threads leaked by a test.

A test-time flake from a stray worker thread is otherwise unattributable:
the failure surfaces several files later with no link to the leaker.
This fixture snapshots live threads around every test and logs the
offenders so the next floater arrives with a return address.

Deliberately warning-only: some suites may legitimately leave daemon
threads, and a detector must never become a new flake source itself.
"""

from __future__ import annotations

import logging
import threading

import pytest

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _isolated_model_catalog_cache(tmp_path, monkeypatch):
    from app.ai import catalog_cache
    monkeypatch.setattr(catalog_cache, "cache_directory", lambda: tmp_path / "model-catalogs")


def _live_workers() -> dict[int, str]:
    return {
        thread.ident: f"{thread.name} (daemon={thread.daemon})"
        for thread in threading.enumerate()
        if thread is not threading.current_thread()
        and thread.name != "MainThread"
    }


@pytest.fixture(autouse=True)
def _no_real_comfy_desktop(tmp_path_factory, monkeypatch):
    """Tests never read the developer's own ComfyUI Desktop installation."""
    from app.comfyui import desktop

    missing = tmp_path_factory.getbasetemp() / "no-comfy-desktop"
    monkeypatch.setattr(desktop, "desktop_data_dir", lambda: missing)


class MemorySecretStore:
    """In-memory stand-in for the system credential store."""

    def __init__(self, *, available: bool = True):
        self.values: dict[str, str] = {}
        self.available = available

    def status(self):
        from app.ai.secrets import SecretStoreStatus

        return SecretStoreStatus(
            available=self.available,
            backend="tests.MemorySecretStore" if self.available else None,
            message="Test credential store",
        )

    def _check(self) -> None:
        if not self.available:
            from app.ai.secrets import SecretStoreError

            raise SecretStoreError("Test credential store is unavailable")

    def get(self, key: str) -> str | None:
        self._check()
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self._check()
        self.values[key] = value

    def delete(self, key: str) -> None:
        self._check()
        self.values.pop(key, None)


@pytest.fixture(autouse=True)
def model_source_secrets(monkeypatch):
    """Tests never read or write the developer's own Civitai token in the system store."""
    from app.model_sources import tokens

    secrets = MemorySecretStore()
    monkeypatch.setattr(tokens, "secret_store", lambda: secrets)
    return secrets


@pytest.fixture(autouse=True)
def _warn_on_leaked_threads():
    before = _live_workers()
    yield
    after = _live_workers()
    leaked = {
        ident: desc for ident, desc in after.items() if ident not in before
    }
    # Threads that finished between snapshot and check vanish on their own.
    leaked = {
        ident: desc
        for ident, desc in leaked.items()
        if any(
            thread.ident == ident and thread.is_alive()
            for thread in threading.enumerate()
        )
    }
    if leaked:
        logger.warning(
            "test leaked %d background thread(s): %s",
            len(leaked),
            ", ".join(f"#{ident} {desc}" for ident, desc in leaked.items()),
        )
