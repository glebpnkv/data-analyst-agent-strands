"""Bootstrap the hackathon Workshop Studio account for the stripped-down
data-analyst-agent.

Creates (idempotently):
  - 3 S3 buckets:  hackathon-da-{raw,processed,gold}-{account}-us-east-1
  - 1 ECR repo:    aiagent-data-analyst
  - 1 IAM role:    aiagent-runtime-execution  (boundary: workshop-boundary)

Why these, in this order:
  - The AgentCore Runtime needs the role ARN at create-time, and the agent
    container (built later) needs the bucket names baked into its env. So
    everything downstream wants the resources here to exist already.
  - Re-runnable: each step is "create or skip", so you can run this after
    every policy tweak without churning the cluster.

Usage:
    AWS_PROFILE=hackathon python scripts/hackathon_bootstrap.py

Hard constraints baked in (don't undo without thinking):
  - Region pinned to us-east-1: the WSParticipantRole's `ws-default-policy`
    has a DenyAllOutsideAllowedRegions clause that blocks writes elsewhere.
  - Role MUST be named `aiagent-*` and MUST have `workshop-boundary`
    attached: that's the only naming/boundary combo `iam-policy-0` lets
    WSParticipantRole create.
  - Lambda functions, Lambda execution roles, and EventBridge rules the
    agent will create at runtime are all constrained to the
    `aiagent-lambda-*` prefix — the role's policy below scopes them
    explicitly so the agent can't reach for arbitrary names.
"""

from __future__ import annotations

import json
import sys

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
ROLE_NAME = "aiagent-runtime-execution"
ROLE_INLINE_POLICY_NAME = "agent-runtime-policy"
ECR_REPO = "aiagent-data-analyst"
PERMISSIONS_BOUNDARY = "workshop-boundary"
BUCKET_TIERS = ("raw", "processed", "gold")
LAMBDA_NAME_PREFIX = "aiagent-lambda-"


def _bucket_name(tier: str, account: str) -> str:
    """Tier name → globally-unique bucket name."""
    return f"hackathon-da-{tier}-{account}-{REGION}"


def _ensure_bucket(s3, name: str) -> None:
    """Create the bucket if missing, and lock down public access either way.

    `head_bucket` 404 / NoSuchBucket means it doesn't exist; anything else
    we re-raise (e.g. 403 = bucket exists but isn't ours, which we must not
    silently overwrite). us-east-1 has the well-known quirk that
    CreateBucket must NOT set LocationConstraint.
    """
    try:
        s3.head_bucket(Bucket=name)
        print(f"  = bucket exists: {name}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket"):
            raise
        s3.create_bucket(Bucket=name)
        print(f"  + bucket created: {name}")
    # Always re-apply BPA so an older bucket without it gets locked down.
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )


def _ensure_ecr_repo(ecr, name: str) -> str:
    """Return the repo URI, creating the repo if missing."""
    try:
        resp = ecr.describe_repositories(repositoryNames=[name])
        uri = resp["repositories"][0]["repositoryUri"]
        print(f"  = ECR repo exists: {uri}")
        return uri
    except ecr.exceptions.RepositoryNotFoundException:
        pass
    resp = ecr.create_repository(
        repositoryName=name,
        imageScanningConfiguration={"scanOnPush": True},
    )
    uri = resp["repository"]["repositoryUri"]
    print(f"  + ECR repo created: {uri}")
    return uri


def _trust_policy() -> dict:
    """Trust policy: AgentCore Runtime assumes this role to run our code."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }


def _inline_policy(account: str, buckets: list[str]) -> dict:
    """Least-privilege-ish runtime policy for the agent container.

    Scoped to the resources the agent actually needs:
      - Bedrock InvokeModel for the LLM
      - bedrock-agentcore:* for sub-services (Code Interpreter sandbox)
      - S3 full access on the three data buckets only
      - Lambda + EventBridge + IAM scoped to the `aiagent-lambda-*` prefix,
        which is the namespace the pipeline-deploy tool uses
      - Logs everywhere (CloudWatch Logs auto-creates groups)
    """
    bucket_arns = [f"arn:aws:s3:::{b}" for b in buckets]
    object_arns = [f"arn:aws:s3:::{b}/*" for b in buckets]
    lambda_arn_pattern = f"arn:aws:lambda:{REGION}:{account}:function:{LAMBDA_NAME_PREFIX}*"
    lambda_role_pattern = f"arn:aws:iam::{account}:role/{LAMBDA_NAME_PREFIX}*"
    events_arn_pattern = f"arn:aws:events:{REGION}:{account}:rule/{LAMBDA_NAME_PREFIX}*"
    boundary_arn = f"arn:aws:iam::{account}:policy/{PERMISSIONS_BOUNDARY}"

    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeBedrockModels",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": "*",
            },
            {
                "Sid": "AgentCoreSubservices",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:*",
                "Resource": "*",
            },
            {
                "Sid": "S3DataBucketsFull",
                "Effect": "Allow",
                "Action": "s3:*",
                "Resource": bucket_arns + object_arns,
            },
            {
                "Sid": "S3ListForDiscovery",
                "Effect": "Allow",
                "Action": ["s3:ListAllMyBuckets", "s3:GetBucketLocation"],
                "Resource": "*",
            },
            {
                "Sid": "LambdaPipelineLifecycle",
                "Effect": "Allow",
                "Action": [
                    "lambda:CreateFunction",
                    "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration",
                    "lambda:GetFunction",
                    "lambda:InvokeFunction",
                    "lambda:DeleteFunction",
                    "lambda:AddPermission",
                    "lambda:RemovePermission",
                ],
                "Resource": lambda_arn_pattern,
            },
            {
                "Sid": "LambdaListAll",
                "Effect": "Allow",
                "Action": "lambda:ListFunctions",
                "Resource": "*",
            },
            {
                # The deploy_pipeline_as_lambda tool attaches the
                # AWS-managed AWSSDKPandas-Python312 layer (account
                # 336392948345) so the deployed Lambda gets pandas /
                # numpy / pyarrow without us packaging them. Cross-
                # account layer references require GetLayerVersion;
                # ListLayerVersions lets us pick the latest at deploy
                # time instead of pinning a version that ages out.
                "Sid": "LambdaLayerLookup",
                "Effect": "Allow",
                "Action": [
                    "lambda:GetLayerVersion",
                    "lambda:ListLayerVersions",
                ],
                "Resource": "*",
            },
            {
                "Sid": "CreateLambdaExecutionRoles",
                "Effect": "Allow",
                "Action": [
                    "iam:CreateRole",
                    "iam:DeleteRole",
                    "iam:PutRolePolicy",
                    "iam:DeleteRolePolicy",
                    "iam:AttachRolePolicy",
                    "iam:DetachRolePolicy",
                    "iam:GetRole",
                ],
                "Resource": lambda_role_pattern,
                "Condition": {
                    "StringEquals": {
                        "iam:PermissionsBoundary": boundary_arn,
                    }
                },
            },
            {
                "Sid": "PassLambdaExecutionRoles",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": lambda_role_pattern,
                "Condition": {
                    "StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}
                },
            },
            {
                "Sid": "EventBridgeSchedules",
                "Effect": "Allow",
                "Action": [
                    "events:PutRule",
                    "events:PutTargets",
                    "events:RemoveTargets",
                    "events:DeleteRule",
                    "events:DescribeRule",
                    "events:ListTargetsByRule",
                ],
                "Resource": events_arn_pattern,
            },
            {
                "Sid": "EventBridgeListAll",
                "Effect": "Allow",
                "Action": "events:ListRules",
                "Resource": "*",
            },
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                    "logs:GetLogEvents",
                    "logs:FilterLogEvents",
                ],
                "Resource": "*",
            },
            {
                # AgentCore Runtime pulls our container image as THIS role
                # (not as a service principal), so the execution role
                # itself needs ECR pull perms. GetAuthorizationToken
                # requires Resource:* (it returns a registry-wide token);
                # the per-image actions are scoped to our repo.
                "Sid": "EcrAuthForRuntimeImagePull",
                "Effect": "Allow",
                "Action": "ecr:GetAuthorizationToken",
                "Resource": "*",
            },
            {
                "Sid": "EcrPullAgentImage",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                "Resource": f"arn:aws:ecr:{REGION}:{account}:repository/{ECR_REPO}",
            },
        ],
    }


def _ensure_role(iam, account: str, buckets: list[str]) -> str:
    """Create the role with the boundary attached, then (re)apply the inline
    policy. Returns the role ARN."""
    boundary_arn = f"arn:aws:iam::{account}:policy/{PERMISSIONS_BOUNDARY}"
    try:
        resp = iam.get_role(RoleName=ROLE_NAME)
        arn = resp["Role"]["Arn"]
        print(f"  = role exists: {arn}")
    except iam.exceptions.NoSuchEntityException:
        resp = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(_trust_policy()),
            PermissionsBoundary=boundary_arn,
            Description="Execution role for the data-analyst-agent on AgentCore Runtime",
        )
        arn = resp["Role"]["Arn"]
        print(f"  + role created: {arn}")
    # PutRolePolicy is upsert — re-running re-applies any policy changes
    # we make to `_inline_policy` without needing manual cleanup.
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=ROLE_INLINE_POLICY_NAME,
        PolicyDocument=json.dumps(_inline_policy(account, buckets)),
    )
    print(f"  ↻ policy applied: {ROLE_INLINE_POLICY_NAME}")
    return arn


def main() -> int:
    session = boto3.Session(region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    s3 = session.client("s3")
    ecr = session.client("ecr")
    iam = session.client("iam")

    print(f"== Hackathon bootstrap ({account} / {REGION}) ==\n")

    print("S3 buckets:")
    buckets: list[str] = []
    for tier in BUCKET_TIERS:
        name = _bucket_name(tier, account)
        _ensure_bucket(s3, name)
        buckets.append(name)

    print("\nECR repo:")
    ecr_uri = _ensure_ecr_repo(ecr, ECR_REPO)

    print("\nIAM role:")
    role_arn = _ensure_role(iam, account, buckets)

    print("\n== Done. Export these for the next step (image push + runtime create) ==")
    print(f"export AGENT_ACCOUNT_ID={account}")
    print(f"export AGENT_REGION={REGION}")
    print(f"export AGENT_ROLE_ARN={role_arn}")
    print(f"export AGENT_ECR_URI={ecr_uri}")
    for tier, name in zip(BUCKET_TIERS, buckets, strict=True):
        print(f"export AGENT_BUCKET_{tier.upper()}={name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
