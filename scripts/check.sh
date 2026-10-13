#!/usr/bin/env bash
# The same service-independent checks run locally and in CI.
set -euo pipefail
TRACEAGENT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TRACEAGENT_ROOT"
export TRACEAGENT_CONTAINER_TESTS=0
export TRACEAGENT_DATABASE_TESTS=0
bash scripts/dev.sh uv run --locked ruff check app scripts tests migrations
bash scripts/dev.sh uv run --locked ruff format --check app scripts tests migrations
bash scripts/dev.sh uv run --locked python -m pytest -q --basetemp=.cache/pytest/tmp --junitxml=.cache/pytest/tests.xml
