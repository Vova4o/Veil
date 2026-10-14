"""Rebuild app/ai/assets/provider_catalog.json from the models.dev catalog.

OpenCode keeps a copy of https://models.dev/api.json in ``~/.cache/opencode/models.json``;
that copy is read by default, so no network access is needed. Only providers MediaStudio can
reach through an OpenAI-compatible ``/chat/completions`` endpoint are kept.

    poetry run python scripts/update_provider_catalog.py
    poetry run python scripts/update_provider_catalog.py --source https://models.dev/api.json
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "app" / "ai" / "assets" / "provider_catalog.json"
DEFAULT_SOURCE = Path.home() / ".cache" / "opencode" / "models.json"

# Providers whose SDK is not the generic OpenAI-compatible one, but which also serve
# the OpenAI chat completions API at a documented address.
OPENAI_COMPATIBLE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
    "togetherai": "https://api.together.xyz/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "vercel": "https://ai-gateway.vercel.sh/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "perplexity": "https://api.perplexity.ai",
    "cohere": "https://api.cohere.ai/compatibility/v1",
    "venice": "https://api.venice.ai/api/v1",
}
# Reached through their own local tools or a sign-in flow, not an API key.
EXCLUDED = {"opencode", "opencode-go", "github-copilot", "gitlab"}
# Shown first, in this order, under "Popular providers".
POPULAR = ["openai", "anthropic", "google", "openrouter", "vercel"]
NOTES = {
    "openai": "GPT models for fast, capable general AI tasks",
    "anthropic": "Direct access to Claude models via API key",
    "google": "Gemini models for fast, structured responses",
    "openrouter": "Access all supported models from one provider",
    "vercel": "Unified access to AI models with smart routing",
    "lmstudio": "Models running in LM Studio on this computer",
    "ollama": "Models running in Ollama on this computer",
}
# Local servers that models.dev does not list.
EXTRA = {
    "ollama": {
        "name": "Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "env": [],
        "doc": "https://ollama.com/library",
        "local": True,
        "models": [],
    },
}
LOCAL = {"lmstudio"}

FLAG_VISION = 1
FLAG_REASONING = 2
FLAG_FREE = 4


def load_source(source: str) -> dict[str, Any]:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=60) as response:
            return json.load(response)
    return json.loads(Path(source).read_text(encoding="utf-8"))


def base_url(provider_id: str, provider: dict[str, Any]) -> str | None:
    if provider_id in OPENAI_COMPATIBLE_URLS:
        return OPENAI_COMPATIBLE_URLS[provider_id]
    api = provider.get("api")
    if provider.get("npm") != "@ai-sdk/openai-compatible" or not isinstance(api, str):
        return None
    if "${" in api or not api.startswith(("http://", "https://")):
        return None
    return api.rstrip("/")


def model_row(model: dict[str, Any]) -> list[Any]:
    inputs = (model.get("modalities") or {}).get("input") or []
    cost = model.get("cost") or {}
    flags = 0
    if "image" in inputs:
        flags |= FLAG_VISION
    if model.get("reasoning"):
        flags |= FLAG_REASONING
    if cost and not cost.get("input") and not cost.get("output"):
        flags |= FLAG_FREE
    return [
        model["id"],
        model.get("name") or model["id"],
        flags,
        model.get("family") or "",
        model.get("release_date") or "",
    ]


def build(catalog: dict[str, Any]) -> dict[str, Any]:
    providers: list[dict[str, Any]] = []
    for provider_id, provider in catalog.items():
        if provider_id in EXCLUDED or not isinstance(provider, dict):
            continue
        url = base_url(provider_id, provider)
        if url is None:
            continue
        models = [
            model_row(model)
            for model in (provider.get("models") or {}).values()
            if isinstance(model, dict) and model.get("id") and model.get("status") != "deprecated"
        ]
        models.sort(key=lambda row: row[1].casefold())
        entry: dict[str, Any] = {
            "id": provider_id,
            "name": provider.get("name") or provider_id,
            "base_url": url,
            "env": [name for name in provider.get("env") or [] if isinstance(name, str)],
            "doc": provider.get("doc") or "",
            "models": models,
        }
        if provider_id in LOCAL:
            entry["local"] = True
        providers.append(entry)
    for provider_id, extra in EXTRA.items():
        if not any(item["id"] == provider_id for item in providers):
            providers.append({"id": provider_id, **extra})
    for entry in providers:
        if entry["id"] in NOTES:
            entry["note"] = NOTES[entry["id"]]
        if entry["id"] in POPULAR:
            entry["popular"] = POPULAR.index(entry["id"]) + 1
    providers.sort(key=lambda item: (item.get("popular") or 99, item["name"].casefold()))
    return {
        "source": "https://models.dev",
        "updated": datetime.now(timezone.utc).date().isoformat(),
        "model_fields": ["id", "name", "flags", "family", "released"],
        "flags": {"vision": FLAG_VISION, "reasoning": FLAG_REASONING, "free": FLAG_FREE},
        "providers": providers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="models.dev api.json path or URL")
    parser.add_argument("--output", default=str(OUTPUT))
    args = parser.parse_args(argv)
    result = build(load_source(args.source))
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    models = sum(len(item["models"]) for item in result["providers"])
    print(f"{len(result['providers'])} providers, {models} models -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
