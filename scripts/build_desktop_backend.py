"""Build a self-contained Python backend and bundle its video tools."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def media_tool(name: str, override: str | None) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    directory = override or os.environ.get("MEDIASTUDIO_MEDIA_TOOLS_DIR")
    if directory:
        candidate = Path(directory) / f"{name}{suffix}"
    else:
        found = shutil.which(name)
        if not found:
            raise SystemExit(f"Install {name} or set MEDIASTUDIO_MEDIA_TOOLS_DIR before building.")
        candidate = Path(found)
        # Chocolatey's PATH executables are launcher shims, not distributable tools.
        chocolatey = os.environ.get("ChocolateyInstall")
        if sys.platform == "win32" and chocolatey:
            real_tool = Path(chocolatey) / "lib/ffmpeg/tools/ffmpeg/bin" / candidate.name
            if candidate.parent == Path(chocolatey) / "bin" and real_tool.is_file():
                candidate = real_tool
    if not candidate.is_file():
        raise SystemExit(f"Video tool is missing: {candidate}")
    return candidate.resolve()


def build() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media-tools-dir")
    args = parser.parse_args()
    tools = [media_tool(name, args.media_tools_dir) for name in ("ffmpeg", "ffprobe")]
    output = (ROOT / "build/desktop-backend").resolve()
    work = (ROOT / "build/pyinstaller").resolve()
    if not output.is_relative_to(ROOT) or not work.is_relative_to(ROOT):
        raise SystemExit("Build paths escaped the workspace.")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir",
        "--name", "mediastudio-backend", "--distpath", str(output),
        "--workpath", str(work), "--specpath", str(work), "--paths", str(ROOT),
        "--collect-data", "app", "--collect-submodules", "keyring.backends",
        str(ROOT / "desktop/backend_entry.py"),
    ], cwd=ROOT, check=True)
    tool_output = output / "mediastudio-backend/tools"
    tool_output.mkdir(parents=True, exist_ok=True)
    for executable in tools:
        shutil.copy2(executable, tool_output / executable.name)
    build_icons()


def build_icons() -> None:
    from PIL import Image, ImageDraw

    directory = ROOT / "desktop/assets"
    directory.mkdir(parents=True, exist_ok=True)
    icon = Image.new("RGBA", (256, 256))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((8, 8, 248, 248), radius=48, fill="#17181e")
    draw.rounded_rectangle((48, 58, 208, 198), radius=14, outline="#b9b6df", width=10)
    draw.line((50, 92, 206, 92), fill="#b9b6df", width=8)
    draw.rectangle((70, 114, 114, 172), fill="#77738e")
    draw.rectangle((132, 114, 184, 138), fill="#b9b6df")
    draw.rectangle((132, 150, 184, 172), fill="#77738e")
    icon.save(directory / "icon.png")
    icon.save(directory / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    build()
