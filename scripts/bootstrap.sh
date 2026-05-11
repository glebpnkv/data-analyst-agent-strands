#!/usr/bin/env bash
# First-time deploy bootstrap.
#
# Solves the chicken-and-egg between the Ecr stack (creates the ECR
# repos) and the Compute stack (creates ECS services that pull
# `:latest` from those repos and block CFN until the service reaches
# steady state). Pushing images BEFORE Compute is created avoids the
# stuck-CFN deadlock.
#
# Four phases:
#   1. Enable ENI trunking at the account level. Required for our
#      ASG sizing (t3.medium needs trunking to host >2 awsvpc tasks).
#      One-time per account/region; idempotent.
#   2. Deploy the prerequisite stacks: Network, Data, Ecr, Auth.
#      This creates the ECR repos so we can push images.
#   3. Build + push agent, frontend, AND sandbox images. Compute
#      services don't exist yet, so deploy.sh runs in push-only mode
#      automatically (no ECS rollout). Sandbox has no service ever,
#      so deploy_sandbox.sh always just builds and pushes.
#   4. Deploy the Compute stack. ECS services come up with images
#      already in ECR, so they reach steady state on the first pull
#      and CFN finishes cleanly.
#
# After this completes, all subsequent rollouts are just:
#   ./scripts/deploy.sh agent | frontend | all
#   ./scripts/deploy_sandbox.sh
#
# Idempotent: safe to re-run after a failed bootstrap. Steps 1, 2, 4
# are no-ops when already up to date; step 3 always rebuilds + pushes
# (cheap with Docker layer cache).
#
# Env knobs:
#   STAGE        default Dev   (must match the CDK stage)
#   AWS_REGION   default eu-central-1   (used for the ENI-trunking call)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_DIR="${REPO_ROOT}/infra"
SCRIPTS_DIR="${REPO_ROOT}/scripts"
STAGE="${STAGE:-Dev}"
REGION="${AWS_REGION:-eu-central-1}"
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

echo "==> [1/4] Enabling awsvpcTrunking at account level (region=${REGION})..."
echo "    Lets t3.medium hosts run >2 awsvpc-mode tasks each. The setting"
echo "    is account-wide (per region) and idempotent — re-running the"
echo "    bootstrap won't disturb anything."
# Errors here are non-fatal — the most common case is a benign "already
# enabled". `aws ecs put-account-setting` is idempotent and returns the
# new state on success; if it fails for a real reason (e.g. no IAM
# permission), the Compute deploy will surface it later as RESOURCE:ENI
# RunTask failures.
aws ecs put-account-setting \
  --region "${REGION}" \
  --name awsvpcTrunking \
  --value enabled \
  --output text >/dev/null \
  || echo "    [warn] put-account-setting awsvpcTrunking failed; continuing."

echo
echo "==> [2/4] Deploying prerequisite stacks (Network, Data, Ecr, Auth)..."
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
echo "==> [3/4] Building + pushing initial agent, frontend, sandbox images..."
echo "    deploy.sh detects that the Compute stack isn't deployed yet"
echo "    (no cluster/service-name SSM params) and runs in push-only"
echo "    mode for agent/frontend. deploy_sandbox.sh always pushes only."
"${SCRIPTS_DIR}/deploy.sh" all
"${SCRIPTS_DIR}/deploy_sandbox.sh"

echo
echo "==> [4/4] Deploying Compute stack..."
echo "    ECS services pull :latest on first task launch; the images"
echo "    are already in ECR so the service reaches steady state on"
echo "    the first try and CFN finishes without hanging. Sandbox task"
echo "    definitions are registered but no sandbox tasks run until"
echo "    the agent's pool warms them on first chat session."
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
  - Future rollouts:
      ./scripts/deploy.sh agent | frontend | all
      ./scripts/deploy_sandbox.sh
EOF
