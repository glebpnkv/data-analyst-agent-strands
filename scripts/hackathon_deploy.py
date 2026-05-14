"""Build, push, and (re)register the agent container on AgentCore Runtime.

Pipeline (idempotent — re-run after every code change):
  1. `docker buildx build` for linux/arm64 (AgentCore Runtime is
     arm64-only — it rejects amd64 images at CreateAgentRuntime time
     with `Architecture incompatible`).
  2. ECR auth + push.
  3. `CreateAgentRuntime` if it's not there yet, else find the existing
     id via `ListAgentRuntimes` and `UpdateAgentRuntime` in place.
  4. Print the runtime ARN.

Inputs come from the bootstrap script's outputs — bucket names, role
ARN, ECR URI all derived from the account id + region, no manual env
juggling needed.

Usage:
    AWS_PROFILE=hackathon uv run python scripts/hackathon_deploy.py
"""

from __future__ import annotations

import base64
import os
import shlex
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
# AgentCore runtime names: must match [a-zA-Z][a-zA-Z0-9_]{0,47}
RUNTIME_NAME = "data_analyst_agent"
ROLE_NAME = "aiagent-runtime-execution"
ECR_REPO = "aiagent-data-analyst"
IMAGE_TAG = "latest"
DOCKERFILE = "agent/Dockerfile.agentcore"
BUILD_CONTEXT = "."

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], **kw) -> None:
    """Run a subprocess, streaming output, fail loud on non-zero."""
    print(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
    subprocess.run(cmd, check=True, cwd=REPO_ROOT, **kw)


def _ecr_login(ecr, repo_uri: str) -> None:
    """Authenticate the local docker daemon to ECR for this account/region.

    GetAuthorizationToken returns a base64'd 'AWS:<password>' pair plus
    the registry endpoint. We feed the password to docker login on stdin
    so it doesn't end up in the process listing or shell history.
    """
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    endpoint = auth["proxyEndpoint"]
    _run(
        ["docker", "login", "--username", user, "--password-stdin", endpoint],
        input=password.encode(),
    )


def _find_runtime_id(agentcore, name: str) -> str | None:
    """Return the agentRuntimeId for `name`, or None if it doesn't exist."""
    paginator = agentcore.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for r in page.get("agentRuntimes", []):
            if r.get("agentRuntimeName") == name:
                return r["agentRuntimeId"]
    return None


def main() -> int:
    session = boto3.Session(region_name=REGION)
    sts = session.client("sts")
    iam = session.client("iam")
    ecr = session.client("ecr")
    agentcore = session.client("bedrock-agentcore-control")

    account = sts.get_caller_identity()["Account"]
    role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    repo_uri = f"{account}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}"
    image_uri = f"{repo_uri}:{IMAGE_TAG}"

    print(f"== Build (linux/arm64) ==")
    _run(
        [
            "docker", "buildx", "build",
            "--platform", "linux/arm64",
            "-t", image_uri,
            "-f", DOCKERFILE,
            BUILD_CONTEXT,
            "--load",
        ]
    )

    print("\n== ECR login + push ==")
    _ecr_login(ecr, repo_uri)
    _run(["docker", "push", image_uri])

    # Bucket names are deterministic from the bootstrap script — no need
    # to round-trip through SSM or env vars; the agent container reads
    # these straight out of os.environ.
    env_vars = {
        "BUCKET_RAW": f"hackathon-da-raw-{account}-{REGION}",
        "BUCKET_PROCESSED": f"hackathon-da-processed-{account}-{REGION}",
        "BUCKET_GOLD": f"hackathon-da-gold-{account}-{REGION}",
        "REGION": REGION,
        # Model is configurable here so we can swap Sonnet ↔ Haiku without
        # touching the agent code (handy when iterating cost vs quality).
        # Cross-region inference profile id — bare `anthropic.*` model ids
        # don't accept on-demand calls for Sonnet 4.5 on us-east-1.
        "MODEL_ID": os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
    }
    runtime_kwargs = dict(
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
        roleArn=role_arn,
        # PUBLIC = AgentCore-managed networking, no VPC. We don't need
        # private connectivity for the hackathon — the agent just needs
        # outbound to Bedrock + S3, and the workshop account doesn't let
        # us create a VPC anyway.
        networkConfiguration={"networkMode": "PUBLIC"},
        # HTTP = the BedrockAgentCoreApp / FastAPI shape. The other
        # options (MCP, A2A, AGUI) are different on-the-wire contracts.
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables=env_vars,
    )

    print("\n== Create or update runtime ==")
    runtime_id = _find_runtime_id(agentcore, RUNTIME_NAME)
    if runtime_id is None:
        try:
            resp = agentcore.create_agent_runtime(
                agentRuntimeName=RUNTIME_NAME, **runtime_kwargs
            )
            action = "created"
        except ClientError as e:
            # Race: someone created it between list and create. Retry as update.
            if e.response["Error"]["Code"] != "ConflictException":
                raise
            runtime_id = _find_runtime_id(agentcore, RUNTIME_NAME)
            if runtime_id is None:
                raise
            resp = agentcore.update_agent_runtime(
                agentRuntimeId=runtime_id, **runtime_kwargs
            )
            action = "updated (after conflict)"
    else:
        resp = agentcore.update_agent_runtime(
            agentRuntimeId=runtime_id, **runtime_kwargs
        )
        action = "updated"

    arn = resp["agentRuntimeArn"]
    rt_id = resp["agentRuntimeId"]
    version = resp.get("agentRuntimeVersion")
    status = resp.get("status")

    print(f"\n  {action}: {arn}")
    print(f"  id:      {rt_id}")
    print(f"  version: {version}")
    print(f"  status:  {status}")
    print(f"\nExport for the invoke step:")
    print(f"  export AGENT_RUNTIME_ARN={arn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
