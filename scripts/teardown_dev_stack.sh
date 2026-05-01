#!/usr/bin/env bash
# Tear down every CDK stack belonging to the DataAnalystAgent dev environment.
#
# Use this whenever you want to stop paying for the deployed dev stack —
# NAT gateway (~$32/mo), RDS instance, ALBs, etc. all go away. Existing
# Postgres data and ECR images go with them by default; this is dev,
# not prod.
#
# What this DOESN'T touch:
#   - The CDK bootstrap stack (CDKToolkit) — leave alone unless you're
#     decommissioning the AWS account.
#   - Resources outside the DataAnalystAgent-* prefix.
#   - CloudWatch log groups (they have their own retention; cheap to leave).
#
# Re-deploying after a teardown is the same as a clean first deploy:
# run `./scripts/bootstrap.sh`. It sequences the ECR-then-images-then-
# Compute steps so CFN doesn't hang on the empty-ECR chicken-and-egg.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_DIR="${REPO_ROOT}/infra"
STACK_PREFIX="${STACK_PREFIX:-DataAnalystAgent-}"
STAGE="${STAGE:-Dev}"
REGION="${CDK_DEFAULT_REGION:-eu-central-1}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 1
  fi
}
require_command cdk
require_command aws

echo "Region: ${REGION}"
echo "Looking for live CloudFormation stacks named ${STACK_PREFIX}*-${STAGE}..."

# Live stacks (exclude DELETE_COMPLETE). Newest-first ordering doesn't
# matter — `cdk destroy --all` resolves the right teardown order itself.
LIVE_STACKS="$(
  aws cloudformation list-stacks \
    --region "${REGION}" \
    --stack-status-filter \
        CREATE_COMPLETE CREATE_IN_PROGRESS \
        UPDATE_COMPLETE UPDATE_IN_PROGRESS \
        UPDATE_ROLLBACK_COMPLETE ROLLBACK_COMPLETE \
        UPDATE_ROLLBACK_FAILED ROLLBACK_FAILED \
    --query "StackSummaries[?starts_with(StackName, \`${STACK_PREFIX}\`) && ends_with(StackName, \`-${STAGE}\`)].StackName" \
    --output text 2>/dev/null || true
)"

if [[ -z "${LIVE_STACKS// /}" ]]; then
  echo "No live ${STACK_PREFIX}*-${STAGE} stacks found in ${REGION}. Nothing to do."
  exit 0
fi

echo "About to destroy:"
for s in ${LIVE_STACKS}; do
  echo "  - ${s}"
done
echo
echo "This will permanently delete the resources in those stacks (NAT GW,"
echo "RDS instance and all its data, ALBs, ECS services, etc)."
read -r -p "Type 'yes' to proceed: " confirm
if [[ "${confirm}" != "yes" ]]; then
  echo "Aborted."
  exit 1
fi

cd "${INFRA_DIR}"
cdk destroy --all --force

echo
echo "Verifying nothing remains..."
REMAINING="$(
  aws cloudformation list-stacks \
    --region "${REGION}" \
    --stack-status-filter \
        CREATE_COMPLETE UPDATE_COMPLETE ROLLBACK_COMPLETE \
        UPDATE_ROLLBACK_COMPLETE \
    --query "StackSummaries[?starts_with(StackName, \`${STACK_PREFIX}\`) && ends_with(StackName, \`-${STAGE}\`)].StackName" \
    --output text 2>/dev/null || true
)"

if [[ -z "${REMAINING// /}" ]]; then
  echo "All ${STACK_PREFIX}*-${STAGE} stacks are gone."
else
  echo "WARNING: these stacks still exist:" >&2
  for s in ${REMAINING}; do echo "  - ${s}" >&2; done
  echo "Inspect them in the CloudFormation console and clean up by hand if needed." >&2
  exit 1
fi

cat <<'EOF'

Worth a glance to be sure your bill stops:
  - https://console.aws.amazon.com/cost-management/home  (filter by tag Project=DataAnalystAgent)
  - ECR repos: any retained images you may want to delete by hand
  - CloudWatch log groups: don't cost much but linger; delete if pedantic
  - RDS automated/manual snapshots: only exist if you took them; check the RDS console

When you're ready to redeploy, run ./scripts/bootstrap.sh — same flow
as a clean first deploy.
EOF
