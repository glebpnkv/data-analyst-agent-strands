#!/usr/bin/env bash
# Build, tag, and push the sandbox container image.
#
# Unlike deploy.sh agent/frontend, the sandbox has no ECS service
# behind it — the agent claims fresh tasks dynamically via
# ecs:RunTask. New images are picked up automatically by the next
# RunTask once the task definition's :latest tag points at them, so
# there's no rolling-deploy step here. Push and you're done.
#
# Build context is `sandbox/` (not repo root) — the sandbox Dockerfile
# only references files inside that subdirectory.
#
# Usage: ./scripts/deploy_sandbox.sh
#
# Prerequisites:
#   - The Ecr stack has been deployed (so /sandbox/repo-uri is in SSM).
#   - You're authenticated to AWS (e.g. `aws sso login`).
#   - Docker is running.
#
# Env knobs:
#   AWS_REGION   default eu-central-1
#   STAGE        default dev
#
# Idempotent. Safe to re-run after a partial failure.

set -euo pipefail

REGION="${AWS_REGION:-eu-central-1}"
STAGE="${STAGE:-dev}"
SSM_PREFIX="/data-analyst-agent/${STAGE}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX_DIR="${REPO_ROOT}/sandbox"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 1
  fi
}
require_command docker
require_command aws
require_command git

ssm_get() {
  aws ssm get-parameter \
    --region "${REGION}" \
    --name "$1" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || true
}

echo "==> [sandbox] Reading repo URI from ${SSM_PREFIX}/sandbox/repo-uri..."
REPO_URI="$(ssm_get "${SSM_PREFIX}/sandbox/repo-uri")"
if [[ -z "${REPO_URI}" ]]; then
  cat <<EOF >&2
ERROR: Missing SSM parameter ${SSM_PREFIX}/sandbox/repo-uri.

The Ecr stack hasn't been deployed yet (or the SandboxRepo addition
hasn't been deployed since the Ecr stack was last touched). Run
\`./scripts/bootstrap.sh\` if this is a fresh stack, or
\`cdk deploy DataAnalystAgent-Ecr-${STAGE^}\` from infra/ to create
just the sandbox repo.

Also check AWS_REGION (currently ${REGION}) matches your CDK region.
EOF
  exit 1
fi

REGISTRY="${REPO_URI%%/*}"

# Tag with git short-sha so a running task's image is always traceable
# back to a real commit. `-dirty` flags uncommitted working tree changes.
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
GIT_DIRTY=""
if ! git -C "${REPO_ROOT}" diff --quiet HEAD 2>/dev/null; then
  GIT_DIRTY="-dirty"
fi
TAG="${GIT_SHA}${GIT_DIRTY}"

echo "==> [sandbox] Building image (context=${SANDBOX_DIR}, tag=${TAG}, also tagging :latest)..."
docker build \
  --platform linux/amd64 \
  -t "${REPO_URI}:${TAG}" \
  -t "${REPO_URI}:latest" \
  "${SANDBOX_DIR}"

echo "==> [sandbox] Logging in to ECR registry ${REGISTRY}..."
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}" >/dev/null

echo "==> [sandbox] Pushing both tags..."
docker push "${REPO_URI}:${TAG}"
docker push "${REPO_URI}:latest"

cat <<EOF

[OK] sandbox image pushed.
    image: ${REPO_URI}:${TAG}

The next chat session that claims a sandbox task will use this image
(via the :latest tag in the task definition). No ECS rollout needed —
existing pool tasks keep running their old image until they're stopped
on session end, and replacements pull :latest at RunTask time.
EOF
