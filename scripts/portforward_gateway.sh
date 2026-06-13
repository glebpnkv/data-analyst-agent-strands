#!/usr/bin/env bash
# Open an SSM port-forward to the internal gateway ALB.
#
# Usage:
#     ./scripts/portforward_gateway.sh [LOCAL_PORT=8080]
#
# The gateway sits between Chainlit and the agent service; it
# consistent-hashes /v1/chat on the X-Session-Id header so a session's
# turns stick to one agent task. From a developer laptop, calls go
# through the gateway by tunneling to its internal ALB.
#
# After this is running, the eval runner (and any local client) can
# treat http://localhost:${LOCAL_PORT} as the production entry point
# to the agent, and gets the same session-affinity behaviour that
# Chainlit gets in deployed traffic.
#
# Pair with print_agent_auth.sh for the X-Service-Auth secret.
#
# Env knobs:
#   STAGE        default Dev   (must match the CDK stage)
#   AWS_REGION   default eu-central-1

set -euo pipefail

LOCAL_PORT="${1:-8080}"
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

GATEWAY_ALB=$(aws ssm get-parameter --region "${REGION}" \
  --name "${SSM_PREFIX}/gateway/alb-dns" \
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
==> Forwarding localhost:${LOCAL_PORT} -> http://${GATEWAY_ALB}:80 via ${INSTANCE_ID}
==> The gateway routes by X-Session-Id, so SSE streams from the same
==> session id will land on the same agent task while it's healthy.
==> Once connected, in another terminal:
        export AGENT_BASE_URL=http://localhost:${LOCAL_PORT}
        export AGENT_SERVICE_AUTH_SECRET=\$(./scripts/print_agent_auth.sh)
        uv run --group dev python -m eval.run
==> Ctrl-C to close the tunnel.
EOF

exec aws ssm start-session --region "${REGION}" \
  --target "${INSTANCE_ID}" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "host=${GATEWAY_ALB},portNumber=80,localPortNumber=${LOCAL_PORT}"
