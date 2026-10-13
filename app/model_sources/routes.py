"""Integrations › Model sources: the Civitai API token."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from app.config_store import ConfigStore

from .tokens import (
    ModelSourceError,
    check_civitai_token,
    civitai_state,
    civitai_token,
    clear_civitai_token,
    save_civitai_token,
)

model_sources_blueprint = Blueprint("model_sources", __name__)

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _config_store() -> ConfigStore:
    return current_app.config["CONFIG_STORE"]


def _local_only():
    """Tokens are set and used only from the app on this computer."""
    if str(request.remote_addr or "") not in _LOOPBACK:
        return jsonify({"error": "Available only in the app on this computer.", "code": "local_only"}), 403
    return None


def _error(exc: ModelSourceError):
    status = 400 if exc.code in {"token_empty", "token_rejected"} else 502
    return jsonify({"error": str(exc), "code": exc.code}), status


@model_sources_blueprint.route("/api/model-sources", methods=["GET"])
def model_sources():
    return jsonify({"civitai": civitai_state(_config_store())})


@model_sources_blueprint.route("/api/model-sources/civitai", methods=["POST", "DELETE"])
def civitai():
    blocked = _local_only()
    if blocked:
        return blocked
    store = _config_store()
    if request.method == "DELETE":
        try:
            clear_civitai_token(store)
        except ModelSourceError as exc:
            return _error(exc)
        return jsonify({"civitai": civitai_state(store)})

    token = str((request.get_json(silent=True) or {}).get("token") or "").strip()
    if not token:
        return _error(ModelSourceError("Paste a Civitai API key.", code="token_empty"))
    # A token Civitai refuses is not saved; without a connection it is saved unchecked.
    account, warning = None, None
    try:
        account = check_civitai_token(token)
    except ModelSourceError as exc:
        if exc.code == "token_rejected":
            return _error(exc)
        warning = f"Saved without checking: {exc}"
    try:
        save_civitai_token(store, token)
    except ModelSourceError as exc:
        return _error(exc)
    return jsonify({"civitai": civitai_state(store), "account": account, "warning": warning})


@model_sources_blueprint.route("/api/model-sources/civitai/check", methods=["POST"])
def civitai_check():
    blocked = _local_only()
    if blocked:
        return blocked
    token = civitai_token(_config_store())
    if not token:
        return jsonify({"error": "No Civitai API key is saved.", "code": "token_missing"}), 404
    try:
        account = check_civitai_token(token)
    except ModelSourceError as exc:
        return _error(exc)
    return jsonify({"account": account})
