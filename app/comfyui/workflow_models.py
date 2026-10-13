from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.ai.prompting.models import StrictModel


class RuntimeInventory(StrictModel):
    online: bool = False
    error: str | None = None
    node_types: list[str] = Field(default_factory=list)
    models: dict[str, list[str]] = Field(default_factory=dict)
    source: Literal["api", "filesystem", "none"] = "none"


class WorkflowDraft(StrictModel):
    id: int
    template_id: str
    template_version: str
    values: dict[str, Any]
    resource_selections: dict[str, Any]
    source_asset_id: int | None = None
    ai_prompt_draft_id: int | None = None
    auto_rate: bool = False
    status: Literal["editing", "queued", "completed", "failed", "cancelled"]
    created_at: str
    updated_at: str


class WorkflowRun(StrictModel):
    id: int
    draft_id: int
    prompt_id: str
    client_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    auto_rate: bool = False
    progress: float | None = None
    queue_position: int | None = None
    current_node: str | None = None
    error: dict[str, Any] | None = None
    output_refs: list[dict[str, Any]] = Field(default_factory=list)
    output_asset_ids: list[int] = Field(default_factory=list)
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None



__all__ = ["RuntimeInventory", "WorkflowDraft", "WorkflowRun"]
