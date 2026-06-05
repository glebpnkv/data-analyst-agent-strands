#!/usr/bin/env bash
# Create the Phoenix logical database on the deployed RDS instance.
#
# Why this script: RDS doesn't expose `CREATE DATABASE` as IaC, so we
# need to run a single SQL statement against the running cluster once
# per environment. The frontend ECS task already has DB credentials,
# boto3, and SQLAlchemy/asyncpg in its image — perfect host for the
# one-shot bootstrap. We just `aws ecs execute-command` into a running
# task and invoke `scripts/bootstrap_phoenix_db.py`.
#
# Pre-reqs:
#   - First Compute deploy has succeeded (cluster + frontend running).
#   - The frontend image includes /app/bootstrap_phoenix_db.py — i.e.
#     deploy.sh has built the image with the M1 commit or later.
#   - AWS CLI + Session Manager plugin installed locally.
#
# Idempotent: re-run is safe; "database already exists" is treated as
# success.
#
# Env knobs:
#   STAGE        default Dev
#   AWS_REGION   default eu-central-1

set -euo pipefail

STAGE="${STAGE:-Dev}"
REGION="${AWS_REGION:-eu-central-1}"
STAGE_LOWER="$(echo "${STAGE}" | tr 'A-Z' 'a-z')"
SSM_PREFIX="/data-analyst-agent/${STAGE_LOWER}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 1
  fi
}
require_command aws
require_command jq

# Resolve cluster + frontend service name from SSM (set by the compute stack).
CLUSTER_NAME=$(aws ssm get-parameter \
  --region "${REGION}" \
  --name "${SSM_PREFIX}/cluster-name" \
  --query 'Parameter.Value' --output text)

FRONTEND_SERVICE=$(aws ssm get-parameter \
  --region "${REGION}" \
  --name "${SSM_PREFIX}/frontend/service-name" \
  --query 'Parameter.Value' --output text)

echo "==> Resolved cluster=${CLUSTER_NAME}, frontend service=${FRONTEND_SERVICE}"

# Pick the first RUNNING frontend task.
TASK_ARN=$(aws ecs list-tasks \
  --region "${REGION}" \
  --cluster "${CLUSTER_NAME}" \
  --service-name "${FRONTEND_SERVICE}" \
  --desired-status RUNNING \
  --query 'taskArns[0]' --output text)

if [[ -z "${TASK_ARN}" || "${TASK_ARN}" == "None" ]]; then
  echo "ERROR: no RUNNING frontend task found in service ${FRONTEND_SERVICE}." >&2
  echo "Deploy the Compute stack and wait for the frontend service to stabilise first." >&2
  exit 1
fi

echo "==> Using frontend task: ${TASK_ARN}"
echo "==> Running CREATE DATABASE phoenix via execute-command (idempotent)…"

# `aws ecs execute-command` needs the Session Manager plugin installed
# locally; it will print a clear error if missing. Output streams back
# to this shell so we see CREATE DATABASE / "already exists" messages.
aws ecs execute-command \
  --region "${REGION}" \
  --cluster "${CLUSTER_NAME}" \
  --task "${TASK_ARN}" \
  --container frontend \
  --interactive \
  --command "python /app/bootstrap_phoenix_db.py"

echo
echo "[OK] Phoenix DB bootstrap complete. The Phoenix service can now finish migrations."
