"""Shared service lifetime for the browser and desktop launchers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from .paths import RuntimePaths


@contextmanager
def application_runtime(paths: RuntimePaths | None = None) -> Iterator[RuntimePaths]:
    from .comfyui.manager import comfy_manager
    from .job_worker import start_job_worker, stop_job_worker
    from .main import configure_runtime
    from .source_monitor import start_source_monitor, stop_source_monitor
    from .worker import start_worker, stop_worker

    runtime_paths = configure_runtime(paths)
    with ExitStack() as lifetime:
        lifetime.callback(comfy_manager.stop_managed)
        lifetime.callback(stop_worker, wait=True)
        start_worker(runtime_paths.thumbnails)
        lifetime.callback(stop_job_worker, wait=True)
        start_job_worker()
        lifetime.callback(stop_source_monitor)
        start_source_monitor(runtime_paths)
        yield runtime_paths
