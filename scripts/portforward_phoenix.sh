#!/usr/bin/env bash
# Open an SSM port-forward to the internal Phoenix ALB.
#
# Usage:
#     ./scripts/portforward_phoenix.sh [LOCAL_PORT=6006]
#
# After this is running, you can:
#   - browse Phoenix UI at http://localhost:${LOCAL_PORT}
#   - export PHOENIX_ENDPOINT=http://localhost:${LOCAL_PORT} so the
#     eval runner + scripts/upload_dataset.py talk to it
#
# Pair with portforward_agent.sh (different terminal) to run an
# end-to-end eval from your laptop:
#     ./scripts/portforward_agent.sh    # terminal 1
#     ./scripts/portforward_phoenix.sh  # terminal 2
#     # then in terminal 3, run eval/upload commands
#
# Env knobs:
#   STAGE        default Dev   (must match the CDK stage)
#   AWS_REGION   default eu-central-1

set -euo pipefail

LOCAL_PORT="${1:-6006}"
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

PHOENIX_ALB=$(aws ssm get-parameter --region "${REGION}" \
  --name "${SSM_PREFIX}/phoenix/alb-dns" \
  --query Parameter.Value --output text)

INSTANCE_ID=$(aws ec2 describe-instances --region "${REGION}" \
  --filters "Name=tag:aws:autoscaling:groupName,Values=*EcsAsg*" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)

if [[ -z "${INSTANCE_ID}" || "${INSTANCE_ID}" == "None" ]]; then
  echo "ERROR: no running ECS instance to tunnel through." >&2
  exit 1
fi

cat <<EOF
==> Forwarding localhost:${LOCAL_PORT} -> http://${PHOENIX_ALB}:80 via ${INSTANCE_ID}
==> Phoenix UI:   http://localhost:${LOCAL_PORT}
==> For scripts:  export PHOENIX_ENDPOINT=http://localhost:${LOCAL_PORT}
==> Ctrl-C to close the tunnel.
EOF

exec aws ssm start-session --region "${REGION}" \
  --target "${INSTANCE_ID}" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "host=${PHOENIX_ALB},portNumber=80,localPortNumber=${LOCAL_PORT}"
