#!/usr/bin/env bash
# Bring up the full local stack: Phoenix (traces UI) + agent service + Chainlit.
#
# Usage:
#   ./scripts/run_local_stack.sh
#
# Environment overrides:
#   PHOENIX_PORT   default 6006   (Phoenix UI + OTLP HTTP receiver)
#   AGENT_PORT     default 8080   (FastAPI agent service)
#   FRONTEND_PORT  default 8000   (Chainlit)
#   SKIP_PHOENIX=1 to skip starting/managing Phoenix (e.g. if running it elsewhere)
#
# Ctrl-C stops everything.

set -euo pipefail

PHOENIX_PORT="${PHOENIX_PORT:-6006}"
AGENT_PORT="${AGENT_PORT:-8080}"
FRONTEND_PORT="${FRONTEND_PORT:-8000}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
PHOENIX_CONTAINER_NAME="phoenix-agent-traces"
PHOENIX_VOLUME_NAME="${PHOENIX_VOLUME_NAME:-phoenix-agent-traces-data}"
POSTGRES_CONTAINER_NAME="chainlit-postgres"
POSTGRES_VOLUME_NAME="${POSTGRES_VOLUME_NAME:-chainlit-postgres-data}"
POSTGRES_DB="${POSTGRES_DB:-chainlit}"
POSTGRES_USER="${POSTGRES_USER:-chainlit}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-chainlit}"
SKIP_PHOENIX="${SKIP_PHOENIX:-0}"
SKIP_POSTGRES="${SKIP_POSTGRES:-0}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_LOG_DIR="${REPO_ROOT}/runs/local-stack"
mkdir -p "${STACK_LOG_DIR}"

AGENT_LOG="${STACK_LOG_DIR}/agent-stdout.log"
PHOENIX_LOG="${STACK_LOG_DIR}/phoenix-stdout.log"
CHAINLIT_SCHEMA_FILE="${REPO_ROOT}/scripts/local_stack/chainlit_schema.sql"

AGENT_PID=""

cleanup() {
  echo
  echo "Stopping local stack..."
  if [[ -n "${AGENT_PID}" ]] && kill -0 "${AGENT_PID}" 2>/dev/null; then
    echo "  - stopping agent (pid=${AGENT_PID})"
    kill "${AGENT_PID}" 2>/dev/null || true
    wait "${AGENT_PID}" 2>/dev/null || true
  fi
  # Catch any orphaned uvicorn workers spawned by run_agent_local.sh.
  pkill -f "uvicorn server.main:app" 2>/dev/null || true

  if [[ "${SKIP_PHOENIX}" != "1" ]] && docker ps -q --filter "name=${PHOENIX_CONTAINER_NAME}" | grep -q .; then
    echo "  - stopping phoenix container"
    docker stop "${PHOENIX_CONTAINER_NAME}" >/dev/null 2>&1 || true
  fi
  if [[ "${SKIP_POSTGRES}" != "1" ]] && docker ps -q --filter "name=${POSTGRES_CONTAINER_NAME}" | grep -q .; then
    echo "  - stopping postgres container"
    docker stop "${POSTGRES_CONTAINER_NAME}" >/dev/null 2>&1 || true
  fi
  echo "Stack stopped."
}
trap cleanup EXIT INT TERM

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 1
  fi
}

wait_for_http() {
  local label="$1" url="$2" attempts=60
  printf "Waiting for %s at %s" "${label}" "${url}"
  for ((i = 0; i < attempts; i++)); do
    if curl -fsS -o /dev/null --max-time 2 "${url}"; then
      printf " ready\n"
      return 0
    fi
    printf "."
    sleep 1
  done
  printf " timeout\n"
  return 1
}

# --- Phoenix ---------------------------------------------------------------

if [[ "${SKIP_PHOENIX}" != "1" ]]; then
  require_command docker

  if docker ps -aq --filter "name=${PHOENIX_CONTAINER_NAME}" | grep -q .; then
    echo "Removing stale phoenix container ${PHOENIX_CONTAINER_NAME}..."
    docker rm -f "${PHOENIX_CONTAINER_NAME}" >/dev/null
  fi

  echo "Starting phoenix on :${PHOENIX_PORT} (container=${PHOENIX_CONTAINER_NAME}, volume=${PHOENIX_VOLUME_NAME})..."
  : > "${PHOENIX_LOG}"
  # Named volume mounted at /root/.phoenix persists Phoenix's SQLite DB
  # across container restarts so traces aren't lost when the stack
  # stops. To wipe history, run `docker volume rm ${PHOENIX_VOLUME_NAME}`.
  docker run -d --name "${PHOENIX_CONTAINER_NAME}" \
    -p "${PHOENIX_PORT}:6006" \
    -v "${PHOENIX_VOLUME_NAME}:/root/.phoenix" \
    arizephoenix/phoenix:latest >/dev/null

  # Phoenix doesn't expose a /healthz endpoint reliably across versions;
  # the UI root returning 200 is good enough for "ready".
  if ! wait_for_http "phoenix" "http://127.0.0.1:${PHOENIX_PORT}/"; then
    echo "ERROR: phoenix did not come up; container logs:" >&2
    docker logs "${PHOENIX_CONTAINER_NAME}" >&2 || true
    exit 1
  fi

  # Strands uses opentelemetry-exporter-otlp HTTP/protobuf transport.
  # Phoenix exposes that on the same port as the UI, path /v1/traces.
  export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:${PHOENIX_PORT}"
  export AGENT_OTLP_ENABLE=1
else
  echo "SKIP_PHOENIX=1 — using whatever OTEL_EXPORTER_OTLP_ENDPOINT / AGENT_OTLP_ENABLE you set."
fi

# --- Postgres (Chainlit data layer) ----------------------------------------

if [[ "${SKIP_POSTGRES}" != "1" ]]; then
  require_command docker

  if docker ps -aq --filter "name=${POSTGRES_CONTAINER_NAME}" | grep -q .; then
    echo "Removing stale postgres container ${POSTGRES_CONTAINER_NAME}..."
    docker rm -f "${POSTGRES_CONTAINER_NAME}" >/dev/null
  fi

  echo "Starting postgres on :${POSTGRES_PORT} (container=${POSTGRES_CONTAINER_NAME}, volume=${POSTGRES_VOLUME_NAME})..."
  # Named volume mounted at /var/lib/postgresql/data persists threads,
  # steps, and elements across restarts. To wipe history, run
  # `docker volume rm ${POSTGRES_VOLUME_NAME}`.
  docker run -d --name "${POSTGRES_CONTAINER_NAME}" \
    -p "${POSTGRES_PORT}:5432" \
    -v "${POSTGRES_VOLUME_NAME}:/var/lib/postgresql/data" \
    -e "POSTGRES_DB=${POSTGRES_DB}" \
    -e "POSTGRES_USER=${POSTGRES_USER}" \
    -e "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" \
    postgres:16 >/dev/null

  # Wait for the server to accept connections (pg_isready ships in the
  # postgres image).
  echo -n "Waiting for postgres"
  for ((i = 0; i < 60; i++)); do
    if docker exec "${POSTGRES_CONTAINER_NAME}" pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -q 2>/dev/null; then
      echo " ready"
      break
    fi
    printf "."
    sleep 1
  done
  if ! docker exec "${POSTGRES_CONTAINER_NAME}" pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -q 2>/dev/null; then
    echo
    echo "ERROR: postgres did not come up; container logs:" >&2
    docker logs "${POSTGRES_CONTAINER_NAME}" >&2 || true
    exit 1
  fi

  if [[ ! -f "${CHAINLIT_SCHEMA_FILE}" ]]; then
    echo "ERROR: chainlit schema file not found: ${CHAINLIT_SCHEMA_FILE}" >&2
    exit 1
  fi

  echo "Applying chainlit schema (idempotent)..."
  # Pipe the SQL in via stdin so it works even when the file isn't
  # mounted into the container.
  docker exec -i \
    -e PGPASSWORD="${POSTGRES_PASSWORD}" \
    "${POSTGRES_CONTAINER_NAME}" \
    psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -v ON_ERROR_STOP=1 -q \
    < "${CHAINLIT_SCHEMA_FILE}" \
    >/dev/null

  # Chainlit's SQLAlchemyDataLayer wants asyncpg-style URLs.
  export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT}/${POSTGRES_DB}"
else
  echo "SKIP_POSTGRES=1 — using whatever DATABASE_URL you set."
fi

# --- Agent service ---------------------------------------------------------

require_command uv

echo "Starting agent service on :${AGENT_PORT} (logs=${AGENT_LOG})..."
: > "${AGENT_LOG}"
PORT="${AGENT_PORT}" "${REPO_ROOT}/scripts/run_agent_local.sh" \
  >"${AGENT_LOG}" 2>&1 &
AGENT_PID=$!

if ! wait_for_http "agent" "http://127.0.0.1:${AGENT_PORT}/healthz"; then
  echo "ERROR: agent did not come up; recent log lines:" >&2
  tail -40 "${AGENT_LOG}" >&2 || true
  exit 1
fi

# --- Frontend (foreground) -------------------------------------------------

# Tell Chainlit where the agent lives (overrides anything in frontend/.env).
export AGENT_BASE_URL="http://127.0.0.1:${AGENT_PORT}"

cat <<EOF

Local stack is up.
  Chainlit (chat UI):     http://127.0.0.1:${FRONTEND_PORT}
EOF

if [[ "${SKIP_PHOENIX}" != "1" ]]; then
  cat <<EOF
  Phoenix (traces UI):    http://127.0.0.1:${PHOENIX_PORT}
EOF
fi

cat <<EOF
  Agent API:              http://127.0.0.1:${AGENT_PORT}
  Agent logs (tail -f):   ${AGENT_LOG}
EOF

if [[ "${SKIP_POSTGRES}" != "1" ]]; then
  cat <<EOF
  Postgres:               postgres://${POSTGRES_USER}:***@127.0.0.1:${POSTGRES_PORT}/${POSTGRES_DB}
EOF
fi

if [[ "${SKIP_PHOENIX}" != "1" ]]; then
  cat <<EOF
  Phoenix logs:           docker logs -f ${PHOENIX_CONTAINER_NAME}
EOF
fi

cat <<EOF

Frontend logs follow. Ctrl-C to stop everything.
EOF
echo

PORT="${FRONTEND_PORT}" "${REPO_ROOT}/scripts/run_frontend_local.sh"
