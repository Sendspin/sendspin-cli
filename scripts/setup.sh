#!/usr/bin/env bash
# Setups the development environment.

# Stop on errors
set -e

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: 'uv' is not installed or not on PATH." >&2
    echo "Install it from https://github.com/astral-sh/uv and rerun this script." >&2
    exit 1
fi

echo "Installing development dependencies..."
uv sync --extra test

echo "Installing pre-commit hooks..."
uv run pre-commit install
