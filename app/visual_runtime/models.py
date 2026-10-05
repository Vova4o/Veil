"""Typed Visual Runtime contracts (EDITOR-STUDIO-RUNTIME-PLAN §4, §5, §7, §14).

Pydantic models describe visual intent and execution policy, never raw
ComfyUI graphs. The workflow layer maps these contracts into node inputs.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.ai.prompting.models import StrictModel


class ReferenceRole(str, Enum):
    CHARACTER = "character"
    ENVIRONMENT = "environment"
    STYLE = "style"
    TYPOGRAPHY = "typography"


class VisualOperation(str, Enum):
    GENERATE_SCENE = "generate_scene"
    COMPOSE_SCENE = "compose_scene"
    EDIT_SCENE = "edit_scene"
    ADD_CHARACTER = "add_character"
    REPLACE_CHARACTER = "replace_character"
    CHANGE_POSE = "change_pose"
    CHANGE_WARDROBE = "change_wardrobe"
    RELOCATE = "relocate"
    REPLACE_BACKGROUND = "replace_background"
    ADJUST_LIGHTING = "adjust_lighting"
    TYPOGRAPHY = "typography"
    POSTERIZE = "posterize"
    FINALIZE = "finalize"


class ExecutionOrigin(str, Enum):
    RUNTIME = "runtime"
    MCP_FREE = "mcp_free"


class StudioPrimitive(str, Enum):
    MOODY_GENERATE = "moody-generate"
    MOODY_EDIT = "moody-edit"


class VisualReference(StrictModel):
    asset_id: int = Field(ge=1)
    role: ReferenceRole
    label: str | None = Field(default=None, max_length=160)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class VisualSession(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    original_asset_id: int | None = Field(default=None, ge=1)
    current_asset_id: int | None = Field(default=None, ge=1)
    references: tuple[VisualReference, ...] = ()
    instruction: str = Field(default="", max_length=20_000)

    @field_validator("id", "instruction")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class EditPolicy(StrictModel):
    preserve_identity: bool = True
    preserve_pose: bool = False
    preserve_environment: bool = False
    reference_strength: float = Field(default=1.0, ge=0.0, le=2.0)
    grounding_resolution: int = Field(default=1024, ge=256, le=2048)
    protected_regions: tuple[str, ...] = ()

    @field_validator("protected_regions", mode="before")
    @classmethod
    def normalize_regions(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, str):
            cleaned = value.strip()
            return (cleaned,) if cleaned else ()
        return value


class VisualAction(StrictModel):
    id: int = Field(ge=1)
    session_id: str = Field(min_length=1, max_length=64)
    operation: VisualOperation
    instruction: str = Field(default="", max_length=20_000)
    origin: ExecutionOrigin = ExecutionOrigin.RUNTIME
    input_asset_ids: tuple[int, ...] = ()
    output_asset_ids: tuple[int, ...] = ()
    workflow_run_id: int | None = Field(default=None, ge=1)
    created_at: str = ""


class ExecutionPlan(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    operation: VisualOperation
    primitive: StudioPrimitive
    policy: EditPolicy = Field(default_factory=EditPolicy)
    reason: str = Field(default="", max_length=2000)

    @field_validator("session_id", "reason")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class FreeModeRequest(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    instruction: str = Field(min_length=1, max_length=20_000)
    input_asset_ids: tuple[int, ...] = ()
    allow_install_nodes: bool = False
    allow_download_models: bool = False

    @field_validator("session_id", "instruction")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("input_asset_ids")
    @classmethod
    def validate_asset_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        for asset_id in value:
            if asset_id < 1:
                raise ValueError("input_asset_ids must contain asset IDs >= 1")
        return value

    @model_validator(mode="after")
    def require_instruction(self) -> FreeModeRequest:
        if not self.instruction:
            raise ValueError("instruction cannot be empty.")
        return self


StudioPrimitiveId = Literal["moody-generate", "moody-edit"]


__all__ = [
    "EditPolicy",
    "ExecutionOrigin",
    "ExecutionPlan",
    "FreeModeRequest",
    "ReferenceRole",
    "StudioPrimitive",
    "StudioPrimitiveId",
    "VisualAction",
    "VisualOperation",
    "VisualReference",
    "VisualSession",
]
