from __future__ import annotations

from typing import Any

MODEL_INPUT_NAMES = {
    "ckpt_name",
    "unet_name",
    "diffusion_model",
    "vae_name",
    "clip_name",
    "lora_name",
    "model_name",
    "control_net_name",
}


def normalize_comfyui_error(raw: Any, *, status: str = "failed") -> dict[str, Any]:
    """Turn ComfyUI job/validation failures into a user-facing diagnostic."""
    original = raw if isinstance(raw, dict) else {"message": str(raw or "")}
    detail = _error_detail(original)
    extra = detail.get("extra_info") if isinstance(detail.get("extra_info"), dict) else {}

    node_id = _text(detail.get("node_id"))
    class_type = _text(detail.get("node_type") or detail.get("class_type"))
    input_name = _text(detail.get("input_name") or extra.get("input_name"))
    error_type = _text(detail.get("type") or detail.get("exception_type"))
    technical_message = _first_message(detail, original)
    expected_type = _expected_type(detail, extra)
    received_type = _received_type(detail, extra)
    category = _category(
        status=status,
        error_type=error_type,
        message=technical_message,
        input_name=input_name,
    )
    message, action = _guidance(
        category,
        target_label=input_name,
        technical_message=technical_message,
    )
    return {
        "message": message,
        "category": category,
        "suggested_action": action,
        "node_id": node_id,
        "class_type": class_type,
        "input_name": input_name,
        "expected_type": expected_type,
        "received_type": received_type,
        "technical_message": technical_message,
        "raw": original,
    }


def _error_detail(raw: dict[str, Any]) -> dict[str, Any]:
    node_errors = raw.get("node_errors")
    if isinstance(node_errors, dict):
        for node_id, node_error in node_errors.items():
            if not isinstance(node_error, dict):
                continue
            errors = node_error.get("errors")
            if not isinstance(errors, list) or not errors:
                continue
            first = errors[0]
            if not isinstance(first, dict):
                continue
            return {
                **first,
                "node_id": str(node_id),
                "node_type": node_error.get("class_type"),
            }
    nested = raw.get("execution_error") or raw.get("error")
    if isinstance(nested, dict):
        return nested
    return raw


def _first_message(detail: dict[str, Any], raw: dict[str, Any]) -> str:
    candidates = (
        detail.get("exception_message"),
        detail.get("message"),
        detail.get("details"),
        raw.get("message"),
    )
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text:
            return text
    return "ComfyUI execution failed."


def _expected_type(detail: dict[str, Any], extra: dict[str, Any]) -> str | None:
    direct = detail.get("expected_type") or extra.get("expected_type")
    if direct is not None:
        return _type_text(direct)
    config = extra.get("input_config")
    if isinstance(config, (list, tuple)) and config:
        if isinstance(config[0], (list, tuple)):
            count = len(config[0])
            suffix = "value" if count == 1 else "values"
            return f"choice ({count} allowed {suffix})"
        return _type_text(config[0])
    return None


def _received_type(detail: dict[str, Any], extra: dict[str, Any]) -> str | None:
    direct = detail.get("received_type") or extra.get("received_type")
    if direct is not None:
        return _type_text(direct)
    if "received_value" in extra:
        return type(extra["received_value"]).__name__
    return None


def _type_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " | ".join(str(item) for item in value)
    return str(value)


def _category(
    *,
    status: str,
    error_type: str | None,
    message: str,
    input_name: str | None,
) -> str:
    haystack = f"{error_type or ''} {message}".casefold()
    if status in {"cancelled", "canceled"} or any(
        token in haystack for token in ("interrupt", "cancelled", "canceled")
    ):
        return "cancelled"
    if any(token in haystack for token in ("out of memory", "cuda oom", "memoryerror", "allocation on device")):
        return "out_of_memory"
    if (
        (error_type or "").casefold() in {"value_not_in_list", "file_not_found", "missing_file"}
        and input_name in MODEL_INPUT_NAMES
    ) or any(token in haystack for token in ("no such file", "file not found", "does not exist")):
        return "missing_resource"
    if any(token in haystack for token in (
        "return_type_mismatch",
        "mat1 and mat2 shapes",
        "shape mismatch",
        "tensor size",
        "incompatible model",
        "incompatible tensor",
    )):
        return "workflow_incompatible"
    if (error_type or "").casefold() in {
        "invalid_input_type",
        "value_smaller_than_min",
        "value_bigger_than_max",
        "value_not_in_list",
        "required_input_missing",
    }:
        return "invalid_input"
    return "execution_failure"


def _guidance(
    category: str,
    *,
    target_label: str | None,
    technical_message: str,
) -> tuple[str, str]:
    target = f" “{target_label}”" if target_label else ""
    if category == "cancelled":
        return "Generation was cancelled.", "Start it again when you are ready."
    if category == "out_of_memory":
        return (
            "ComfyUI ran out of GPU memory while generating.",
            "Reduce the number of images or image resolution, then retry.",
        )
    if category == "missing_resource":
        return (
            f"ComfyUI cannot find the selected resource{target}.",
            "Install the missing model files, then retry.",
        )
    if category == "invalid_input":
        return (
            f"ComfyUI rejected the value for{target or ' a workflow input'}.",
            "Check the value named in the technical details, then retry.",
        )
    if category == "workflow_incompatible":
        return (
            "The model components are incompatible with this workflow.",
            "Check the model files named in the technical details, then retry.",
        )
    concise = technical_message.splitlines()[0].strip()
    if len(concise) > 220:
        concise = f"{concise[:217]}..."
    return (
        concise or "ComfyUI could not complete this workflow.",
        "Review the technical details and the failing node before retrying.",
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = ["normalize_comfyui_error"]
