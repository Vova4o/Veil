"""A persisted Create task owns agent -> queue -> import, independent of its page.

The coordinator lives in workflow_drafts; actual generation still uses the existing
workflow runs. Request IDs are idempotent so a lost browser receipt cannot duplicate a batch.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from flask import Flask, make_response

from app import database as db

from .create_agent import CreateAgentError
from .create_outputs import ensure_output_source
from .create_sessions import agent_history, touch_session
from .manager import COMFYUI_STARTUP_TIMEOUT_SECONDS
from .workflow_execution import WorkflowExecutionService
from .workflow_store import WorkflowStore

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled"}


class CreateTaskService:
    def __init__(self, app: Flask):
        self.app = app
        self.store = WorkflowStore()
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._runtime_threads: dict[str, threading.Thread] = {}

    def _draft(self, task_id: str):
        conn = db.get_conn()
        try:
            row = conn.execute(
                """SELECT id FROM workflow_drafts WHERE template_id='create-task'
                AND json_extract(resource_selections_json, '$.create_task_id')=?""", (task_id,),
            ).fetchone()
        finally:
            conn.close()
        return self.store.get_draft(int(row["id"])) if row else None

    def start(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        from . import simple_routes as routes
        with self._lock:
            draft = self._draft(task_id)
            if draft:
                if draft.resource_selections["request"] != payload:
                    raise ValueError("This task ID already belongs to another request.")
                return self.snapshot(task_id)
            backend = routes._backend().kind
            session_id = payload.get("session_id")
            selections = {"create_task_id": task_id, "request": payload, "backend": backend}
            if session_id:
                selections["session_id"] = session_id
            self.store.create_draft(
                template_id="create-task", template_version="1.0.0",
                resource_selections=selections,
                values={"phase": "agent" if payload["use_agent"] else "queue", "run_ids": [], "runs": []},
            )
            if session_id:
                touch_session(session_id, request=payload.get("request") or "", mode=payload.get("mode"))
            self._update(task_id, status="queued")
            self._spawn(task_id, self._execute)
            return self.snapshot(task_id)

    def _spawn(self, task_id: str, target) -> None:
        thread = threading.Thread(target=self._worker, args=(task_id, target), name=f"mediastudio-create-{task_id}", daemon=True)
        self._threads[task_id] = thread
        thread.start()

    def _worker(self, task_id: str, target) -> None:
        with self.app.app_context():
            try:
                target(task_id)
            except CreateAgentError as exc:
                from .simple_routes import _agent_error_payload
                self._update(task_id, status="failed", error={**_agent_error_payload(exc), "status": exc.status})
            except Exception as exc:
                logger.exception("Create task %s failed", task_id)
                self._update(task_id, status="failed", error={
                    "error": "The Create task failed.", "code": "create_failed", "technical_error": str(exc)[:4000],
                })

    def _update(self, task_id: str, *, status: str | None = None, even_cancelled=False, **changes) -> None:
        # Cancellation and progress callbacks merge against the current record under one lock.
        with self._lock:
            draft = self._draft(task_id)
            if draft is None or (draft.status in TERMINAL and not even_cancelled):
                return
            self.store.update_draft(draft.id, values={**draft.values, **changes}, status=status)

    def _cancelled(self, task_id: str) -> bool:
        draft = self._draft(task_id)
        return draft is None or draft.status == "cancelled"

    def _progress(self, task_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            draft = self._draft(task_id)
            if draft is None or draft.status in TERMINAL:
                return
            progress = {**draft.values.get("agent_progress", {}), **event}
            if event.get("log"):
                progress["logs"] = [*progress.get("logs", []), event["log"]][-30:]
            self._update(task_id, agent_progress=progress)

    def _execute(self, task_id: str) -> None:
        from . import simple_routes as routes
        draft = self._draft(task_id)
        if draft is None:
            return
        payload = draft.resource_selections["request"]
        backend = routes._backend(draft.resource_selections["backend"])
        ensure_output_source(routes._config_store())
        if self._cancelled(task_id):
            return
        if payload["use_agent"]:
            kwargs, finish = routes._prepare_agent_request(routes.SimpleAgentRequest.model_validate(payload))
            # Local startup and prompt preparation are independent. The prompt survives
            # a startup failure; an agent failure is reported without waiting for startup.
            readiness = self._prepare_local_runtime(task_id, backend) if backend.kind == "local" else None
            # Earlier runs of the same session give the agent the thread of the conversation.
            kwargs["session_history"] = agent_history(draft.resource_selections.get("session_id"), before_task=task_id)
            result = routes.run_create_agent(**kwargs, on_progress=lambda event: self._progress(task_id, event))
            plan = finish(result)
        else:
            readiness = None
            plan = {
                "positive_prompt": payload.get("prompt") or payload["request"],
                "negative_prompt": payload.get("negative_prompt") or "",
                "edit_profile": payload.get("edit_profile"),
                "agent": {"name": payload["agent_name"]} if payload.get("agent_name") else None,
                "note": "",
            }
        if self._cancelled(task_id):
            return
        self._update(task_id, phase="queue", plan=plan)
        if readiness is not None:
            thread, outcome = readiness
            while thread.is_alive():
                if self._cancelled(task_id):
                    return
                thread.join(timeout=0.1)
            if self._cancelled(task_id):
                return
            if not outcome["ready"]:
                self._update(task_id, status="failed", error={**outcome["error"], "status": 503})
                return

        def queued(run):
            current = self._draft(task_id)
            ids = [*current.values.get("run_ids", []), run["run_id"]]
            # Even when Stop races with a queue receipt, retain and cancel the actual run.
            self._update(task_id, even_cancelled=True, run_ids=ids)

        req = routes.SimpleGenerateRequest.model_validate({
            **payload, "prompt": plan["positive_prompt"], "user_request": payload["request"],
            "negative_prompt": plan.get("negative_prompt") if payload["mode"] == "edit" else None,
            "edit_profile": plan.get("edit_profile") if payload["mode"] == "edit" else None,
            "agent_name": (plan.get("agent") or {}).get("name"),
        })
        profile = routes.APPROVED_PROFILES[req.profile_id or routes._default_profile_id()]
        response = make_response(routes._generate_fixed(
            profile, req, backend=backend, on_queued=queued, cancelled=lambda: self._cancelled(task_id),
            runtime_ready=readiness is not None,
        ))
        result = response.get_json() or {}
        if self._cancelled(task_id):
            self._cancel_runs(task_id)
            return
        if response.status_code >= 400:
            self._update(task_id, status="failed", error={**result, "status": response.status_code})
            return
        self._update(task_id, phase="running", queue_result=result)
        self._monitor(task_id, backend=backend)

    def _prepare_local_runtime(self, task_id: str, backend):
        outcome: dict[str, Any] = {"ready": False}
        started_at = time.time()
        self._update(task_id, runtime_progress={"state": "starting", "started_at": started_at})

        def prepare():
            with self.app.app_context():
                try:
                    outcome["ready"] = backend.ensure_ready(timeout=COMFYUI_STARTUP_TIMEOUT_SECONDS)
                except Exception as exc:
                    logger.exception("Create task %s runtime startup failed", task_id)
                    outcome["error"] = {**backend.not_ready_payload(), "technical_error": str(exc)[:8000]}
                if not outcome["ready"] and "error" not in outcome:
                    outcome["error"] = backend.not_ready_payload()
                self._update(task_id, runtime_progress={
                    "state": "ready" if outcome["ready"] else "failed",
                    "started_at": started_at,
                })

        thread = threading.Thread(target=prepare, name=f"mediastudio-create-runtime-{task_id}", daemon=True)
        with self._lock:
            self._runtime_threads[task_id] = thread
            thread.start()
        return thread, outcome

    def _monitor(self, task_id: str, *, backend=None) -> None:
        from . import simple_routes as routes
        draft = self._draft(task_id)
        if draft is None:
            return
        backend = backend or routes._backend(draft.resource_selections["backend"])
        client = backend.client()
        service = WorkflowExecutionService(store=self.store, client=client)
        while not self._cancelled(task_id):
            draft = self._draft(task_id)
            ids = draft.values["run_ids"]
            runs = []
            for run_id in ids:
                try:
                    run = service.refresh(run_id)
                except Exception:
                    logger.debug("Create run %s could not refresh; retrying", run_id, exc_info=True)
                    run = self.store.get_run(run_id)
                outputs = [asset for asset_id in run.output_asset_ids
                           if (asset := routes._output_asset_payload(asset_id)) is not None]
                monitor = backend.monitor(client)
                runs.append({
                    "run": run.model_dump(mode="json"), "status": run.status, "outputs": outputs,
                    "is_complete": run.status in TERMINAL,
                    "live": monitor.snapshot(run.prompt_id) if monitor else None,
                    "remote": backend.run_details(run.prompt_id),
                })
            done = bool(runs) and all(run["is_complete"] for run in runs)
            status = None
            if done:
                status = "completed" if any(run["outputs"] for run in runs) else "failed"
            self._update(task_id, status=status, phase="running", runs=runs)
            if done:
                return
            time.sleep(1)

    def snapshot(self, task_id: str, *, include_request=False) -> dict[str, Any]:
        with self._lock:
            draft = self._draft(task_id)
            if draft is None:
                raise KeyError(task_id)
            thread = self._threads.get(task_id)
            if draft.status not in TERMINAL and (thread is None or not thread.is_alive()):
                # Never resubmit an agent or an ambiguous queue request after a server restart.
                if draft.values.get("run_ids"):
                    self._spawn(task_id, self._monitor)
                else:
                    self._update(task_id, status="failed", error={
                        "error": "The app stopped before this task reached generation. Start it again.",
                        "code": "create_interrupted",
                    })
                    draft = self._draft(task_id)
            result = {"task_id": task_id, **draft.values, "status": draft.status,
                      "backend": draft.resource_selections["backend"], "created_at": draft.created_at,
                      "is_complete": draft.status in TERMINAL}
            if draft.status == "queued" and result.get("phase") == "running":
                result["status"] = "running"
            result["outputs"] = [output for run in result.get("runs", []) for output in run["outputs"]]
            if include_request:
                result["request"] = draft.resource_selections["request"]
            return result

    def _cancel_runs(self, task_id: str) -> None:
        from .simple_routes import _backend
        draft = self._draft(task_id)
        backend = _backend(draft.resource_selections["backend"])
        service = WorkflowExecutionService(store=self.store, client=backend.client())
        for run_id in draft.values.get("run_ids", []):
            try:
                service.cancel(run_id)
            except Exception:
                logger.warning("Could not cancel Create run %s", run_id, exc_info=True)

    def cancel(self, task_id: str) -> dict[str, Any]:
        self.snapshot(task_id)
        self._update(task_id, status="cancelled")
        self._cancel_runs(task_id)
        return self.snapshot(task_id)

    def wait(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        for thread in [*self._threads.values(), *self._runtime_threads.values()]:
            thread.join(max(0, deadline - time.monotonic()))
