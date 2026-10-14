"""Read native CLI model catalogs without sending a user prompt or starting inference."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from . import catalog_cache
from .cli import (
    CLIIntegrationError,
    _subprocess_options,
    _terminate_process_tree,
    find_executable,
    sanitize_output,
)

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_.:-]{0,199}")
_LEVEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
_cache = catalog_cache.memory
_locks = {host: threading.Lock() for host in ("codex", "claude")}


def family_vision(model: str) -> bool | None:
    """Explicit fallback for known families; future/unknown families stay unknown."""
    name = model.casefold().split("/")[-1]
    if re.match(r"gpt-oss(?:-|$)", name):
        return False
    if name.startswith(("gemini-", "claude-")) or name in {"sonnet", "opus", "haiku"}:
        return True
    return None


def model_vision(item: dict[str, Any], model: str) -> tuple[bool | None, str]:
    modalities = item.get("inputModalities")
    if isinstance(modalities, list) and all(isinstance(value, str) for value in modalities):
        return "image" in modalities, "model"
    inputs = (item.get("capabilities") or {}).get("input") if isinstance(item.get("capabilities"), dict) else None
    if isinstance(inputs, dict) and isinstance(inputs.get("image"), bool):
        return inputs["image"], "model"
    vision = family_vision(model)
    return vision, "family" if vision is not None else "unknown"


def parse_native_models(host: str, items: Any) -> list[dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {}
    if not isinstance(items, list):
        raise CLIIntegrationError(f"{host} returned an invalid model catalog.")
    for item in items:
        if not isinstance(item, dict) or item.get("hidden") is True:
            continue
        model = item.get("model") or item.get("value") or item.get("id")
        if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
            continue
        vision, source = model_vision(item, model)
        # Claude's 'default' alias names the native family in its description.
        if host == "claude" and model == "default" and vision is None:
            if re.match(r"(?:Opus|Sonnet|Haiku)\b", str(item.get("description") or "")):
                vision, source = True, "family"
        variants = item.get("supportedEffortLevels") or []
        if host == "codex":
            variants = [level.get("reasoningEffort") for level in item.get("supportedReasoningEfforts") or []
                        if isinstance(level, dict)]
        if not isinstance(variants, list):
            variants = []
        variants = list(dict.fromkeys(value for value in variants if isinstance(value, str) and _LEVEL.fullmatch(value)))
        models[model] = {
            "id": model, "name": item.get("displayName") or model,
            "vision": vision, "vision_source": source, "variants": variants,
            "default_variant": item.get("defaultReasoningEffort"),
        }
    return list(models.values())


class _Session:
    def __init__(self, process: subprocess.Popen, timeout: float):
        self.process = process
        self.deadline = time.monotonic() + timeout
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.errors: list[str] = []
        self.readers = [
            threading.Thread(target=self._read, args=(process.stdout, False), daemon=True),
            threading.Thread(target=self._read, args=(process.stderr, True), daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def _read(self, stream, stderr: bool):
        size = 0
        try:
            for line in stream:
                size += len(line)
                if size > 4_000_000:
                    break
                if stderr:
                    if len(self.errors) < 20:
                        self.errors.append(line[:1000])
                else:
                    self.messages.put(line)
        finally:
            if not stderr:
                self.messages.put(None)

    def send(self, message: dict[str, Any]):
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CLIIntegrationError("The CLI closed its model catalog connection.") from exc

    def receive(self, matches: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise CLIIntegrationError("The CLI model catalog timed out.", code="timeout")
            try:
                line = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise CLIIntegrationError("The CLI model catalog timed out.", code="timeout") from exc
            if line is None:
                detail = sanitize_output("".join(self.errors), maximum=500)
                raise CLIIntegrationError(detail or "The CLI closed its model catalog connection.")
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and matches(value):
                return value


@contextmanager
def _catalog_session(args: list[str], *, timeout: float = 30):
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env["NO_COLOR"] = "1"
    with tempfile.TemporaryDirectory(prefix="mediastudio-models-", ignore_cleanup_errors=True) as work:
        try:
            process = subprocess.Popen(
                args, cwd=work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", **_subprocess_options(),
            )
        except OSError as exc:
            raise CLIIntegrationError("The CLI model catalog could not start.") from exc
        session = _Session(process, timeout)
        try:
            yield session
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
                process.wait(timeout=5)
            for reader in session.readers:
                reader.join(timeout=1)
            process.stdout.close()
            process.stderr.close()


def _codex_models(executable: str) -> list[dict[str, Any]]:
    with _catalog_session([executable, "app-server"]) as session:
        session.send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "mediastudio", "title": "MediaStudio", "version": "1.0"},
        }})
        reply = session.receive(lambda value: value.get("id") == 1)
        if "error" in reply:
            raise CLIIntegrationError("Codex could not initialize its model catalog.")
        session.send({"method": "initialized", "params": {}})
        items: list[dict[str, Any]] = []
        cursor = None
        seen_cursors: set[str] = set()
        for request_id in range(2, 22):
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            session.send({"id": request_id, "method": "model/list", "params": params})
            reply = session.receive(lambda value, expected=request_id: value.get("id") == expected)
            result = reply.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise CLIIntegrationError("Codex could not list its models.")
            items.extend(result["data"])
            cursor = result.get("nextCursor")
            if not cursor:
                return parse_native_models("codex", items)
            if not isinstance(cursor, str) or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        raise CLIIntegrationError("Codex returned incomplete model catalog pages.")


def _claude_models(executable: str) -> list[dict[str, Any]]:
    args = [executable, "--print", "--verbose", "--input-format", "stream-json", "--output-format", "stream-json",
            "--no-session-persistence", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", '{"disableAllHooks":true}', "--disable-slash-commands"]
    with _catalog_session(args) as session:
        session.send({"type": "control_request", "request_id": "models", "request": {"subtype": "initialize"}})
        reply = session.receive(lambda value: value.get("type") == "control_response"
                                and (value.get("response") or {}).get("request_id") == "models")
        response = reply["response"]
        if response.get("subtype") != "success":
            raise CLIIntegrationError("Claude Code could not initialize its model catalog.")
        return parse_native_models("claude", (response.get("response") or {}).get("models"))


def native_cli_models(host: str, *, refresh: bool = False) -> tuple[str | None, list[dict[str, Any]]]:
    executable = find_executable(host)
    if executable is None:
        return None, []
    with _locks[host]:
        cached = catalog_cache.load(host, executable) if not refresh else None
        if cached:
            return executable, cached
        models = _codex_models(executable) if host == "codex" else _claude_models(executable)
        if not models:
            raise CLIIntegrationError(f"{host} returned no readable model catalog.")
        catalog_cache.save(host, executable, models)
        return executable, models
