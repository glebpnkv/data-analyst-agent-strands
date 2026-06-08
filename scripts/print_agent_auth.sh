#!/usr/bin/env bash
# Print the X-Service-Auth secret value to stdout.
#
# Usage:
#     export AGENT_SERVICE_AUTH_SECRET=$(./scripts/print_agent_auth.sh)
#
# This is the same secret the deployed Chainlit task uses to sign its
# requests to the agent. The eval runner needs it so its POSTs to
# /v1/chat get past the service-auth middleware.
#
# Env knobs:
#   STAGE        default Dev
#   AWS_REGION   default eu-central-1

set -euo pipefail

STAGE="${STAGE:-Dev}"
REGION="${AWS_REGION:-eu-central-1}"
STAGE_LOWER="$(echo "${STAGE}" | tr 'A-Z' 'a-z')"
SSM_PREFIX="/data-analyst-agent/${STAGE_LOWER}"

SECRET_ARN=$(aws ssm get-parameter --region "${REGION}" \
  --name "${SSM_PREFIX}/service-auth-secret-arn" \
  --query Parameter.Value --output text)

aws secretsmanager get-secret-value --region "${REGION}" \
  --secret-id "${SECRET_ARN}" \
  --query SecretString --output text
