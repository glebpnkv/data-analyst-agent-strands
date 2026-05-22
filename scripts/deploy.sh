#!/usr/bin/env bash
# Build, push, and roll out one or both deployed services.
#
# Usage:
#   ./scripts/deploy.sh agent       # just the agent FastAPI service
#   ./scripts/deploy.sh frontend    # just the Chainlit frontend
#   ./scripts/deploy.sh all         # both, agent first
#
# Prerequisites:
#   - The Ecr stack has been deployed (so the ECR repos exist). For a
#     fresh repo, use `./scripts/bootstrap.sh` instead — it sequences
#     the first-deploy chicken-and-egg correctly.
#   - You're authenticated to AWS (e.g. `aws sso login`).
#   - Docker is running on your machine.
#
# What it does, per service:
#   1. Reads repo URI (and, if available, cluster + service names) from
#      SSM Parameter Store entries the CDK stacks write.
#   2. docker build with --platform linux/amd64 (matches the task
#      definition's CpuArchitecture.X86_64; cross-compiles via buildx
#      on M-series Macs).
#   3. Tags <repo>:<git-short-sha>[-dirty] and <repo>:latest, pushes both.
#      The dirty suffix flags deploys from uncommitted working trees so
#      a running task's tag is always traceable to a real git commit.
#   4. If the ECS service exists (cluster-name and service-name SSM
#      params are present), update-service --force-new-deployment and
#      wait for it to stabilize. Otherwise the script runs in
#      "push-only" mode — useful during bootstrap, when the Compute
#      stack hasn't been deployed yet and the services don't exist.
#
# Idempotent: same script handles bootstrap, first deploy, and rolling
# replacement cleanly. Safe to re-run.
#
# Env knobs:
#   AWS_REGION   default eu-central-1   (must match the CDK deploy region)
#   STAGE        default dev            (drives the SSM prefix)

set -euo pipefail

usage() {
  cat <<EOF
Usage: $0 <agent|frontend|all>
EOF
  exit 2
}

if [[ $# -ne 1 ]]; then
  usage
fi

case "$1" in
  agent|frontend|all) ;;
  *) usage ;;
esac

REGION="${AWS_REGION:-eu-central-1}"
STAGE="${STAGE:-dev}"
SSM_PREFIX="/data-analyst-agent/${STAGE}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 1
  fi
}

ssm_get() {
  # `|| true` so a missing parameter (ParameterNotFound, exit 254) never
  # propagates failure out of the function. Important under `set -e`:
  # bash's behaviour around assignment-from-command-substitution is
  # subtly version-dependent, and we don't want a missing-but-expected
  # SSM key (e.g. /cluster-name during bootstrap) to abort the script.
  aws ssm get-parameter \
    --region "${REGION}" \
    --name "$1" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || true
}

deploy_one() {
  local svc="$1"          # "agent" | "frontend"
  local dockerfile_rel="$2"

  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local repo_root
  repo_root="$(cd "${script_dir}/.." && pwd)"

  echo "==> [${svc}] Reading deployment targets from SSM (${SSM_PREFIX})..."
  local repo_uri cluster_name service_name
  repo_uri="$(ssm_get "${SSM_PREFIX}/${svc}/repo-uri")"
  cluster_name="$(ssm_get "${SSM_PREFIX}/cluster-name")"
  service_name="$(ssm_get "${SSM_PREFIX}/${svc}/service-name")"

  # repo-uri is the only hard requirement — without it we can't even
  # tag the image. cluster + service are optional: if they're missing,
  # the Compute stack hasn't been deployed yet and we're in bootstrap
  # mode (push the image so the Compute deploy can pull it).
  if [[ -z "${repo_uri}" ]]; then
    cat <<EOF >&2
ERROR: Missing SSM parameter ${SSM_PREFIX}/${svc}/repo-uri.

The Ecr stack hasn't been deployed yet. For a clean repo, run
\`./scripts/bootstrap.sh\` — it deploys the prerequisite stacks,
pushes the initial images, then deploys Compute in the right order.

If you've already deployed once, double-check AWS_REGION (currently
${REGION}) matches the region you deployed into.
EOF
    exit 1
  fi

  local push_only=0
  if [[ -z "${cluster_name}" || -z "${service_name}" ]]; then
    push_only=1
    echo "    Cluster/service SSM params not yet present:"
    echo "      cluster-name         = ${cluster_name:-<missing>}"
    echo "      ${svc}/service-name  = ${service_name:-<missing>}"
    echo "    Running in push-only mode (Compute stack not deployed yet)."
  fi

  local registry="${repo_uri%%/*}"

  local git_sha
  git_sha="$(git -C "${repo_root}" rev-parse --short HEAD)"
  local git_dirty=""
  if ! git -C "${repo_root}" diff --quiet HEAD 2>/dev/null; then
    git_dirty="-dirty"
  fi
  local tag="${git_sha}${git_dirty}"

  echo "==> [${svc}] Building image (tag=${tag}, also tagging :latest)..."
  docker build \
    --platform linux/amd64 \
    -f "${repo_root}/${dockerfile_rel}" \
    -t "${repo_uri}:${tag}" \
    -t "${repo_uri}:latest" \
    "${repo_root}"

  echo "==> [${svc}] Logging in to ECR registry ${registry}..."
  aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${registry}" >/dev/null

  echo "==> [${svc}] Pushing both tags..."
  docker push "${repo_uri}:${tag}"
  docker push "${repo_uri}:latest"

  if [[ "${push_only}" == "1" ]]; then
    echo
    echo "[OK] ${svc} image pushed (push-only mode; no ECS rollout)."
    echo "    image: ${repo_uri}:${tag}"
    echo "    Run \`./scripts/bootstrap.sh\` (or finish the Compute stack"
    echo "    deploy) to bring the service up against this image."
    echo
    return 0
  fi

  echo "==> [${svc}] Triggering deployment of ${service_name}..."
  local current_desired
  current_desired="$(aws ecs describe-services \
    --region "${REGION}" \
    --cluster "${cluster_name}" \
    --services "${service_name}" \
    --query 'services[0].desiredCount' \
    --output text)"

  if [[ "${current_desired}" == "0" ]]; then
    aws ecs update-service \
      --region "${REGION}" \
      --cluster "${cluster_name}" \
      --service "${service_name}" \
      --desired-count 1 \
      --force-new-deployment \
      >/dev/null
    echo "    Bumped desired count from 0 to 1 (first deploy)."
  else
    aws ecs update-service \
      --region "${REGION}" \
      --cluster "${cluster_name}" \
      --service "${service_name}" \
      --force-new-deployment \
      >/dev/null
    echo "    Forced new deployment (desired count remains ${current_desired})."
  fi

  echo "==> [${svc}] Waiting for ${service_name} to stabilize..."
  echo "    (2-5 minutes typically: ECS pulls the image, runs the task,"
  echo "     ALB target group needs two consecutive successful health checks.)"
  aws ecs wait services-stable \
    --region "${REGION}" \
    --cluster "${cluster_name}" \
    --services "${service_name}"

  echo
  echo "[OK] ${svc} deployed."
  echo "    image:    ${repo_uri}:${tag}"
  echo "    service:  ${service_name}"
  echo "    cluster:  ${cluster_name}"
  echo
}

require_command docker
require_command aws
require_command git

case "$1" in
  agent)
    deploy_one "agent" "agent/Dockerfile"
    ;;
  frontend)
    deploy_one "frontend" "frontend/Dockerfile"
    ;;
  all)
    # Agent first so the frontend's first request to the agent ALB
    # finds a live target. Both services tolerate the other being down
    # for a few minutes (Chainlit boots and shows the login form even
    # if the agent isn't up; the agent doesn't need the frontend at
    # all), so the order is a small optimization, not a hard
    # dependency.
    deploy_one "agent" "agent/Dockerfile"
    deploy_one "frontend" "frontend/Dockerfile"
    ;;
esac
