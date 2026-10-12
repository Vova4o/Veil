"""Create on a Colab GPU: the inference backend and ComfyUI-shaped client over ColabRuntime."""

from __future__ import annotations

import atexit
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.comfyui.backends import BackendKind
from app.comfyui.simple_profiles import (
    APPROVED_PROFILES,
    ApprovedProfile,
    check_profile_health,
)
from app.config_store import ConfigStore

from . import wsl
from .colab_cli import ColabCLI, ColabCLIError
from .colab_runtime import ColabRuntime, RelayedProgressMonitor, RemoteJob

SIGN_IN_CHECK_SECONDS = 60.0
NOT_READY = {
    "error": "Colab is not ready: sign in to Google in Integrations › Remote GPU.",
    "code": "colab_signed_out",
    "suggestion": "Open Integrations › Remote GPU, install the Colab CLI and sign in, or switch Create back to this computer.",
}


def workflow_values(workflow: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for node in workflow.values():
        if isinstance(node, dict):
            for value in (node.get("inputs") or {}).values():
                if isinstance(value, str):
                    values.add(value)
    return values


def job_resources(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """The profile models a workflow loads, by the file names in its inputs."""
    used = workflow_values(workflow)
    found: dict[str, dict[str, Any]] = {}
    for profile in APPROVED_PROFILES.values():
        for dep in profile.required_resources:
            if dep.download_url and dep.filename in used:
                found[dep.filename] = {
                    "folder": dep.folder, "filename": dep.filename, "url": dep.download_url, "sha256": dep.sha256,
                }
    return list(found.values())


def job_inputs(workflow: dict[str, Any], stashed: set[str]) -> list[str]:
    """Photos the workflow loads that Create uploaded for it."""
    return sorted(value for value in workflow_values(workflow) if value in stashed)


class ColabComfyUIClient:
    """The ComfyUI job API that Create and WorkflowExecutionService use, run on Colab."""

    def __init__(self, runtime: ColabRuntime):
        self.runtime = runtime

    def upload_image(self, filename: str, data: bytes, *, subfolder: str = "cmv", overwrite: bool = False) -> dict[str, Any]:
        name = f"{subfolder}/{filename}" if subfolder else filename
        self.runtime.stash_input(name, data)
        return {"name": filename, "subfolder": subfolder, "type": "input"}

    def queue_prompt(
        self,
        workflow: dict[str, Any],
        *,
        client_id: str,
        prompt_id: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_id = prompt_id or str(uuid.uuid4())
        self.runtime.submit(RemoteJob(
            prompt_id=job_id,
            workflow=workflow,
            extra_data=extra_data or {},
            inputs=job_inputs(workflow, self.runtime.stashed_inputs()),
            resources=job_resources(workflow),
        ))
        return {"prompt_id": job_id, "number": 0, "node_errors": {}}

    def get_job(self, prompt_id: str) -> dict[str, Any] | None:
        return self.runtime.job_snapshot(prompt_id)

    def download_output(self, output: dict[str, Any]) -> bytes:
        return self.runtime.output_bytes(output)

    def cancel_prompt(self, prompt_id: str) -> bool:
        self.runtime.cancel(prompt_id)
        return True


class ColabBackend:
    kind: BackendKind = "colab"

    def __init__(self, runtime: ColabRuntime):
        self.runtime = runtime
        self._signed_in: tuple[float, bool] | None = None

    def client(self) -> ColabComfyUIClient:
        return ColabComfyUIClient(self.runtime)

    def monitor(self, client: Any) -> RelayedProgressMonitor:
        return self.runtime.monitor

    def _signed_in_now(self) -> bool:
        cached = self._signed_in
        if cached and time.monotonic() - cached[0] < SIGN_IN_CHECK_SECONDS:
            return cached[1]
        try:
            signed_in = self.runtime.cli.token_present()
        except (ColabCLIError, wsl.WslError):
            signed_in = False
        self._signed_in = (time.monotonic(), signed_in)
        return signed_in

    def ensure_ready(self, timeout: float) -> bool:
        """Runs are queued at once; they wait here while the runtime starts (about 1-2 minutes)."""
        if self.runtime.state in {"booting", "ready", "allocating"}:
            return True
        if not self._signed_in_now():
            return False
        self.runtime.ensure_started()
        return True

    def not_ready_payload(self) -> dict[str, str]:
        return dict(NOT_READY)

    def profile_health(self, profile: ApprovedProfile) -> dict[str, Any]:
        # The worker downloads the profile's models and installs its node packs itself.
        models: dict[str, list[str]] = {}
        for dep in profile.required_resources:
            models.setdefault(dep.folder, []).append(dep.filename)
        inventory = SimpleNamespace(
            online=True,
            node_types=[name for pack in profile.custom_nodes for name in pack.node_classes],
            models=models,
            source="colab",
        )
        health = check_profile_health(profile, inventory, installed_node_packs={pack.id for pack in profile.custom_nodes})
        health["backend"] = {"kind": "colab", "signed_in": self._signed_in_now(), **self.runtime.status()}
        return health

    def run_details(self, prompt_id: str) -> dict[str, Any] | None:
        return self.runtime.run_details(prompt_id)


_lock = threading.Lock()
_runtime: ColabRuntime | None = None


def colab_runtime(store: ConfigStore) -> ColabRuntime:
    """The process-wide runtime manager, created on first use."""
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = ColabRuntime(store, cache_dir=Path(store.path).parent / "remote-cache", cli=ColabCLI())
            atexit.register(_stop_on_exit)
        return _runtime


def existing_runtime() -> ColabRuntime | None:
    return _runtime


def colab_backend(store: ConfigStore) -> ColabBackend:
    return ColabBackend(colab_runtime(store))


def _stop_on_exit() -> None:
    # Best effort; the worker also ends the runtime by itself after the idle time.
    if _runtime is not None:
        _runtime.stop("CMV closed")


__all__ = [
    "ColabBackend",
    "ColabComfyUIClient",
    "colab_backend",
    "colab_runtime",
    "existing_runtime",
    "job_inputs",
    "job_resources",
]
