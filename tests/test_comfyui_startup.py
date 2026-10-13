import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from app.comfyui import manager, simple_routes


def test_retry_waits_for_live_comfyui_even_after_readiness_timeout(monkeypatch):
    managed = manager.ComfyUIManager()
    managed._mode = manager.ComfyUIMode.MANAGED
    managed._status = manager.ComfyUIStatus.ERROR
    managed._last_error = "Timeout waiting for ComfyUI API readiness"
    managed._process = Mock(pid=42)
    managed._process.poll.return_value = None
    managed.start_managed = Mock(side_effect=RuntimeError("already running"))
    monkeypatch.setattr(manager, "comfy_manager", managed)
    client = Mock()
    client.check_health.side_effect = [manager.ComfyUIClientError("offline"), {"online": True}]
    monkeypatch.setattr(manager, "ComfyUIClient", lambda **kwargs: client)
    monkeypatch.setattr(manager, "resolve_comfyui_installation", lambda store: Mock(is_valid=True))
    monkeypatch.setattr(manager, "launch_settings", lambda *args, **kwargs: {})
    assert manager.ensure_comfyui_online(timeout=1)
    managed.start_managed.assert_not_called()


def test_local_startup_error_exposes_process_diagnostics(monkeypatch):
    managed = Mock()
    managed.get_info.return_value = {"last_error": "Process exited with code 1"}
    managed.get_logs.return_value = ["ModuleNotFoundError: missing_node"]
    managed.managed_process_alive = False
    monkeypatch.setattr(manager, "comfy_manager", managed)
    payload = simple_routes._LocalBackend().not_ready_payload()
    assert "Process exited with code 1" in payload["technical_error"]
    assert "missing_node" in payload["technical_error"]
    assert "path in settings" not in payload["suggestion"]


def test_concurrent_autostarts_share_one_managed_process(monkeypatch):
    managed = manager.ComfyUIManager()
    monkeypatch.setattr(manager, "comfy_manager", managed)
    both_offline = threading.Barrier(2)

    class Client:
        first_check = True

        def check_health(self):
            if self.first_check:
                self.first_check = False
                both_offline.wait(timeout=2)
                raise manager.ComfyUIClientError("offline")
            return {"online": True}

    def start(**kwargs):
        managed._mode = manager.ComfyUIMode.MANAGED
        managed._process = Mock(pid=42)
        managed._process.poll.return_value = None

    managed.start_managed = Mock(side_effect=start)
    monkeypatch.setattr(manager, "ComfyUIClient", lambda **kwargs: Client())
    monkeypatch.setattr(manager, "resolve_comfyui_installation", lambda store: Mock(is_valid=True))
    monkeypatch.setattr(manager, "launch_settings", lambda *args, **kwargs: {})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manager.ensure_comfyui_online, timeout=1)
        second = pool.submit(manager.ensure_comfyui_online, timeout=1)
        assert first.result(timeout=3)
        assert second.result(timeout=3)
    assert managed.start_managed.call_count == 1
