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

# Sandbox tasks are launched standalone via ecs:RunTask (NOT as part
# of an ECS service), so CFN doesn't know about them. If any are still
# running when we destroy the cluster, the deletion can hang. Stop
# them defensively before cdk destroy.
#
# We look up the cluster name from SSM rather than CFN outputs so this
# step still works after a previous half-failed teardown (where the
# Compute stack might be partially gone but tasks linger).
SANDBOX_CLUSTER="$(
  aws ssm get-parameter \
    --region "${REGION}" \
    --name "/data-analyst-agent/$(echo "${STAGE}" | tr '[:upper:]' '[:lower:]')/cluster-name" \
    --query 'Parameter.Value' --output text 2>/dev/null || true
)"
if [[ -n "${SANDBOX_CLUSTER}" ]]; then
  echo "Stopping any leftover sandbox tasks in cluster ${SANDBOX_CLUSTER}..."
  # `--family` filter narrows by task definition family. The `|| true`
  # handles the case where the family no longer exists (already torn down).
  SANDBOX_TASKS="$(
    aws ecs list-tasks \
      --region "${REGION}" \
      --cluster "${SANDBOX_CLUSTER}" \
      --family "DataAnalystSandbox-${STAGE}" \
      --desired-status RUNNING \
      --query 'taskArns' --output text 2>/dev/null || true
  )"
  if [[ -n "${SANDBOX_TASKS// /}" && "${SANDBOX_TASKS}" != "None" ]]; then
    for task_arn in ${SANDBOX_TASKS}; do
      echo "  - StopTask ${task_arn}"
      aws ecs stop-task \
        --region "${REGION}" \
        --cluster "${SANDBOX_CLUSTER}" \
        --task "${task_arn}" \
        --reason "teardown" \
        --output text >/dev/null 2>&1 || true
    done
  else
    echo "  (none running — nothing to do)"
  fi
fi

# Cloud Map (service discovery) cleanup — added with the M1.5 gateway.
# The agent ECS service registers each task as an instance in the
# `dataanalyst.local` private DNS namespace (for the session-affinity
# gateway). CloudFormation owns the Cloud Map *service* and *namespace*,
# but the per-task *instances* are created by ECS at runtime, and ECS
# deregisters them asynchronously when tasks stop. If CFN tries to
# delete the namespace before that async deregistration finishes, it
# fails with "Namespace has associated services" / "Service contains
# registered instances" and the Compute stack deletion hangs — exactly
# what bit us during the M1.5 gateway rollback.
#
# Defuse it: scale the agent service to 0 so nothing re-registers, wait
# for it to drain, then explicitly deregister any lingering instances.
# We deliberately do NOT delete the Cloud Map service/namespace here —
# those are CFN-managed, and deleting them out of band would make
# `cdk destroy` fail with "resource not found". We only clear the
# runtime-created instances that block the CFN deletion.
if [[ -n "${SANDBOX_CLUSTER}" ]]; then
  AGENT_SERVICE="$(
    aws ssm get-parameter \
      --region "${REGION}" \
      --name "/data-analyst-agent/$(echo "${STAGE}" | tr '[:upper:]' '[:lower:]')/agent/service-name" \
      --query 'Parameter.Value' --output text 2>/dev/null || true
  )"
  if [[ -n "${AGENT_SERVICE}" && "${AGENT_SERVICE}" != "None" ]]; then
    echo "Scaling agent service ${AGENT_SERVICE} to 0 (drains Cloud Map registrations)..."
    aws ecs update-service \
      --region "${REGION}" \
      --cluster "${SANDBOX_CLUSTER}" \
      --service "${AGENT_SERVICE}" \
      --desired-count 0 \
      --output text >/dev/null 2>&1 || true
    echo "  waiting for agent tasks to stop..."
    aws ecs wait services-stable \
      --region "${REGION}" \
      --cluster "${SANDBOX_CLUSTER}" \
      --services "${AGENT_SERVICE}" 2>/dev/null || true
  fi

  echo "Deregistering any lingering Cloud Map instances in dataanalyst.local..."
  CLOUDMAP_NS_ID="$(
    aws servicediscovery list-namespaces \
      --region "${REGION}" \
      --query "Namespaces[?Name=='dataanalyst.local'].Id" \
      --output text 2>/dev/null || true
  )"
  if [[ -n "${CLOUDMAP_NS_ID// /}" && "${CLOUDMAP_NS_ID}" != "None" ]]; then
    for svc_id in $(
      aws servicediscovery list-services \
        --region "${REGION}" \
        --filters "Name=NAMESPACE_ID,Values=${CLOUDMAP_NS_ID},Condition=EQ" \
        --query 'Services[].Id' --output text 2>/dev/null || true
    ); do
      for inst_id in $(
        aws servicediscovery list-instances \
          --region "${REGION}" \
          --service-id "${svc_id}" \
          --query 'Instances[].Id' --output text 2>/dev/null || true
      ); do
        echo "  - deregister ${svc_id}/${inst_id}"
        aws servicediscovery deregister-instance \
          --region "${REGION}" \
          --service-id "${svc_id}" \
          --instance-id "${inst_id}" \
          --output text >/dev/null 2>&1 || true
      done
    done
    # Give async deregistration a moment to propagate before CFN tries
    # to delete the Cloud Map service + namespace.
    echo "  waiting 15s for deregistration to propagate..."
    sleep 15
  else
    echo "  (no dataanalyst.local namespace found — nothing to do)"
  fi
fi

cd "${INFRA_DIR}"

# Retry wrapper around `cdk destroy`. Why: cdk's underlying AWS SDK has
# a 300000ms (5-min) per-request timeout. RDS instance deletion takes
# 5-15 min, so one of cdk's wait-on-RDS requests can exceed that
# ceiling — the deletion succeeds server-side, but the cdk CLI's
# polling loop loses the thread and stalls before advancing to the
# remaining stacks (you see "@smithy/node-http-handler ... exceeded the
# configured 300000 ms requestTimeout"). cdk destroy is idempotent —
# re-running skips already-deleted stacks and resumes — so we just loop
# until either everything's gone or we hit the attempt cap.
TEARDOWN_MAX_ATTEMPTS="${TEARDOWN_MAX_ATTEMPTS:-4}"

live_project_stacks() {
  aws cloudformation list-stacks \
    --region "${REGION}" \
    --stack-status-filter \
        CREATE_COMPLETE CREATE_IN_PROGRESS \
        UPDATE_COMPLETE UPDATE_IN_PROGRESS \
        UPDATE_ROLLBACK_COMPLETE ROLLBACK_COMPLETE \
        UPDATE_ROLLBACK_FAILED ROLLBACK_FAILED \
        DELETE_IN_PROGRESS DELETE_FAILED \
    --query "StackSummaries[?starts_with(StackName, \`${STACK_PREFIX}\`) && ends_with(StackName, \`-${STAGE}\`)].StackName" \
    --output text 2>/dev/null || true
}

attempt=1
while true; do
  echo
  echo "==> cdk destroy attempt ${attempt}/${TEARDOWN_MAX_ATTEMPTS}..."
  # Don't let a non-zero exit (e.g. the smithy timeout) abort the
  # script under `set -e` — we inspect remaining stacks ourselves.
  cdk destroy --all --force || echo "    [warn] cdk destroy exited non-zero (likely the SDK request timeout); will re-check stacks."

  STILL_LIVE="$(live_project_stacks)"
  if [[ -z "${STILL_LIVE// /}" ]]; then
    echo "    All project stacks deleted."
    break
  fi

  if (( attempt >= TEARDOWN_MAX_ATTEMPTS )); then
    echo "    [warn] still-live stacks after ${TEARDOWN_MAX_ATTEMPTS} attempts:" >&2
    for s in ${STILL_LIVE}; do echo "      - ${s}" >&2; done
    echo "    Falling through to final verification." >&2
    break
  fi

  echo "    Still live (cdk likely stalled on a slow delete); retrying:"
  for s in ${STILL_LIVE}; do echo "      - ${s}"; done
  # Short settle so any in-flight DELETE_IN_PROGRESS can advance before
  # the next cdk invocation re-attaches to it.
  sleep 20
  attempt=$(( attempt + 1 ))
done

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
