"""Where Create runs its graphs: the local ComfyUI or a remote GPU.

A backend hands out a ComfyUI-shaped job client, the live progress monitor that
belongs to that client, and the readiness and profile health Create checks before
it queues runs. The local backend talks to ComfyUI over HTTP; a remote backend
(docs/COLAB-REMOTE-PLAN.md) relays the same calls to a worker on a rented GPU.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from .progress_monitor import ProgressMonitor
    from .simple_profiles import ApprovedProfile

BackendKind = Literal["local", "colab"]


class ComfyUIJobClient(Protocol):
    """The part of the ComfyUI API that Create and WorkflowExecutionService use."""

    def upload_image(
        self,
        filename: str,
        data: bytes,
        *,
        subfolder: str = "cmv",
        overwrite: bool = False,
    ) -> dict[str, Any]: ...

    def queue_prompt(
        self,
        workflow: dict[str, Any],
        *,
        client_id: str,
        prompt_id: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def get_job(self, prompt_id: str) -> dict[str, Any] | None: ...

    def download_output(self, output: dict[str, Any]) -> bytes: ...

    def cancel_prompt(self, prompt_id: str) -> bool: ...


class InferenceBackend(Protocol):
    kind: BackendKind

    def client(self) -> ComfyUIJobClient: ...

    def monitor(self, client: Any) -> ProgressMonitor | None:
        """The live-progress monitor for runs queued through ``client``; None without one."""
        ...

    def ensure_ready(self, timeout: float) -> bool:
        """Whether runs can be queued now; a local backend may start ComfyUI first."""
        ...

    def not_ready_payload(self) -> dict[str, str]:
        """The error, code and suggestion Create shows when ``ensure_ready`` is False."""
        ...

    def profile_health(self, profile: ApprovedProfile) -> dict[str, Any]: ...

    def run_details(self, prompt_id: str) -> dict[str, Any] | None:
        """What a queued run waits for beyond ComfyUI's own progress (a remote GPU starting)."""
        ...


__all__ = ["BackendKind", "ComfyUIJobClient", "InferenceBackend"]
