#!/usr/bin/env bash
# Run the agent FastAPI service locally for development.
#
# Usage: ./scripts/run_agent_local.sh [uvicorn args...]
# Example: ./scripts/run_agent_local.sh --port 18080
#
# Sets PYTHONPATH so both `agent_server` (repo root) and the agent's
# sibling modules (`agent`, `server`) resolve correctly.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="${REPO_ROOT}/agent"

if [[ ! -f "${AGENT_DIR}/server/main.py" ]]; then
  echo "ERROR: ${AGENT_DIR}/server/main.py not found." >&2
  exit 1
fi

export PYTHONPATH="${REPO_ROOT}:${AGENT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

exec uv run uvicorn server.main:app \
  --app-dir "${AGENT_DIR}" \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8080}" \
  --workers 1 \
  "$@"
