"""Studio workflow primitives (EDITOR-STUDIO-RUNTIME-PLAN §5, §9).

The runtime uses a small number of tested workflow primitives instead of a
model zoo. Several visual operations share one physical ComfyUI graph: e.g.
``change_pose``, ``replace_background``, ``change_wardrobe``, ``relocate``
and ``add_character`` are all policies over the single Krea edit primitive.

Honesty note: no Krea2 graph has been validated against live ComfyUI yet
(``model_04`` itself ships ``workflow_ready=false``), so both primitives
report ``pending_graph`` until a tested ``workflow.json`` lands. The registry
already pins the backend, the operation mapping, and the validation checklist
so a future tested graph plugs in without changing the API.
"""

from __future__ import annotations

from typing import Any

from app.visual_runtime.backend import (
    STUDIO_BACKEND_ARCHITECTURE,
    STUDIO_BACKEND_MODEL_FILENAME,
    STUDIO_BACKEND_RESOURCE_FOLDER,
    studio_backend_candidates,
)
from app.visual_runtime.models import (
    EditPolicy,
    ExecutionPlan,
    StudioPrimitive,
    VisualOperation,
)


#: Operations served by the text-to-image primitive (no source image needed).
GENERATE_OPERATIONS: tuple[VisualOperation, ...] = (
    VisualOperation.GENERATE_SCENE,
)

#: Operations served by the image-editing primitive (policies over one graph).
EDIT_OPERATIONS: tuple[VisualOperation, ...] = (
    VisualOperation.COMPOSE_SCENE,
    VisualOperation.EDIT_SCENE,
    VisualOperation.ADD_CHARACTER,
    VisualOperation.REPLACE_CHARACTER,
    VisualOperation.CHANGE_POSE,
    VisualOperation.CHANGE_WARDROBE,
    VisualOperation.RELOCATE,
    VisualOperation.REPLACE_BACKGROUND,
    VisualOperation.ADJUST_LIGHTING,
    VisualOperation.TYPOGRAPHY,
    VisualOperation.POSTERIZE,
    VisualOperation.FINALIZE,
)

#: Node classes a tested Krea2 graph is expected to provide. This is a
#: validation checklist for the future tested graph, not a verified claim.
EXPECTED_KREA_NODES: tuple[str, ...] = (
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "CLIPTextEncode",
    "KSampler",
    "VAEDecode",
    "VAEEncode",
    "LoadImage",
    "SaveImage",
)

#: Resource slots a tested primitive graph must expose.
PRIMITIVE_RESOURCE_SLOTS: tuple[dict[str, Any], ...] = (
    {
        "slot": "diffusion_model",
        "folder": STUDIO_BACKEND_RESOURCE_FOLDER,
        "filename": STUDIO_BACKEND_MODEL_FILENAME,
        "aliases": list(studio_backend_candidates()[1:]),
    },
    {"slot": "text_encoder", "folder": "text_encoders", "filename": None},
    {"slot": "vae", "folder": "vae", "filename": None},
)


def operations_for_primitive(primitive: StudioPrimitive) -> tuple[VisualOperation, ...]:
    """Return the visual operations served by *primitive*."""
    if primitive == StudioPrimitive.MOODY_GENERATE:
        return GENERATE_OPERATIONS
    return EDIT_OPERATIONS


def resolve_primitive(
    operation: VisualOperation | str,
    *,
    has_current_image: bool = False,
) -> StudioPrimitive:
    """Map a visual operation to its workflow primitive.

    ``compose_scene`` with no working image yet starts from generation;
    once a current image exists it becomes a policy over the edit graph.
    """
    operation_value = (
        operation.value if isinstance(operation, VisualOperation) else str(operation)
    )
    try:
        resolved = VisualOperation(operation_value)
    except ValueError as exc:
        raise ValueError(f"unknown visual operation '{operation_value}'") from exc
    if resolved == VisualOperation.COMPOSE_SCENE and not has_current_image:
        return StudioPrimitive.MOODY_GENERATE
    if resolved in GENERATE_OPERATIONS and has_current_image:
        return StudioPrimitive.MOODY_EDIT
    if resolved in GENERATE_OPERATIONS:
        return StudioPrimitive.MOODY_GENERATE
    return StudioPrimitive.MOODY_EDIT


def default_policy_for_operation(operation: VisualOperation | str) -> EditPolicy:
    """Seed execution policy: what stays fixed vs. what may change."""
    operation_value = (
        operation.value if isinstance(operation, VisualOperation) else str(operation)
    )
    try:
        resolved = VisualOperation(operation_value)
    except ValueError as exc:
        raise ValueError(f"unknown visual operation '{operation_value}'") from exc

    preserve_identity = resolved != VisualOperation.REPLACE_CHARACTER
    preserve_environment = resolved in (
        VisualOperation.CHANGE_POSE,
        VisualOperation.CHANGE_WARDROBE,
        VisualOperation.ADJUST_LIGHTING,
        VisualOperation.TYPOGRAPHY,
        VisualOperation.POSTERIZE,
        VisualOperation.FINALIZE,
    )
    preserve_pose = resolved in (
        VisualOperation.CHANGE_WARDROBE,
        VisualOperation.REPLACE_BACKGROUND,
        VisualOperation.ADJUST_LIGHTING,
        VisualOperation.TYPOGRAPHY,
        VisualOperation.POSTERIZE,
        VisualOperation.FINALIZE,
    )
    return EditPolicy(
        preserve_identity=preserve_identity,
        preserve_pose=preserve_pose,
        preserve_environment=preserve_environment,
    )


def compile_plan(
    session_id: str,
    operation: VisualOperation | str,
    instruction: str = "",
    *,
    has_current_image: bool = False,
) -> ExecutionPlan:
    """Build a typed execution plan without touching ComfyUI or the DB."""
    operation_value = (
        operation.value if isinstance(operation, VisualOperation) else str(operation)
    )
    try:
        resolved = VisualOperation(operation_value)
    except ValueError as exc:
        raise ValueError(f"unknown visual operation '{operation_value}'") from exc
    primitive = resolve_primitive(resolved, has_current_image=has_current_image)
    reason = (
        f"'{resolved.value}' resolves to '{primitive.value}' "
        f"on {STUDIO_BACKEND_MODEL_FILENAME}"
    )
    if resolved == VisualOperation.COMPOSE_SCENE:
        reason += (
            " with the current image as edit base"
            if has_current_image
            else " from references (no working image yet)"
        )
    return ExecutionPlan(
        session_id=session_id.strip(),
        operation=resolved,
        primitive=primitive,
        policy=default_policy_for_operation(resolved),
        reason=reason,
    )


def describe_primitive(primitive: StudioPrimitive) -> dict[str, Any]:
    """Serializable registry record for one primitive."""
    return {
        "id": primitive.value,
        "backend": {
            "filename": STUDIO_BACKEND_MODEL_FILENAME,
            "architecture": STUDIO_BACKEND_ARCHITECTURE,
            "folder": STUDIO_BACKEND_RESOURCE_FOLDER,
        },
        "operations": [op.value for op in operations_for_primitive(primitive)],
        "graph": None,  # No tested workflow.json registered yet.
        "graph_status": "pending_graph",
        "resource_slots": [dict(slot) for slot in PRIMITIVE_RESOURCE_SLOTS],
        "expected_nodes": list(EXPECTED_KREA_NODES),
    }


def list_primitives() -> list[dict[str, Any]]:
    """Describe all registered Studio primitives."""
    return [describe_primitive(item) for item in StudioPrimitive]


def _inventory_node_types(inventory: Any | None) -> set[str]:
    nodes = getattr(inventory, "nodes", None)
    if isinstance(nodes, dict):
        return {str(name) for name in nodes}
    if isinstance(nodes, (list, tuple, set)):
        return {str(name) for name in nodes}
    return set()


def _inventory_files(inventory: Any | None, folder: str) -> set[str]:
    models = getattr(inventory, "models", None)
    if not isinstance(models, dict):
        return set()
    files = models.get(folder, [])
    if not isinstance(files, list):
        return set()
    return {str(name).casefold() for name in files}


def validate_primitive(
    primitive: StudioPrimitive | str,
    inventory: Any | None = None,
) -> dict[str, Any]:
    """Check a primitive against a live runtime inventory.

    ``ready`` is True only when the backend file, the expected node classes,
    and a tested graph are all present. Today the graph is always missing, so
    the result is honestly ``pending`` with machine-readable reasons.
    """
    primitive_value = (
        primitive.value if isinstance(primitive, StudioPrimitive) else str(primitive)
    )
    try:
        resolved = StudioPrimitive(primitive_value)
    except ValueError as exc:
        raise ValueError(f"unknown studio primitive '{primitive_value}'") from exc

    record = describe_primitive(resolved)
    reasons: list[str] = []
    backend_ready = False
    nodes_ready = False

    if inventory is None or not isinstance(
        getattr(inventory, "models", None), dict
    ):
        reasons.append("runtime inventory is unavailable")
    else:
        known_files: set[str] = set()
        for folder in (STUDIO_BACKEND_RESOURCE_FOLDER, "checkpoints"):
            known_files.update(_inventory_files(inventory, folder))
        wanted = {name.casefold() for name in studio_backend_candidates()}
        backend_ready = bool(known_files & wanted)
        if not backend_ready:
            reasons.append(
                f"backend file '{STUDIO_BACKEND_MODEL_FILENAME}' is not installed"
            )
        node_types = _inventory_node_types(inventory)
        if node_types:
            missing_nodes = [
                name for name in EXPECTED_KREA_NODES if name not in node_types
            ]
            nodes_ready = not missing_nodes
            if missing_nodes:
                reasons.append(
                    "custom nodes missing: " + ", ".join(sorted(missing_nodes))
                )
        else:
            reasons.append("node inventory is unavailable")

    graph_ready = record["graph"] is not None
    if not graph_ready:
        reasons.append("no tested workflow graph registered yet")

    status = "ready" if (backend_ready and nodes_ready and graph_ready) else "pending"
    return {
        **record,
        "status": status,
        "ready": status == "ready",
        "checks": {
            "backend": backend_ready,
            "nodes": nodes_ready,
            "graph": graph_ready,
        },
        "reasons": reasons,
    }


__all__ = [
    "EDIT_OPERATIONS",
    "EXPECTED_KREA_NODES",
    "GENERATE_OPERATIONS",
    "PRIMITIVE_RESOURCE_SLOTS",
    "compile_plan",
    "default_policy_for_operation",
    "describe_primitive",
    "list_primitives",
    "operations_for_primitive",
    "resolve_primitive",
    "validate_primitive",
]
