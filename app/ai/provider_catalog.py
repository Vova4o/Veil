"""Provider templates for Integrations › Providers, taken from models.dev.

``assets/provider_catalog.json`` is rebuilt by ``scripts/update_provider_catalog.py``
from the copy of models.dev that OpenCode keeps. It lists the providers MediaStudio can reach
through an OpenAI-compatible endpoint, with their base URL, key variable and the
cards of their models (photo input, reasoning, free, family, release date).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("assets") / "provider_catalog.json"
FLAG_VISION, FLAG_REASONING, FLAG_FREE = 1, 2, 4


@lru_cache(maxsize=1)
def _catalog() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        item["id"]: item
        for item in payload.get("providers") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "name": item.get("name") or item["id"],
        "base_url": item.get("base_url") or "",
        "env": list(item.get("env") or []),
        "doc": item.get("doc") or "",
        "note": item.get("note") or "",
        "popular": item.get("popular"),
        "local": bool(item.get("local")),
        "models": len(item.get("models") or []),
    }


def templates() -> list[dict[str, Any]]:
    """Every template without its model cards, popular ones first."""
    return [_public(item) for item in _catalog().values()]


def template(template_id: str | None) -> dict[str, Any] | None:
    item = _catalog().get(template_id or "")
    return _public(item) if item else None


def template_for_url(base_url: str) -> dict[str, Any] | None:
    """The template whose endpoint is ``base_url`` (a migrated or custom provider)."""
    wanted = base_url.rstrip("/").casefold()
    for item in _catalog().values():
        if (item.get("base_url") or "").rstrip("/").casefold() == wanted:
            return _public(item)
    return None


def model_cards(template_id: str | None) -> dict[str, dict[str, Any]]:
    """The models.dev cards of one template by model id."""
    item = _catalog().get(template_id or "")
    cards: dict[str, dict[str, Any]] = {}
    for row in (item or {}).get("models") or []:
        if not isinstance(row, list) or len(row) < 5:
            continue
        model_id, name, flags, family, released = row[:5]
        cards[model_id] = {
            "id": model_id,
            "name": name,
            "vision": bool(flags & FLAG_VISION),
            "reasoning": bool(flags & FLAG_REASONING),
            "free": bool(flags & FLAG_FREE),
            "family": family or None,
            "released": released or None,
        }
    return cards
