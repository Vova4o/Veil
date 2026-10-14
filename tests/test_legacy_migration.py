"""Data written before the codename rename must keep working after it."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from app.paths import (
    DATA_DIRNAME,
    LEGACY_DATA_DIRNAME,
    build_runtime_paths,
    migrate_legacy_data_dir,
)
from app.remote.colab_cli import ColabCLI


def _legacy_dir(root):
    legacy = root / LEGACY_DATA_DIRNAME
    legacy.mkdir()
    (legacy / "meta.db").write_text("library", encoding="utf-8")
    return legacy


def test_default_development_data_dir_is_moved_once(tmp_path):
    legacy = _legacy_dir(tmp_path)
    paths = build_runtime_paths({}, project_root=tmp_path)

    migrate_legacy_data_dir(paths)
    migrate_legacy_data_dir(paths)

    assert paths.data_dir == tmp_path / DATA_DIRNAME
    assert not legacy.exists()
    assert paths.database.read_text(encoding="utf-8") == "library"


def test_existing_current_data_dir_is_never_overwritten(tmp_path):
    legacy = _legacy_dir(tmp_path)
    (tmp_path / DATA_DIRNAME).mkdir()
    paths = build_runtime_paths({}, project_root=tmp_path)

    migrate_legacy_data_dir(paths)

    assert legacy.is_dir()
    assert not paths.database.exists()


def test_explicit_data_dir_is_left_alone(tmp_path):
    legacy = _legacy_dir(tmp_path)
    custom = tmp_path / "elsewhere"
    paths = build_runtime_paths({"MEDIASTUDIO_DATA_DIR": str(custom)}, project_root=tmp_path)

    migrate_legacy_data_dir(paths)

    assert legacy.is_dir()
    assert not custom.exists()


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_colab_command_moves_the_old_session_folder_first(tmp_path):
    old = tmp_path / ".config" / "cmv-colab"
    old.mkdir(parents=True)
    (old / "sessions.json").write_text("{}", encoding="utf-8")
    command = ColabCLI.command("list").replace("colab --auth", "true --auth", 1)

    subprocess.run(["sh", "-c", command], env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, check=True, capture_output=True)

    assert not old.exists()
    assert (tmp_path / ".config" / "mediastudio-colab" / "sessions.json").read_text(encoding="utf-8") == "{}"
