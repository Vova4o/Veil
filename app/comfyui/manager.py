from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Any

from .client import ComfyUIClient, ComfyUIClientError
from .desktop import launch_settings, state_dir_of
from .detector import ComfyUIDetectionResult, detect_comfyui
from .launcher import generate_launcher_script
from .validation import validate_extra_args
from .win_job import CREATE_SUSPENDED, KillOnCloseJob, resume_process


class ComfyUIMode(str, Enum):
    NONE = "none"
    MANAGED = "managed"
    EXTERNAL = "external"


class ComfyUIStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    STOPPING = "stopping"
    ERROR = "error"
    EXTERNAL = "external"


class ComfyUIManager:
    """Thread-safe Process & Integration Manager for local ComfyUI.

    Handles:
    - Managed process launching (subprocess.Popen)
    - Stdout/stderr log capturing into ring buffer
    - Health polling & readiness detection
    - External ComfyUI detection & connection
    - Interrupt and clean stopping
    """

    def __init__(self, max_log_lines: int = 1000):
        self._lock = threading.RLock()
        self._mode: ComfyUIMode = ComfyUIMode.NONE
        self._status: ComfyUIStatus = ComfyUIStatus.STOPPED
        self._process: subprocess.Popen | None = None
        self._job_object: KillOnCloseJob | None = None
        self._host: str = "127.0.0.1"
        self._port: int = 8188
        self._last_error: str | None = None
        self._logs: deque[str] = deque(maxlen=max_log_lines)
        self._reader_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._stop_monitor_event = threading.Event()
        self._installation: ComfyUIDetectionResult | None = None
        self._launcher_script: Path | None = None

    @property
    def mode(self) -> ComfyUIMode:
        with self._lock:
            return self._mode

    @property
    def status(self) -> ComfyUIStatus:
        with self._lock:
            return self._status

    @property
    def pid(self) -> int | None:
        with self._lock:
            if self._mode == ComfyUIMode.MANAGED and self._process:
                return self._process.pid
            return None

    def get_info(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self._mode.value,
                "status": self._status.value,
                "pid": self.pid,
                "host": self._host,
                "port": self._port,
                "last_error": self._last_error,
                "installation": self._installation.to_dict() if self._installation else None,
                "launcher_script": str(self._launcher_script) if self._launcher_script else None,
            }

    def get_logs(self, max_lines: int = 200) -> list[str]:
        with self._lock:
            lines = list(self._logs)
            return lines[-max_lines:]

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()

    def set_installation(
        self,
        install_path: str | Path,
        custom_python: str | Path | None = None,
    ) -> ComfyUIDetectionResult:
        with self._lock:
            detection = detect_comfyui(install_path, custom_python=custom_python)
            self._installation = detection
            if not detection.is_valid:
                self._last_error = detection.error
            else:
                self._last_error = None
            return detection

    def start_managed(
        self,
        install_path: str | Path,
        host: str = "127.0.0.1",
        port: int = 8188,
        extra_args: str | Sequence[str] | None = None,
        custom_python: str | Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._mode == ComfyUIMode.MANAGED and self._process and self._process.poll() is None:
                raise RuntimeError("ComfyUI managed process is already running.")

            detection = self.set_installation(install_path, custom_python=custom_python)
            if not detection.is_valid:
                self._status = ComfyUIStatus.ERROR
                self._last_error = detection.error or "Invalid installation directory"
                raise ValueError(f"Cannot start ComfyUI: {self._last_error}")

            self._host = host
            self._port = port

            try:
                self._launcher_script = generate_launcher_script(
                    detection=detection,
                    extra_args=extra_args,
                    host=host,
                    port=port,
                )
            except Exception as exc:
                self._log(f"[CMV] Warning: failed to generate launcher script: {exc}")

            # -s skips the user's site-packages, as Desktop and the portable build launch ComfyUI.
            cmd = [str(detection.interpreter), "-s", str(detection.main_py), "--listen", host, "--port", str(port)]
            if extra_args:
                tokens = validate_extra_args(extra_args)
                cmd.extend(tokens)

            self._log(f"[CMV] Launching ComfyUI managed process: {' '.join(cmd)}")
            self._log(f"[CMV] Working directory: {detection.comfy_dir}")

            # Captured Python output must support Unicode even on Windows with a
            # legacy console code page. Use the same encoding for both pipe ends.
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            popen_kwargs: dict[str, Any] = {
                "cwd": str(detection.comfy_dir),
                "env": child_env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            }

            self._close_job()
            job = self._create_job()
            if sys.platform == "win32":
                # No console window of its own; the job needs the process suspended until it is assigned.
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | (CREATE_SUSPENDED if job else 0)

            try:
                process = subprocess.Popen(cmd, **popen_kwargs)
                if job is not None:
                    job = self._enter_job(process, job)
            except Exception as exc:
                if job is not None:
                    job.close()
                self._status = ComfyUIStatus.ERROR
                self._last_error = str(exc)
                self._log(f"[CMV] Failed to spawn process: {exc}")
                raise RuntimeError(f"Failed to spawn ComfyUI process: {exc}") from exc
            self._process = process
            self._job_object = job

            self._mode = ComfyUIMode.MANAGED
            self._status = ComfyUIStatus.STARTING
            self._last_error = None

            self._reader_thread = threading.Thread(
                target=self._read_stdout_loop,
                args=(self._process,),
                daemon=True,
            )
            self._reader_thread.start()

            self._stop_monitor_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
            )
            self._monitor_thread.start()

            return self.get_info()

    def stop_managed(self) -> None:
        with self._lock:
            if self._mode != ComfyUIMode.MANAGED or not self._process:
                return

            self._status = ComfyUIStatus.STOPPING
            self._log("[CMV] Stopping managed ComfyUI process...")

            proc = self._process
            self._process = None

        if proc.poll() is None:
            try:
                if self._job_object is not None:
                    self._job_object.terminate()

                if proc.poll() is None:
                    self._terminate_process_tree(proc)

                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._log("[CMV] Process did not exit after tree kill, forcing...")
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
            except Exception as exc:
                self._log(f"[CMV] Error terminating process: {exc}")

        self._close_job()

        self._stop_monitor_event.set()
        if proc.stdout:
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass

        reader_thread = self._reader_thread
        monitor_thread = self._monitor_thread
        if reader_thread and reader_thread is not threading.current_thread():
            reader_thread.join(timeout=1.0)
        if monitor_thread and monitor_thread is not threading.current_thread():
            monitor_thread.join(timeout=1.0)

        with self._lock:
            self._mode = ComfyUIMode.NONE
            self._status = ComfyUIStatus.STOPPED
            self._reader_thread = None
            self._monitor_thread = None
            self._log("[CMV] Managed ComfyUI process stopped.")

    def restart_managed(
        self,
        install_path: str | Path | None = None,
        host: str | None = None,
        port: int | None = None,
        extra_args: str | Sequence[str] | None = None,
        custom_python: str | Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            target_path = install_path or (self._installation.root_path if self._installation else None)
            target_host = host or self._host
            target_port = port or self._port

        if not target_path:
            raise ValueError("No installation path available for restart.")

        self.stop_managed()
        time.sleep(0.5)
        return self.start_managed(
            install_path=target_path,
            host=target_host,
            port=target_port,
            extra_args=extra_args,
            custom_python=custom_python,
        )

    def check_external_or_status(self, host: str = "127.0.0.1", port: int = 8188) -> dict[str, Any]:
        """Poll API status. If no managed process is running, check if an external instance is running."""
        with self._lock:
            mode = self._mode
            managed_proc = self._process

        client = ComfyUIClient(host=host, port=port)
        try:
            health = client.check_health()
            _online = health.get("online", False)
            queue_info = health.get("queue_info", {})
            is_busy = queue_info.get("is_busy", False)

            with self._lock:
                if mode == ComfyUIMode.MANAGED and managed_proc and managed_proc.poll() is None:
                    self._status = ComfyUIStatus.BUSY if is_busy else ComfyUIStatus.READY
                    self._last_error = None
                elif mode != ComfyUIMode.MANAGED:
                    self._mode = ComfyUIMode.EXTERNAL
                    self._status = ComfyUIStatus.EXTERNAL
                    self._host = host
                    self._port = port
                    self._last_error = None

            return {
                "mode": self._mode.value,
                "status": self._status.value,
                "online": True,
                "is_busy": is_busy,
                "queue_info": queue_info,
                "system_stats": health.get("system_stats"),
            }
        except ComfyUIClientError as exc:
            with self._lock:
                if self._mode == ComfyUIMode.EXTERNAL:
                    self._mode = ComfyUIMode.NONE
                    self._status = ComfyUIStatus.STOPPED
                elif self._mode == ComfyUIMode.MANAGED:
                    if self._process and self._process.poll() is not None:
                        exit_code = self._process.poll()
                        self._status = ComfyUIStatus.ERROR
                        self._last_error = f"ComfyUI process exited unexpectedly with code {exit_code}"

            return {
                "mode": self._mode.value,
                "status": self._status.value,
                "online": False,
                "error": str(exc),
            }

    def interrupt_generation(self) -> bool:
        client = ComfyUIClient(host=self._host, port=self._port)
        return client.interrupt()

    def _log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        self._logs.append(line)

    def _read_stdout_loop(self, process: subprocess.Popen) -> None:
        if not process.stdout:
            return
        try:
            for line in iter(process.stdout.readline, ""):
                if not line:
                    break
                stripped = line.rstrip()
                if stripped:
                    with self._lock:
                        self._logs.append(stripped)
        except (OSError, ValueError):
            pass

    def _monitor_loop(self) -> None:
        start_time = time.time()
        ready_detected = False

        while not self._stop_monitor_event.is_set():
            with self._lock:
                proc = self._process
                mode = self._mode

            if mode != ComfyUIMode.MANAGED or not proc:
                break

            exit_code = proc.poll()
            if exit_code is not None:
                with self._lock:
                    self._status = ComfyUIStatus.ERROR
                    self._last_error = f"Managed ComfyUI process exited with code {exit_code}"
                    self._log(f"[CMV] Process exited unexpectedly with code {exit_code}")
                break

            client = ComfyUIClient(host=self._host, port=self._port)
            try:
                health = client.check_health()
                queue_info = health.get("queue_info", {})
                is_busy = queue_info.get("is_busy", False)

                with self._lock:
                    if self._mode == ComfyUIMode.MANAGED:
                        if not ready_detected:
                            ready_detected = True
                            self._log(f"[CMV] ComfyUI API is ready at http://{self._host}:{self._port}")
                        self._status = ComfyUIStatus.BUSY if is_busy else ComfyUIStatus.READY
                        self._last_error = None
            except ComfyUIClientError:
                with self._lock:
                    if self._mode == ComfyUIMode.MANAGED and not ready_detected:
                        self._status = ComfyUIStatus.STARTING
                        if time.time() - start_time > 120.0:
                            self._status = ComfyUIStatus.ERROR
                            self._last_error = "Timeout waiting for ComfyUI API readiness"
                            self._log("[CMV] Timed out waiting for ComfyUI API readiness")

            time.sleep(1.5)

    def _create_job(self) -> KillOnCloseJob | None:
        if sys.platform != "win32":
            return None
        try:
            return KillOnCloseJob()
        except OSError as exc:
            self._log(f"[CMV] Warning: ComfyUI will not be tied to CMV's lifetime: {exc}")
            return None

    def _enter_job(self, process: subprocess.Popen, job: KillOnCloseJob) -> KillOnCloseJob | None:
        """Put a suspended process into the job, then let it run. A job failure only loses the tie."""
        handle = int(process._handle)  # type: ignore[attr-defined]
        try:
            job.assign(handle)
        except OSError as exc:
            self._log(f"[CMV] Warning: ComfyUI will not be tied to CMV's lifetime: {exc}")
            job.close()
            job = None
        try:
            resume_process(handle)
        except OSError:
            process.kill()
            raise
        return job

    def _close_job(self) -> None:
        job, self._job_object = self._job_object, None
        if job is not None:
            job.close()

    def _terminate_process_tree(self, proc: subprocess.Popen) -> None:
        """Terminate the process and all its child processes."""
        if sys.platform == "win32" or os.name == "nt":
            try:
                system_root = os.environ.get("SystemRoot", "C:\\Windows")
                taskkill_path = os.path.join(system_root, "System32", "taskkill.exe")
                subprocess.run(
                    [taskkill_path, "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(proc.pid, 9)
            except (OSError, ProcessLookupError):
                pass

        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


comfy_manager = ComfyUIManager()


def resolve_comfyui_installation(store: Any | None = None) -> ComfyUIDetectionResult | None:
    """The ComfyUI to launch: the chosen ComfyUI Desktop installation, else the one Desktop used last."""
    settings = store.comfyui_settings() if store and hasattr(store, "comfyui_settings") else {}
    launch = launch_settings(settings, state_dir=state_dir_of(store))
    install_path = launch.get("install_path")
    if not install_path:
        return None
    detection = detect_comfyui(install_path, custom_python=launch.get("custom_python"))
    return detection if detection.is_valid else None


def ensure_comfyui_online(store: Any | None = None, timeout: float = 60.0) -> bool:
    """Ensure ComfyUI API is online. If offline, automatically launches a managed process and waits for readiness."""
    import logging
    log = logging.getLogger("comfy-meta-viewer.comfyui")

    settings = store.comfyui_settings() if store and hasattr(store, "comfyui_settings") else {}
    host = str(settings.get("host") or "127.0.0.1")
    port = int(settings.get("port") or 8188)
    client = ComfyUIClient(host=host, port=port, timeout=1.5)

    # 1. Quick check if already online
    try:
        health = client.check_health()
        if health.get("online"):
            log.info("[CMV] ComfyUI already online at %s:%s", host, port)
            return True
    except Exception:
        pass

    log.info("[CMV] ComfyUI offline at %s:%s, attempting auto-start...", host, port)

    # 2. If already starting or running, wait for it
    info = comfy_manager.get_info()
    if info.get("status") in ("starting", "ready", "busy") and comfy_manager.mode == ComfyUIMode.MANAGED:
        log.info("[CMV] ComfyUI managed process already in state '%s', waiting...", info["status"])
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if client.check_health().get("online"):
                    log.info("[CMV] ComfyUI became ready")
                    return True
            except Exception:
                pass
            time.sleep(1.0)
        log.warning("[CMV] Timed out waiting for existing managed ComfyUI process")
        return False

    # 3. Resolve installation directory
    installation = resolve_comfyui_installation(store)
    if not installation or not installation.is_valid:
        log.error("[CMV] Could not find valid ComfyUI installation for auto-start")
        return False

    log.info("[CMV] Found ComfyUI at %s (interpreter: %s)", installation.comfy_dir, installation.interpreter)

    launch = launch_settings(settings, state_dir=state_dir_of(store))
    extra_args = launch.get("extra_args") or ""
    custom_python = launch.get("custom_python")

    try:
        comfy_manager.start_managed(
            install_path=installation.root_path,
            host=host,
            port=port,
            extra_args=extra_args,
            custom_python=custom_python,
        )
        log.info("[CMV] ComfyUI managed process started, waiting for API readiness...")
    except Exception as exc:
        log.error("[CMV] Failed to start ComfyUI managed process: %s", exc)
        return False

    # 4. Wait for ComfyUI API to report ready
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            health = client.check_health()
            if health.get("online"):
                log.info("[CMV] ComfyUI API is now ready at %s:%s", host, port)
                return True
        except Exception:
            pass

        if comfy_manager.status == ComfyUIStatus.ERROR:
            log.error("[CMV] ComfyUI process entered ERROR state: %s", comfy_manager.get_info().get("last_error"))
            break
        time.sleep(1.0)

    try:
        result = client.check_health().get("online", False)
        if not result:
            log.error("[CMV] ComfyUI did not become ready within %.0fs timeout", timeout)
        return result
    except Exception:
        log.error("[CMV] ComfyUI still unreachable after %.0fs timeout", timeout)
        return False

