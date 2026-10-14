#!/usr/bin/env bash
set -e

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

if [[ "${1:-}" != "--web" ]]; then
    if ! command -v npm &> /dev/null; then
        echo "[ERROR] Node.js is needed for source development. Use a desktop package instead."
        exit 1
    fi
    if [[ ! -d node_modules/electron ]]; then npm ci; fi
    if [[ ! -x .venv/bin/python ]]; then POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --no-root --quiet; fi
    exec npm run desktop -- "$@"
fi
shift

if ! command -v poetry &> /dev/null; then
    echo "[ERROR] Poetry not found. Install: pip install poetry"
    exit 1
fi

echo "Installing dependencies..."
poetry install --no-root --quiet

echo "Starting VEIL..."
exec poetry run python -m app.main "$@"
