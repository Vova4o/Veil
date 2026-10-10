"""Authenticated loopback WSGI runtime owned by the Electron parent process."""

from __future__ import annotations

import hmac
import json
import os
import signal
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, request

from .desktop_cache import configure_legacy_cache
from .desktop_data import import_legacy_library

TOKEN_HEADER = "X-CMV-Desktop-Token"


def install_desktop_guard(application: Flask, token: str) -> None:
    @application.before_request
    def require_desktop_token():
        # This existing OAuth callback validates its one-time state in VKPublisher.
        if request.path == "/api/social/vk/auth/callback" and request.method == "GET":
            return None
        if not hmac.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            return jsonify({"error": "Desktop session required"}), 403
        return None

    @application.after_request
    def desktop_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' data: blob: https:; "
            "script-src 'self' 'unsafe-inline' https://esm.sh; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https:; media-src 'self' blob:; "
            "connect-src 'self' https: http://127.0.0.1:* ws://127.0.0.1:*; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    application.add_url_rule(
        "/_desktop/health", "desktop_health", lambda: jsonify({
            "pid": os.getpid(), "port": application.config.get("DESKTOP_PORT"),
        }),
    )


def main() -> None:
    token = os.environ.get("COMFY_META_DESKTOP_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("The desktop backend must be started by its Electron launcher.")
    data_dir = Path(os.environ["COMFY_META_DATA_DIR"])
    import_dir = os.environ.get("COMFY_META_IMPORT_DIR")
    if import_dir:
        import_legacy_library(Path(import_dir), data_dir)
    legacy_cache = os.environ.get("COMFY_META_IMPORT_CACHE_DIR")
    eligible = configure_legacy_cache(data_dir, Path(legacy_cache) if legacy_cache else None)
    if eligible:
        print(f"[Desktop] Legacy cache available for {eligible} matching assets", flush=True)

    if getattr(sys, "frozen", False):
        tools_dir = Path(sys.executable).parent / "tools"
        suffix = ".exe" if sys.platform == "win32" else ""
        for tool in ("ffmpeg", "ffprobe"):
            executable = tools_dir / f"{tool}{suffix}"
            if executable.is_file():
                os.environ.setdefault(f"COMFY_META_{tool.upper()}", str(executable))

    from waitress import create_server

    from .main import app
    from .runtime import application_runtime

    install_desktop_guard(app, token)
    stop = threading.Event()

    def read_parent() -> None:
        # EOF also means the owner exited; there is no independent server lifetime.
        for line in sys.stdin:
            if line.strip() == "shutdown":
                break
        stop.set()

    threading.Thread(target=read_parent, name="desktop-parent", daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with application_runtime():
        server = create_server(app, host="127.0.0.1", port=0, threads=16)
        app.config["DESKTOP_PORT"] = int(server.effective_port)
        serving = threading.Thread(target=server.run, name="desktop-http", daemon=True)
        serving.start()
        print("CMV_DESKTOP_READY " + json.dumps({
            "port": int(server.effective_port), "pid": os.getpid(),
            "session": os.environ.get("COMFY_META_DESKTOP_SESSION"),
        }), flush=True)
        try:
            while not stop.wait(0.5):
                if not serving.is_alive():
                    raise RuntimeError("The desktop HTTP server stopped unexpectedly.")
        finally:
            server.close()
            serving.join(timeout=5)


if __name__ == "__main__":
    main()
