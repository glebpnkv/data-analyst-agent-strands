#!/usr/bin/env bash
# First-time deploy bootstrap.
#
# Solves the chicken-and-egg between the Ecr stack (creates the ECR
# repos) and the Compute stack (creates ECS services that pull
# `:latest` from those repos and block CFN until the service reaches
# steady state). Pushing images BEFORE Compute is created avoids the
# stuck-CFN deadlock.
#
# Three phases:
#   1. Deploy the prerequisite stacks: Network, Data, Ecr, Auth.
#      This creates the ECR repos so we can push images.
#   2. Build + push agent and frontend images via deploy.sh. Compute
#      services don't exist yet, so deploy.sh runs in push-only mode
#      automatically (no ECS rollout).
#   3. Deploy the Compute stack. ECS services come up with images
#      already in ECR, so they reach steady state on the first pull
#      and CFN finishes cleanly.
#
# After this completes, all subsequent rollouts are just:
#   ./scripts/deploy.sh agent | frontend | all
#
# Idempotent: safe to re-run after a failed bootstrap. Steps 1 and 3
# are no-ops when the stacks are already up to date; step 2 always
# rebuilds + pushes (cheap with Docker layer cache).
#
# Env knobs:
#   STAGE        default Dev   (must match the CDK stage)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_DIR="${REPO_ROOT}/infra"
SCRIPTS_DIR="${REPO_ROOT}/scripts"
STAGE="${STAGE:-Dev}"
PREFIX="DataAnalystAgent"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 1
  fi
}
require_command cdk
require_command aws
require_command docker
require_command git

echo "==> [1/3] Deploying prerequisite stacks (Network, Data, Ecr, Auth)..."
echo "    Compute is intentionally excluded — its ECS services would"
echo "    block CFN waiting for tasks to start while ECR is empty."
cd "${INFRA_DIR}"
cdk deploy \
  "${PREFIX}-Network-${STAGE}" \
  "${PREFIX}-Data-${STAGE}" \
  "${PREFIX}-Ecr-${STAGE}" \
  "${PREFIX}-Auth-${STAGE}" \
  --require-approval never
cd - >/dev/null

echo
echo "==> [2/3] Building + pushing initial agent + frontend images..."
echo "    deploy.sh detects that the Compute stack isn't deployed yet"
echo "    (no cluster/service-name SSM params) and runs in push-only"
echo "    mode — no ECS rollout, just docker build + push."
"${SCRIPTS_DIR}/deploy.sh" all

echo
echo "==> [3/3] Deploying Compute stack..."
echo "    ECS services pull :latest on first task launch; the images"
echo "    are already in ECR so the service reaches steady state on"
echo "    the first try and CFN finishes without hanging."
cd "${INFRA_DIR}"
cdk deploy "${PREFIX}-Compute-${STAGE}" --require-approval never
cd - >/dev/null

cat <<'EOF'

[OK] Bootstrap complete.

Next steps:
  - Add yourself as a Cognito user (admins invite by email):
      aws cognito-idp admin-create-user \
        --user-pool-id "$(aws ssm get-parameter \
          --name /data-analyst-agent/dev/cognito/user-pool-id \
          --query Parameter.Value --output text)" \
        --username <your-email> \
        --user-attributes Name=email,Value=<your-email> Name=email_verified,Value=true \
        --desired-delivery-mediums EMAIL
  - Visit https://<domain_name> (the value pinned in infra/cdk.json).
  - Future rollouts: ./scripts/deploy.sh agent | frontend | all
EOF
