"""Live progress of Create runs from the ComfyUI WebSocket.

ComfyUI reports sampler steps (``progress``), the running node (``executing``) and the
start and end of a prompt (``execution_start`` / ``execution_success`` / ...) only to
the client id that queued the prompt. Create queues every run with the monitor's
client id; one background connection per ComfyUI address collects the events while
runs are active, and the run status route reads a snapshot: stage, sampler step,
elapsed time and an estimate of the time left. Without a connection Create falls
back to the calibrated per-profile estimate.

The connection uses a small RFC 6455 client (text frames, ping/pong, close) because
the app ships without a WebSocket library; preview images arrive as binary frames and
are skipped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
SAMPLER_CLASSES = {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"}
# What the running node means for the person waiting.
_STAGE_BY_CLASS = {
    "sampling": SAMPLER_CLASSES,
    "loading": {"UNETLoader", "CLIPLoader", "DualCLIPLoader", "VAELoader", "CheckpointLoaderSimple",
                "LoraLoader", "LoraLoaderModelOnly"},
    "encoding": {"CLIPTextEncode", "Krea2EditGroundedEncode", "ConditioningZeroOut", "VAEEncode"},
    "decoding": {"VAEDecode", "VAEDecodeTiled"},
    "saving": {"SaveImage"},
}
# Decode and save after the last step; refined by finished runs of this monitor.
DEFAULT_TAIL_SECONDS = 4.0
IDLE_DISCONNECT_SECONDS = 120.0
FINISHED_KEEP_SECONDS = 3600.0


class WebSocketClosed(ConnectionError):
    pass


class _Idle(TimeoutError):
    pass


class MiniWebSocket:
    """Just enough of a WebSocket client to read ComfyUI's JSON events."""

    def __init__(self, host: str, port: int, path: str, *, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketClosed("ComfyUI closed the WebSocket handshake")
            self.buffer += chunk
            if len(self.buffer) > 65536:
                raise WebSocketClosed("WebSocket handshake is too large")
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in f"{lines[0]} ":
            raise WebSocketClosed(f"WebSocket upgrade refused: {lines[0]}")
        headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}
        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketClosed("WebSocket accept key does not match")

    def _fill(self, size: int, *, idle_ok: bool) -> None:
        while len(self.buffer) < size:
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError as exc:
                # Partial bytes stay in the buffer, so the frame resumes on the next call.
                if idle_ok:
                    raise _Idle() from exc
                raise
            if not chunk:
                raise WebSocketClosed("ComfyUI closed the WebSocket")
            self.buffer += chunk

    def _frame(self, *, idle_ok: bool) -> tuple[bool, int, bytes]:
        self._fill(2, idle_ok=idle_ok)
        first, second = self.buffer[0], self.buffer[1]
        length = second & 0x7F
        offset = 2
        if length == 126:
            self._fill(4, idle_ok=idle_ok)
            length = struct.unpack(">H", self.buffer[2:4])[0]
            offset = 4
        elif length == 127:
            self._fill(10, idle_ok=idle_ok)
            length = struct.unpack(">Q", self.buffer[2:10])[0]
            offset = 10
        mask = b""
        if second & 0x80:
            self._fill(offset + 4, idle_ok=idle_ok)
            mask = self.buffer[offset:offset + 4]
            offset += 4
        self._fill(offset + length, idle_ok=idle_ok)
        payload = self.buffer[offset:offset + length]
        self.buffer = self.buffer[offset + length:]
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return bool(first & 0x80), first & 0x0F, payload

    def send(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        if len(payload) < 126:
            header += bytes([0x80 | len(payload)])
        elif len(payload) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(payload))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv_text(self, idle_timeout: float) -> str | None:
        """The next text message; ``None`` for a skipped binary message.

        Raises ``_Idle`` when nothing arrives within ``idle_timeout`` seconds.
        """
        self.sock.settimeout(idle_timeout)
        parts: list[bytes] = []
        opcode_of_message = None
        while True:
            fin, opcode, payload = self._frame(idle_ok=not parts)
            if opcode == 0x9:  # ping
                self.send(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:
                raise WebSocketClosed("ComfyUI closed the WebSocket")
            if opcode != 0x0:
                opcode_of_message = opcode
            parts.append(payload)
            if fin:
                break
        if opcode_of_message != 0x1:
            return None
        return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.send(0x8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _stage(node_class: str | None) -> str:
    if not node_class:
        return "working"
    for stage, classes in _STAGE_BY_CLASS.items():
        if node_class in classes:
            return stage
    return "working"


def sampler_steps(workflow: dict[str, Any]) -> tuple[int | None, dict[str, str]]:
    """Total sampler steps of an API-format workflow, plus node id -> class type."""
    classes: dict[str, str] = {}
    total = 0
    known = True
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        classes[str(node_id)] = class_type
        if class_type not in SAMPLER_CLASSES:
            continue
        inputs = node.get("inputs") or {}
        steps = inputs.get("steps")
        if not isinstance(steps, int | float):
            known = False
            continue
        if class_type == "KSamplerAdvanced":
            start = inputs.get("start_at_step", 0)
            end = inputs.get("end_at_step", steps)
            if not isinstance(start, int | float) or not isinstance(end, int | float):
                known = False
                continue
            total += max(0, int(min(end, steps)) - int(start))
        elif class_type == "KSampler":
            total += int(steps)
        else:
            known = False
    return (total if known and total > 0 else None), classes


@dataclass
class PromptProgress:
    prompt_id: str
    total_steps: int | None
    node_classes: dict[str, str]
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    outcome: str | None = None
    node: str | None = None
    # Sampler segments by node id: latest (value, max).
    segments: dict[str, tuple[int, int]] = field(default_factory=dict)
    first_step_at: float | None = None
    first_step_done: int = 0
    last_step_at: float | None = None
    last_sampling_end: float | None = None

    @property
    def steps_done(self) -> int:
        return sum(min(value, maximum) for value, maximum in self.segments.values())

    def snapshot(self, *, now: float, tail_seconds: float) -> dict[str, Any]:
        node_class = self.node_classes.get(self.node or "")
        finished = self.finished_at is not None
        state = "finished" if finished else "running" if self.started_at else "queued"
        steps_total = self.total_steps or (sum(maximum for _, maximum in self.segments.values()) or None)
        result: dict[str, Any] = {
            "state": state,
            "stage": "done" if finished else _stage(node_class) if self.started_at else "queued",
            "node_class": node_class,
            "step": self.steps_done,
            "steps": steps_total,
            "elapsed_seconds": round((self.finished_at or now) - self.started_at, 1) if self.started_at else None,
            "eta_seconds": None,
            "duration_seconds": None,
            "outcome": self.outcome,
        }
        if finished and self.started_at:
            result["duration_seconds"] = round(self.finished_at - self.started_at, 1)
            result["eta_seconds"] = 0
            return result
        done = self.steps_done
        if (
            steps_total and self.first_step_at is not None and self.last_step_at is not None
            and done > self.first_step_done and self.last_step_at > self.first_step_at
        ):
            per_step = (self.last_step_at - self.first_step_at) / (done - self.first_step_done)
            remaining_steps = max(0, steps_total - done)
            # Time already spent since the last reported step counts against the estimate.
            since_last = max(0.0, now - self.last_step_at)
            result["eta_seconds"] = max(1, round(remaining_steps * per_step + tail_seconds - since_last))
        return result


class ProgressMonitor:
    """One ComfyUI address: a client id, tracked prompts and a reader thread."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = int(port)
        self.client_id = f"cmv-create-{uuid.uuid4().hex}"
        self._prompts: dict[str, PromptProgress] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._tail_seconds = DEFAULT_TAIL_SECONDS
        self._last_activity = time.monotonic()

    # --- public ---------------------------------------------------------------------

    def ensure_connected(self, timeout: float = 2.0) -> bool:
        """Start the reader (if needed) and wait briefly until it is connected."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._connected.clear()
                self._thread = threading.Thread(target=self._run, name="cmv-comfy-progress", daemon=True)
                self._last_activity = time.monotonic()
                self._thread.start()
        return self._connected.wait(timeout)

    def track(self, prompt_id: str, workflow: dict[str, Any]) -> None:
        total, classes = sampler_steps(workflow)
        with self._lock:
            self._prune(time.time())
            self._prompts[prompt_id] = PromptProgress(prompt_id=prompt_id, total_steps=total, node_classes=classes)
            self._last_activity = time.monotonic()

    def snapshot(self, prompt_id: str) -> dict[str, Any] | None:
        with self._lock:
            progress = self._prompts.get(prompt_id)
            if progress is None:
                return None
            return progress.snapshot(now=time.time(), tail_seconds=self._tail_seconds)

    # --- events ---------------------------------------------------------------------

    def handle(self, message: dict[str, Any], *, now: float | None = None) -> None:
        """Apply one ComfyUI event (public for tests)."""
        now = time.time() if now is None else now
        kind = message.get("type")
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        prompt_id = data.get("prompt_id")
        with self._lock:
            progress = self._prompts.get(str(prompt_id)) if prompt_id else None
            if progress is None:
                return
            self._last_activity = time.monotonic()
            if kind == "execution_start":
                progress.started_at = progress.started_at or now
            elif kind == "executing":
                progress.started_at = progress.started_at or now
                node = data.get("node")
                if node is None:
                    self._finish(progress, now, progress.outcome or "success")
                else:
                    if progress.node_classes.get(progress.node or "") in SAMPLER_CLASSES:
                        progress.last_sampling_end = now
                    progress.node = str(node)
            elif kind == "progress":
                self._step(progress, data, now)
            elif kind == "execution_success":
                self._finish(progress, now, "success")
            elif kind in {"execution_error", "execution_interrupted"}:
                self._finish(progress, now, "error" if kind == "execution_error" else "interrupted")

    def _step(self, progress: PromptProgress, data: dict[str, Any], now: float) -> None:
        node = str(data.get("node") or progress.node or "")
        node_class = progress.node_classes.get(node)
        if node_class is not None and node_class not in SAMPLER_CLASSES:
            return  # tiled decode, upscale and other progress bars are not sampler steps
        value, maximum = data.get("value"), data.get("max")
        if not isinstance(value, int | float) or not isinstance(maximum, int | float):
            return
        progress.started_at = progress.started_at or now
        progress.node = node or progress.node
        progress.segments[node] = (int(value), int(maximum))
        if progress.first_step_at is None:
            progress.first_step_at = now
            progress.first_step_done = progress.steps_done
        progress.last_step_at = now

    def _finish(self, progress: PromptProgress, now: float, outcome: str) -> None:
        if progress.finished_at is not None:
            return
        progress.finished_at = now
        progress.outcome = outcome
        if outcome == "success" and progress.last_step_at is not None:
            tail = now - max(progress.last_step_at, progress.last_sampling_end or 0.0)
            if 0 < tail < 120:
                self._tail_seconds = round(0.5 * self._tail_seconds + 0.5 * tail, 2)

    def _prune(self, now: float) -> None:
        for prompt_id in [
            key for key, item in self._prompts.items()
            if (item.finished_at and now - item.finished_at > FINISHED_KEEP_SECONDS)
            or now - item.queued_at > 6 * FINISHED_KEEP_SECONDS
        ]:
            del self._prompts[prompt_id]

    def _has_active(self) -> bool:
        with self._lock:
            return any(item.finished_at is None for item in self._prompts.values())

    # --- reader thread --------------------------------------------------------------

    def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                ws = MiniWebSocket(self.host, self.port, f"/ws?clientId={self.client_id}")
            except (OSError, WebSocketClosed) as exc:
                self._connected.clear()
                logger.debug("ComfyUI progress socket unavailable: %s", exc)
                if not self._has_active() and time.monotonic() - self._last_activity > IDLE_DISCONNECT_SECONDS:
                    return
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            backoff = 1.0
            self._connected.set()
            try:
                while True:
                    try:
                        text = ws.recv_text(idle_timeout=10.0)
                    except _Idle:
                        if not self._has_active() and time.monotonic() - self._last_activity > IDLE_DISCONNECT_SECONDS:
                            return
                        continue
                    if not text:
                        continue
                    try:
                        message = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict):
                        self.handle(message)
            except (OSError, WebSocketClosed) as exc:
                logger.debug("ComfyUI progress socket dropped: %s", exc)
            finally:
                self._connected.clear()
                ws.close()


_monitors: dict[tuple[str, int], ProgressMonitor] = {}
_monitors_lock = threading.Lock()


def monitor_for(host: str, port: int) -> ProgressMonitor:
    with _monitors_lock:
        key = (str(host), int(port))
        if key not in _monitors:
            _monitors[key] = ProgressMonitor(*key)
        return _monitors[key]


__all__ = [
    "MiniWebSocket",
    "ProgressMonitor",
    "PromptProgress",
    "monitor_for",
    "sampler_steps",
]
