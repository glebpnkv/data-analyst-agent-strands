"""CDK app entrypoint.

Synthesises the CloudFormation templates that provision the deployed
infrastructure for the data_analyst_agent's Chainlit chat frontend +
FastAPI agent service.

Stack dependency graph:

    Network -> Data
            -> Ecr
            -> Auth (Cognito user pool; depends only on stage)
            -> Compute (depends on all of the above + the existing
                        Route 53 hosted zone for the demo subdomain)

Naming: DataAnalystAgent-<Layer>-<Stage>. Stage is just "Dev" until we add
a second environment.

Region defaults to eu-central-1 (override with CDK_DEFAULT_REGION).
Account is read from CDK_DEFAULT_ACCOUNT at synth/deploy time so we
don't have to hard code it.

Domain-related context: `hosted_zone_id` and `domain_name` come from
infra/cdk.json (or per-deploy --context flags). Everything else
specific to a deployer (Glue role ARN, target repo, etc.) likewise.
"""

import os

import aws_cdk as cdk
from aws_cdk import aws_route53 as route53

from stacks.auth import AuthStack
from stacks.compute import ComputeStack
from stacks.data import DataStack
from stacks.ecr import EcrStack
from stacks.network import NetworkStack

DEFAULT_REGION = "eu-central-1"
STAGE = "Dev"


def main() -> None:
    app = cdk.App()

    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION") or DEFAULT_REGION,
    )

    # Tag every taggable resource so AWS Cost Explorer can filter the whole
    # project's spend in one click. Scoped to the App so every stack picks
    # it up automatically — no per-stack tagging boilerplate needed.
    cdk.Tags.of(app).add("Project", "DataAnalystAgent")
    cdk.Tags.of(app).add("Stage", STAGE)
    cdk.Tags.of(app).add("ManagedBy", "cdk")

    # --- Domain context ----------------------------------------------------
    # Pinned in infra/cdk.json. The hosted zone must already exist (created
    # manually in the AWS console; see agent README "Setting up a domain
    # and SSO"). CDK looks it up by ID — no API call at synth, just a
    # symbolic reference.
    hosted_zone_id = (app.node.try_get_context("hosted_zone_id") or "").strip()
    domain_name = (app.node.try_get_context("domain_name") or "").strip()
    if not hosted_zone_id or not domain_name:
        raise SystemExit(
            "ERROR: hosted_zone_id and domain_name must be set in cdk.json "
            "(or via --context). Cognito + ALB-HTTPS auth requires both."
        )

    hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
        app,
        "DelegatedZone",
        hosted_zone_id=hosted_zone_id,
        zone_name=_zone_name_from_domain(domain_name),
    )

    # Cognito hosted UI prefix is *globally* unique (per AWS region). Mix in
    # the account ID so two deployers can't collide. Lowercase, hyphenated,
    # 3-63 chars per AWS rules.
    account_for_prefix = env.account or "local"
    cognito_domain_prefix = f"dataanalystagent-{STAGE.lower()}-{account_for_prefix}"

    network = NetworkStack(
        app,
        f"DataAnalystAgent-Network-{STAGE}",
        env=env,
        description="VPC + security groups for the data_analyst_agent stack",
    )

    data = DataStack(
        app,
        f"DataAnalystAgent-Data-{STAGE}",
        env=env,
        description="RDS Postgres for Chainlit thread persistence + SM-managed credentials",
        vpc=network.vpc,
        rds_security_group=network.rds_sg,
    )

    ecr_stack = EcrStack(
        app,
        f"DataAnalystAgent-Ecr-{STAGE}",
        env=env,
        description="ECR repos for the agent + frontend container images",
        stage=STAGE,
    )

    auth = AuthStack(
        app,
        f"DataAnalystAgent-Auth-{STAGE}",
        env=env,
        description="Cognito User Pool + hosted UI for SSO via the frontend ALB",
        stage=STAGE,
        # ALB callback path is fixed — Cognito redirects users back to
        # https://<your-domain>/oauth2/idpresponse after auth, and the
        # ALB handles that path automatically.
        callback_urls=[f"https://{domain_name}/oauth2/idpresponse"],
        logout_urls=[f"https://{domain_name}/"],
        domain_prefix=cognito_domain_prefix,
    )

    ComputeStack(
        app,
        f"DataAnalystAgent-Compute-{STAGE}",
        env=env,
        description="ECS cluster + agent + frontend (public HTTPS, Cognito-fronted)",
        vpc=network.vpc,
        agent_alb_sg=network.agent_alb_sg,
        agent_task_sg=network.agent_task_sg,
        frontend_alb_sg=network.frontend_alb_sg,
        frontend_task_sg=network.frontend_task_sg,
        agent_repo=ecr_stack.agent_repo,
        frontend_repo=ecr_stack.frontend_repo,
        db_instance=data.db_instance,
        db_secret=data.db_secret,
        hosted_zone=hosted_zone,
        domain_name=domain_name,
        user_pool=auth.user_pool,
        user_pool_client=auth.user_pool_client,
        user_pool_domain=auth.user_pool_domain,
        stage=STAGE,
    )

    app.synth()


def _zone_name_from_domain(domain_name: str) -> str:
    """Derive the parent hosted zone name from a fully-qualified domain.

    Most setups deploy at the apex of the delegated zone (e.g.
    domain_name=`dataagent.gpnkv.com`, zone=`dataagent.gpnkv.com`).
    But you might point the chat at `chat.dataagent.gpnkv.com` while
    the hosted zone is `dataagent.gpnkv.com`. We assume the user used
    the apex case unless `domain_name` looks like more than two labels
    deeper than the typical zone — in that case we pop one label off.

    For the common case (apex), this is identity. For
    chat.dataagent.gpnkv.com it returns dataagent.gpnkv.com.
    """
    parts = domain_name.split(".")
    if len(parts) <= 3:
        # e.g. dataagent.gpnkv.com -> use as-is
        return domain_name
    # Strip the leftmost label, treat rest as the zone.
    return ".".join(parts[1:])


if __name__ == "__main__":
    main()
