"""Shared helpers for agent-host CLIs (Claude Code, Antigravity).

Instruction bundles are far longer than the Windows command line allows
(cmd.exe: 8191 characters, CreateProcess: 32767), so the task package never
travels as an argument. Every run gets its own temporary folder that holds the
task package and, when present, a copy of the input image.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..prompting import InstructionBundle

MAX_USER_INPUT_CHARS = 100_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
TASK_FILE_NAME = "mediastudio-task.md"


class AgentCLIInputError(ValueError):
    def __init__(self, message: str, *, code: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class AgentRunWorkspace:
    root: Path
    task_file: Path | None
    image_name: str | None


def image_file_name(image_path: Path | None) -> str | None:
    """Name of the image copy inside the run folder."""
    return f"input{image_path.suffix.lower()}" if image_path is not None else None


def validate_user_input(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentCLIInputError("Prompt task input cannot be empty.", code="invalid_input")
    cleaned = value.strip()
    if len(cleaned) > MAX_USER_INPUT_CHARS:
        raise AgentCLIInputError("Prompt task input is too large.", code="input_too_large")
    return cleaned


def validate_image_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser().resolve(strict=False)
    if not path.is_file():
        raise AgentCLIInputError(f"Image file does not exist: {path}", code="image_not_found")
    if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        raise AgentCLIInputError(
            "Agent images must be PNG, JPEG, WEBP, or GIF.", code="unsupported_image"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AgentCLIInputError(
            f"Cannot inspect image file: {path}: {exc}", code="image_read_error"
        ) from exc
    if size <= 0:
        raise AgentCLIInputError("The selected image is empty.", code="invalid_image")
    if size > MAX_IMAGE_BYTES:
        raise AgentCLIInputError("Image input is too large.", code="image_too_large")
    return path


def render_task_package(
    bundle: InstructionBundle,
    user_input: str,
    *,
    image_name: str | None = None,
    image_attached: bool = False,
) -> str:
    """``image_attached`` means the host attaches the image itself (no file reading)."""
    if image_name and image_attached:
        image_block = (
            "INPUT IMAGE\n"
            "The input image is attached to this message. Inspect it before writing. "
            "Do not read any files.\n\n"
        )
    elif image_name:
        image_block = (
            "INPUT IMAGE\n"
            f"The input image is the file `{image_name}` in the current working directory. "
            "Open it with your file-reading tool and inspect it before writing. It is the only "
            "file you may read besides this task package.\n\n"
        )
    else:
        image_block = ""
    return (
        "# MediaStudio agent task\n\n"
        "The instruction bundle below is authoritative. Do not inspect the project, browse, "
        "run commands, or seek additional context.\n\n"
        f"{bundle.render()}\n"
        f"{image_block}"
        "USER TASK INPUT\n"
        f"{user_input}\n"
    )


@contextmanager
def agent_run_workspace(
    prefix: str,
    *,
    image_path: Path | None = None,
    task_package: str | None = None,
) -> Iterator[AgentRunWorkspace]:
    """Create a throwaway run folder with an optional image copy and task file.

    Some hosts (agy) leave a background process whose working directory is the
    run folder, so Windows cannot delete it right away. A folder that cannot be
    removed stays in the temp directory instead of failing a finished run.
    """
    with tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True) as temp_dir:
        root = Path(temp_dir)
        task_file = None
        if task_package is not None:
            task_file = root / TASK_FILE_NAME
            task_file.write_text(task_package.strip() + "\n", encoding="utf-8", newline="\n")
        image_name = image_file_name(image_path)
        if image_path is not None and image_name is not None:
            shutil.copyfile(image_path, root / image_name)
        yield AgentRunWorkspace(root=root, task_file=task_file, image_name=image_name)


def unwrap_markdown_fence(text: str) -> tuple[str, bool]:
    """Return the body of the first Markdown code block, if the reply has one."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().endswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip(), True
    start = cleaned.find("```")
    if start == -1:
        return cleaned, False
    body_start = cleaned.find("\n", start)
    end = cleaned.find("```", body_start if body_start != -1 else start + 3)
    if body_start == -1 or end == -1:
        return cleaned, False
    return cleaned[body_start + 1:end].strip(), True


__all__ = [
    "TASK_FILE_NAME",
    "AgentCLIInputError",
    "AgentRunWorkspace",
    "agent_run_workspace",
    "image_file_name",
    "render_task_package",
    "unwrap_markdown_fence",
    "validate_image_path",
    "validate_user_input",
]
