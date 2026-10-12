from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

from app.comfyui import routes
from app.main import app

REPO = Path(__file__).resolve().parents[1]

# Starts a managed "ComfyUI", reports the interpreter's PID, then crashes on request:
# os._exit skips atexit handlers and stop_managed, as a killed or crashed CMV would.
PARENT = """
import os, sys, time
from pathlib import Path
from app.comfyui.manager import ComfyUIManager

base = Path(sys.argv[1])
manager = ComfyUIManager()
manager._monitor_loop = lambda: None
manager.start_managed(install_path=base, custom_python=sys.executable)
pid_file = base / "pid.txt"
deadline = time.monotonic() + 30
while not pid_file.exists() and time.monotonic() < deadline:
    time.sleep(0.05)
print(pid_file.read_text(), flush=True)
sys.stdin.readline()
os._exit(3)
"""

# Behind a venv launcher this runs in a grandchild process; its PID is the real interpreter's.
COMFY_MAIN = """
import os, time
from pathlib import Path

here = Path(__file__).parent
(here / "pid.tmp").write_text(str(os.getpid()))
os.replace(here / "pid.tmp", here / "pid.txt")
time.sleep(120)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Job objects are Windows-only")
def test_managed_comfyui_dies_with_a_crashed_parent(tmp_path):
    (tmp_path / "main.py").write_text(COMFY_MAIN, encoding="utf-8")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    parent = subprocess.Popen(
        [sys.executable, "-c", PARENT, str(tmp_path)],
        cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        pid = int(parent.stdout.readline())
        # Holding a handle pins the process identity, so the wait cannot hit a reused PID.
        handle = kernel32.OpenProcess(0x00100000 | 0x0001, False, pid)  # SYNCHRONIZE | PROCESS_TERMINATE
        assert handle, "the managed process is not running"
        try:
            parent.stdin.write("crash\n")
            parent.stdin.flush()
            assert parent.wait(timeout=15) == 3
            assert kernel32.WaitForSingleObject(handle, 10_000) == 0, "ComfyUI outlived the crashed parent"
        finally:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.stdout.close()
        parent.stdin.close()


def test_start_route_does_not_start_a_second_comfyui(monkeypatch):
    started = []
    monkeypatch.setattr(
        routes.comfy_manager, "check_external_or_status",
        lambda host, port: {"mode": "external", "status": "external", "online": True},
    )
    monkeypatch.setattr(routes.comfy_manager, "start_managed", lambda **kwargs: started.append(kwargs))

    resp = app.test_client().post("/api/comfyui/start", json={"install_path": str(REPO)})

    assert resp.status_code == 200
    assert resp.get_json()["online"] is True
    assert started == []
