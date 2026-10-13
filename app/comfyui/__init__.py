from __future__ import annotations

from .routes import comfyui_blueprint
from .simple_download_probe import install_simple_download_probe
from .simple_routes import simple_blueprint

install_simple_download_probe()

__all__ = [
    "comfyui_blueprint",
    "simple_blueprint",
]
