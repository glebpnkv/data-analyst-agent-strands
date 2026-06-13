#!/usr/bin/env bash
# First-time deploy bootstrap.
#
# Solves two chicken-and-egg problems:
#
# (1) Ecr <-> Compute. The Ecr stack creates the repos, then Compute
#     creates ECS services that pull `:latest` from those repos and
#     block CFN until the service reaches steady state. Images must
#     be in ECR before Compute is created, otherwise CFN deadlocks.
#
# (2) Phoenix <-> RDS. The Phoenix container expects a logical DB named
#     `phoenix` on the existing RDS instance. RDS doesn't expose
#     CREATE DATABASE as IaC. If Phoenix's service comes up before
#     that DB exists it crashloops, and CFN waits ~30 min for steady
#     state before rolling back. We sidestep this by deploying Compute
#     once with `phoenix_desired_count=0` (service exists, no tasks),
#     creating the DB via `bootstrap_phoenix_db.sh`, then redeploying
#     with the default (1) so Phoenix starts against the now-existing
#     DB and reaches steady state on first try.
#
# Six phases:
#   1. Enable ENI trunking at the account level. Required for our
#      ASG sizing (t3.medium needs trunking to host >2 awsvpc tasks).
#      One-time per account/region; idempotent.
#   2. Deploy the prerequisite stacks: Network, Data, Ecr, Auth.
#      This creates the ECR repos so we can push images.
#   3. Build + push agent, frontend, AND sandbox images. Compute
#      services don't exist yet, so deploy.sh runs in push-only mode
#      automatically (no ECS rollout). Sandbox has no service ever,
#      so deploy_sandbox.sh always just builds and pushes.
#   4. Deploy the Compute stack with phoenix_desired_count=0. ECS
#      services come up with images already in ECR. Agent + Frontend
#      reach steady state immediately. Phoenix service is registered
#      but has no tasks running, so its (missing) DB doesn't block CFN.
#   5. Bootstrap the Phoenix logical DB. The script runs CREATE DATABASE
#      via `aws ecs execute-command` into the now-running frontend
#      task (only task we have with both Postgres deps and RDS SG
#      ingress). Idempotent.
#   6. Re-deploy the Compute stack with the default phoenix_desired_count
#      (=1). ECS scales Phoenix from 0 to 1; the new task connects to
#      the just-created DB, runs Alembic migrations, and reaches
#      steady state.
#
# After this completes, subsequent rollouts are just:
#   ./scripts/deploy.sh agent | frontend | all
#   ./scripts/deploy_sandbox.sh
#   cdk deploy DataAnalystAgent-Compute-Dev   (for infra-only changes)
#
# Idempotent: safe to re-run after a failed bootstrap. CDK deploys are
# no-ops when up to date; the DB bootstrap script treats
# "already exists" as success; image rebuilds are cheap with the
# Docker layer cache.
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
echo "==> [3/6] Building + pushing initial agent, frontend, sandbox images..."
echo "    deploy.sh detects that the Compute stack isn't deployed yet"
echo "    (no cluster/service-name SSM params) and runs in push-only"
echo "    mode for agent/frontend. deploy_sandbox.sh always pushes only."
"${SCRIPTS_DIR}/deploy.sh" all
"${SCRIPTS_DIR}/deploy_sandbox.sh"

echo
echo "==> [4/6] Deploying Compute stack with Phoenix held at desired_count=0..."
echo "    Phoenix's logical DB doesn't exist on RDS yet. If we let the"
echo "    Phoenix service try to start now, it would crashloop on"
echo "    'database \"phoenix\" does not exist' and CFN would wait up to"
echo "    ~30 min for steady state. Bringing the service up empty avoids"
echo "    that — agent + frontend reach steady state cleanly; Phoenix"
echo "    has no tasks running so its missing DB doesn't block CFN."
cd "${INFRA_DIR}"
cdk deploy "${PREFIX}-Compute-${STAGE}" \
  --context phoenix_desired_count=0 \
  --require-approval never
cd - >/dev/null

echo
echo "==> [5/6] Creating the Phoenix logical database on RDS..."
echo "    Runs scripts/bootstrap_phoenix_db.py via 'aws ecs execute-command'"
echo "    inside the running frontend task (the only task with both"
echo "    Postgres deps and RDS SG ingress). Idempotent: re-runs are"
echo "    a no-op if the database already exists."
"${SCRIPTS_DIR}/bootstrap_phoenix_db.sh"

echo
echo "==> [6/6] Re-deploying Compute stack to scale Phoenix to desired_count=1..."
echo "    No context flag this time, so phoenix_desired_count defaults"
echo "    to 1. ECS launches a Phoenix task; it connects to the"
echo "    just-created DB, runs Alembic migrations (~60s), and the"
echo "    service reaches steady state. CFN finishes."
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
