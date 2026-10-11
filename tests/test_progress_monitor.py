from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading

import pytest

from app.comfyui.progress_monitor import (
    MiniWebSocket,
    ProgressMonitor,
    WebSocketClosed,
    sampler_steps,
)

WORKFLOW = {
    "1": {"class_type": "UNETLoader", "inputs": {}},
    "2": {"class_type": "KSamplerAdvanced", "inputs": {"steps": 8, "start_at_step": 0, "end_at_step": 2}},
    "3": {"class_type": "KSamplerAdvanced", "inputs": {"steps": 8, "start_at_step": 2, "end_at_step": 10000}},
    "4": {"class_type": "VAEDecodeTiled", "inputs": {}},
    "5": {"class_type": "SaveImage", "inputs": {}},
}


def test_sampler_steps_follow_split_samplers():
    total, classes = sampler_steps(WORKFLOW)
    assert total == 8
    assert classes["4"] == "VAEDecodeTiled"
    assert sampler_steps({"1": {"class_type": "KSampler", "inputs": {"steps": 10}}})[0] == 10
    assert sampler_steps({"1": {"class_type": "SamplerCustom", "inputs": {}}})[0] is None


def _event(kind, **data):
    return {"type": kind, "data": {"prompt_id": "p1", **data}}


def test_snapshot_reports_stage_steps_time_left_and_duration():
    monitor = ProgressMonitor("127.0.0.1", 1)
    monitor.track("p1", WORKFLOW)
    assert monitor.snapshot("p1")["state"] == "queued"
    monitor.handle(_event("execution_start"), now=100.0)
    monitor.handle(_event("executing", node="1"), now=100.0)
    assert monitor.snapshot("p1")["stage"] == "loading"
    monitor.handle(_event("executing", node="2"), now=110.0)
    monitor.handle(_event("progress", node="2", value=1, max=2), now=115.0)
    monitor.handle(_event("progress", node="2", value=2, max=2), now=120.0)
    monitor.handle(_event("executing", node="3"), now=120.5)
    monitor.handle(_event("progress", node="3", value=1, max=6), now=125.0)
    # A tiled decode progress bar is not a sampler step.
    monitor.handle(_event("progress", node="4", value=3, max=9), now=125.0)
    with monitor._lock:
        snap = monitor._prompts["p1"].snapshot(now=125.0, tail_seconds=4.0)
    assert snap["state"] == "running" and snap["stage"] == "sampling"
    assert (snap["step"], snap["steps"]) == (3, 8)
    assert snap["elapsed_seconds"] == 25.0
    # Two steps after the first took 10 s: 5 steps left at 5 s each plus the tail.
    assert snap["eta_seconds"] == 29
    monitor.handle(_event("executing", node="4"), now=150.0)
    monitor.handle(_event("execution_success"), now=154.0)
    done = monitor.snapshot("p1")
    assert done["state"] == "finished" and done["duration_seconds"] == 54.0 and done["outcome"] == "success"
    # Events of other prompts are ignored.
    monitor.handle({"type": "progress", "data": {"prompt_id": "other", "value": 1, "max": 2}})
    assert monitor.snapshot("other") is None


def _serve(frames: list[bytes], *, accept_ok: bool = True):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    received: list[bytes] = []

    def run():
        conn, _ = server.accept()
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            key = next(
                line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key")
            )
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            if not accept_ok:
                accept = b"wrong"
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            for frame in frames:
                conn.sendall(frame)
            conn.settimeout(2)
            try:
                received.append(conn.recv(4096))
            except OSError:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return server.getsockname()[1], received, thread


def _frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    head = bytes([(0x80 if fin else 0) | opcode])
    if len(payload) < 126:
        return head + bytes([len(payload)]) + payload
    return head + bytes([126]) + struct.pack(">H", len(payload)) + payload


def test_mini_websocket_reads_text_skips_binary_and_answers_ping():
    big = json.dumps({"type": "status", "data": {"pad": "x" * 300}}).encode()
    frames = [
        _frame(0x9, b"hi"),
        _frame(0x2, b"\x00\x01preview"),
        _frame(0x1, big),
        _frame(0x1, b'{"type": "pro', fin=False),
        _frame(0x0, b'gress"}'),
        _frame(0x8, b""),
    ]
    port, received, thread = _serve(frames)
    ws = MiniWebSocket("127.0.0.1", port, "/ws?clientId=t")
    assert ws.recv_text(idle_timeout=2) is None  # binary preview
    assert json.loads(ws.recv_text(idle_timeout=2))["data"]["pad"] == "x" * 300
    assert ws.recv_text(idle_timeout=2) == '{"type": "progress"}'
    with pytest.raises(WebSocketClosed):
        ws.recv_text(idle_timeout=2)
    ws.close()
    thread.join(timeout=3)
    # The ping was answered with a masked pong carrying the same payload.
    pong = received[0] if received else b""
    assert pong[:1] == b"\x8a" and pong[1] & 0x80
    mask = pong[2:6]
    assert bytes(byte ^ mask[index % 4] for index, byte in enumerate(pong[6:8])) == b"hi"


def test_mini_websocket_rejects_a_bad_accept_key():
    port, _, thread = _serve([], accept_ok=False)
    with pytest.raises(WebSocketClosed):
        MiniWebSocket("127.0.0.1", port, "/ws")
    thread.join(timeout=3)
