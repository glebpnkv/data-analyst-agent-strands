#!/usr/bin/env bash
# Run the Chainlit frontend locally for development.
#
# Usage: ./scripts/run_frontend_local.sh [chainlit args...]
# Example: ./scripts/run_frontend_local.sh --port 8501

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${REPO_ROOT}/frontend"

if [[ ! -f "${FRONTEND_DIR}/app.py" ]]; then
  echo "ERROR: ${FRONTEND_DIR}/app.py not found" >&2
  exit 1
fi

# Chainlit refuses to boot without an auth secret in its config dir. For
# local dev we synthesize a stable per-user one if the env var is empty.
if [[ -z "${CHAINLIT_AUTH_SECRET:-}" ]]; then
  export CHAINLIT_AUTH_SECRET="local-dev-$(whoami)"
fi

cd "${FRONTEND_DIR}"
exec uv run chainlit run app.py \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8000}" \
  "$@"
