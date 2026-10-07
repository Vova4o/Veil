"""One-time, non-destructive import of a legacy local library."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def import_legacy_library(source: Path, destination: Path) -> bool:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination or not (source / "meta.db").is_file():
        return False
    if destination.exists() and any(destination.iterdir()):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cmv-import-", dir=destination.parent) as staging:
        staged = Path(staging) / "data"
        staged.mkdir()
        # SQLite backup includes committed WAL contents without copying a live WAL.
        for database in source.glob("*.db"):
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as original:
                with closing(sqlite3.connect(staged / database.name)) as copy:
                    original.backup(copy)
                    if copy.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise RuntimeError(f"Library integrity check failed: {database.name}")
        for config in source.glob("*.json"):
            shutil.copy2(config, staged / config.name)
        templates = source / "workflow_templates"
        if templates.is_dir():
            shutil.copytree(templates, staged / templates.name)
        (staged / "desktop-import.json").write_text(
            json.dumps({"schema_version": 1, "source": str(source)}, indent=2),
            encoding="utf-8",
        )
        if destination.exists():
            destination.rmdir()  # Only the verified empty target, never a recursive delete.
        staged.rename(destination)
    return True
