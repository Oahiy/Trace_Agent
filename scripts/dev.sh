#!/usr/bin/env bash
# Run from any directory; all disposable development data stays under .cache/.
set -euo pipefail
umask 077
TRACEAGENT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TRACEAGENT_CACHE="$TRACEAGENT_ROOT/.cache"
export UV_CACHE_DIR="$TRACEAGENT_CACHE/uv"
export UV_PYTHON_INSTALL_DIR="$TRACEAGENT_CACHE/uv-python"
export RUFF_CACHE_DIR="$TRACEAGENT_CACHE/ruff"
export PYTHONPYCACHEPREFIX="$TRACEAGENT_CACHE/python"
export TMPDIR="$TRACEAGENT_CACHE/tmp"
export XDG_CACHE_HOME="$TRACEAGENT_CACHE/xdg"
export HOMEBREW_CACHE="$TRACEAGENT_CACHE/homebrew"
export HOMEBREW_TEMP="$TMPDIR"
export HOMEBREW_LOGS="$TRACEAGENT_CACHE/homebrew/logs"
export HOMEBREW_NO_AUTO_UPDATE=1
export COLIMA_HOME="$TRACEAGENT_CACHE/colima"
export LIMA_HOME="$TRACEAGENT_CACHE/lima"
export DOCKER_CONFIG="$TRACEAGENT_CACHE/docker"
export OLLAMA_MODELS="$TRACEAGENT_CACHE/ollama/models"
export OLLAMA_HOST="127.0.0.1:11434"
export OLLAMA_NO_CLOUD=1
mkdir -p "$UV_CACHE_DIR" "$TMPDIR" "$XDG_CACHE_HOME" "$DOCKER_CONFIG" "$TRACEAGENT_CACHE/day1" "$TRACEAGENT_CACHE/day2" "$TRACEAGENT_CACHE/day3" "$TRACEAGENT_CACHE/pytest" "$RUFF_CACHE_DIR" "$COLIMA_HOME" "$LIMA_HOME"
mkdir -p "$TRACEAGENT_CACHE/day4/uploads"
mkdir -p "$TRACEAGENT_CACHE/day5"
mkdir -p "$TRACEAGENT_CACHE/day6"
chmod 700 "$TRACEAGENT_CACHE"
cat > "$TRACEAGENT_CACHE/manifest.$$.tmp" <<'JSON'
{
  "root": ".cache/",
  "manager": "scripts/dev.sh",
  "uv": "uv/",
  "uv_managed_python": "uv-python/",
  "python_bytecode": "python/",
  "pytest": "pytest/",
  "ruff": "ruff/",
  "temporary_files": "tmp/",
  "xdg_cache": "xdg/",
  "homebrew_downloads_and_logs": "homebrew/",
  "container_vm": ["colima/", "lima/"],
  "docker_client": "docker/",
  "local_models": "ollama/models/",
  "day1_downloads_logs_and_reports": "day1/",
  "day2_downloads_logs_reports_and_isolated_environment": "day2/",
  "day3_database_env_logs_reports_and_isolated_environment": "day3/",
  "day4_logs_reports_and_isolated_environment": "day4/",
  "day4_isolated_environment": "day4/ci-env/",
  "day5_reports_fixtures_and_pdf_previews": "day5/",
  "day5_isolated_environment": "day5/ci-env/",
  "day5_failure_fixture_uploads": {"path": "day5/failure-uploads/", "database": "traceagent_test", "disposable_with_test_database": true},
  "day6_queue_credentials_logs_reports_and_isolated_environment": "day6/",
  "day6_queue_credentials": {"path": "day6/queue.env", "contains_secrets": true, "disposable_with_existing_redis_volume": false},
  "day6_demo_uploads": {"path": "day6/uploads/", "database": "traceagent_test", "disposable_with_test_database": true},
  "day6_redis_volume": {"name": "traceagent-day6-redis", "location": "Docker volume inside the managed Colima VM", "recovery_source": "PostgreSQL parse_tasks; retain credentials with volume"},
  "runtime_ipc": {"location": "OS temporary Colima/Lima sockets", "lifecycle": "released when local VM and services stop"},
  "upload_files": {"path": "day4/uploads/", "disposable_with_existing_database": false, "sharing": "Only this directory is mounted into Colima; consumers bind it at /data/uploads"},
  "api_key_credentials": {"path": "day4/auth.env", "contains_secrets": true, "disposable_with_existing_database": false},
  "database_volume": {"name": "traceagent-day3-postgres", "location": "Docker volume inside the managed Colima VM", "disposable": false},
  "database_credentials": {"path": "day3/database.env", "contains_secrets": true, "disposable_with_existing_volume": false},
  "cleanup": "Stop local model and container services before deleting their cache. Preserve database credentials, API keys and referenced upload files while retaining the database volume. Preserve docs verification records; recreate downloads and environments through documented commands."
}
JSON
mv "$TRACEAGENT_CACHE/manifest.$$.tmp" "$TRACEAGENT_CACHE/manifest.json"
if [[ ! -f "$DOCKER_CONFIG/config.json" ]]; then
cat > "$DOCKER_CONFIG/config.$$.tmp" <<'JSON'
{"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]}
JSON
mv "$DOCKER_CONFIG/config.$$.tmp" "$DOCKER_CONFIG/config.json"
fi
cd "$TRACEAGENT_ROOT"
exec "$@"
