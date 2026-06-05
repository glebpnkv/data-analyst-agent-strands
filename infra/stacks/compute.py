"""Compute stack: ECS cluster on EC2 launch type + agent + frontend services + sandbox task def.

Why EC2 launch type, not Fargate
--------------------------------
Two of the user's accounts forbid Fargate (org-level SCP for non-Spark
workloads). The whole stack therefore runs on an Auto Scaling Group of
ECS-optimized EC2 instances. ECS API surfaces (`RunTask`, `StopTask`,
`DescribeTasks`) work the same; the only conceptual difference is that
capacity is now bounded by the ASG min/max, and tasks scheduled to one
host share that host's kernel. See the plan in
`/Users/gleb/.claude/plans/synthetic-rolling-shore.md` for the
isolation trade-off analysis.

What lives here
---------------
- One ECS cluster.
- ASG of EC2 instances feeding the cluster, plus a managed
  AsgCapacityProvider that scales the ASG up when ECS needs more
  capacity. ENI trunking enabled at account level so each t3.medium
  fits ~11 awsvpc tasks (default 2) — required for our pool sizing.
- Per-service auth + signing secrets in Secrets Manager (auto-generated;
  ECS injects them as env on task launch). GitHub PAT placeholder (user
  populates manually).
- Sandbox: ECR repo (created in the Ecr stack), task role (logs-only),
  task definition. NO ECS service for the sandbox — the agent claims
  tasks dynamically via `ecs:RunTask`.
- Agent: task role with Bedrock, Athena, Glue (from the policy doc),
  plus the new ECS RunTask / IAM PassRole / EC2 DescribeNetworkInterfaces
  surface. Task def, ALB (internal HTTP), Ec2Service.
- Frontend: task role with read on three SM secrets. Task def, public
  ALB (HTTPS, Cognito-fronted), Ec2Service.
- SSM Parameter Store entries for everything the deploy scripts need
  to look up by a stable path (cluster name, service names, ALB DNSes,
  sandbox task def ARN, sandbox SG ID, sandbox subnet IDs, secret ARNs).
"""

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_actions as elbv2_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from stacks.network import (  # noqa: F401  (reused symbols)
    AGENT_HTTP_PORT,
    ALB_HTTP_PORT,
    ALB_HTTPS_PORT,
    FRONTEND_HTTP_PORT,
    PHOENIX_HTTP_PORT,
    SANDBOX_HTTP_PORT,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GLUE_POLICY_PATH = _REPO_ROOT / "infra" / "policies" / "data_analyst_access.json"

AGENT_TASK_CPU = 512  # 0.5 vCPU
AGENT_TASK_MEMORY_MIB = 1024  # 1 GB
FRONTEND_TASK_CPU = 256  # 0.25 vCPU
FRONTEND_TASK_MEMORY_MIB = 512  # 0.5 GB
PHOENIX_TASK_CPU = 512  # 0.5 vCPU
PHOENIX_TASK_MEMORY_MIB = 2048  # 2 GB

# Phoenix container tag. Pinned for reproducibility; bump deliberately
# after reading the release notes — Phoenix's storage schema migrates
# at container boot under a write lock, so a rollback isn't free.
# Verify the latest tag at https://hub.docker.com/r/arizephoenix/phoenix/tags
# before bumping.
PHOENIX_IMAGE_TAG = "11.4.0"
PHOENIX_DATABASE_NAME = "phoenix"  # logical DB on the existing RDS instance

# Defaults if cdk.json doesn't override; bound the runtime within
# small-dev-friendly ranges. Per-deploy override via `cdk deploy --context key=value`.
DEFAULT_ASG_INSTANCE_TYPE = "t3.medium"
DEFAULT_ASG_MIN = 2
DEFAULT_ASG_MAX = 8
DEFAULT_SANDBOX_POOL_SIZE = 2
DEFAULT_SANDBOX_CPU = 1024  # 1 vCPU
DEFAULT_SANDBOX_MEMORY_MIB = 2048  # 2 GB
SANDBOX_CONTAINER_NAME = "sandbox"


class ComputeStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        agent_alb_sg: ec2.ISecurityGroup,
        agent_task_sg: ec2.ISecurityGroup,
        frontend_alb_sg: ec2.ISecurityGroup,
        frontend_task_sg: ec2.ISecurityGroup,
        sandbox_task_sg: ec2.ISecurityGroup,
        phoenix_alb_sg: ec2.ISecurityGroup,
        phoenix_task_sg: ec2.ISecurityGroup,
        agent_repo: ecr.IRepository,
        frontend_repo: ecr.IRepository,
        sandbox_repo: ecr.IRepository,
        db_instance: rds.IDatabaseInstance,
        db_secret: secretsmanager.ISecret,
        hosted_zone: route53.IHostedZone,
        domain_name: str,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        user_pool_domain: cognito.IUserPoolDomain,
        stage: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Resolve context-flag knobs once so we can reference them everywhere.
        asg_instance_type = (
            self.node.try_get_context("asg_instance_type") or DEFAULT_ASG_INSTANCE_TYPE
        )
        asg_min = int(self.node.try_get_context("asg_min") or DEFAULT_ASG_MIN)
        asg_max = int(self.node.try_get_context("asg_max") or DEFAULT_ASG_MAX)
        sandbox_pool_size = int(
            self.node.try_get_context("sandbox_pool_size") or DEFAULT_SANDBOX_POOL_SIZE
        )
        sandbox_cpu = int(self.node.try_get_context("sandbox_cpu") or DEFAULT_SANDBOX_CPU)
        sandbox_memory_mib = int(
            self.node.try_get_context("sandbox_memory_mib") or DEFAULT_SANDBOX_MEMORY_MIB
        )

        ssm_prefix = f"/data-analyst-agent/{stage.lower()}"

        # =====================================================================
        # CLUSTER + ASG CAPACITY PROVIDER
        # =====================================================================
        self.cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.DISABLED,
        )

        # ENI trunking is an account/region-wide ECS setting that is NOT
        # a CloudFormation resource type — it's only settable via the
        # `aws ecs put-account-setting` CLI / SDK. We do it once per
        # account/region from `scripts/bootstrap.sh` (see the
        # `awsvpcTrunking` block there). Without it, a t3.medium only
        # supports 2 awsvpc-mode tasks (3 ENIs minus 1 for the host),
        # which is below our floor (1 agent + 1 frontend + 2 sandbox
        # pool tasks = 4).

        # IAM role attached to every EC2 instance the ASG launches. CDK
        # auto-creates the InstanceProfile from this role when we pass it
        # to the LaunchTemplate. The two managed policies cover:
        #   - AmazonEC2ContainerServiceforEC2Role: lets the ECS agent
        #     register the instance with the cluster, pull from ECR, and
        #     report task state. add_asg_capacity_provider would add this
        #     anyway, but attaching it here is harmless and more
        #     discoverable.
        #   - AmazonSSMManagedInstanceCore: enables `aws ecs
        #     execute-command` and SSM Session Manager onto the host so
        #     you can shell in for debugging without a bastion.
        ecs_instance_role = iam.Role(
            self,
            "EcsInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonEC2ContainerServiceforEC2Role"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )

        # Auto Scaling Group backed by an explicit LaunchTemplate.
        #
        # We must use LaunchTemplate (NOT LaunchConfiguration) because
        # AWS no longer supports Launch Configuration creation in newly
        # provisioned accounts (`The Launch Configuration creation
        # operation is not available in your account`). CDK's
        # AutoScalingGroup with `instance_type=`/`machine_image=`
        # defaults to a LaunchConfiguration for backward compat, which
        # 400s on those accounts. Passing `launch_template=` explicitly
        # forces a LaunchTemplate and the deploy goes through.
        #
        # `ecs.EcsOptimizedImage.amazon_linux2()` ships an AMI with the
        # ECS agent pre-installed; `cluster.add_asg_capacity_provider`
        # appends user data to register the instance with this cluster
        # (works with both LaunchConfiguration and LaunchTemplate).
        ecs_launch_template = ec2.LaunchTemplate(
            self,
            "EcsLaunchTemplate",
            instance_type=ec2.InstanceType(asg_instance_type),
            machine_image=ecs.EcsOptimizedImage.amazon_linux2(),
            role=ecs_instance_role,
            require_imdsv2=True,
            # Empty UserData up front so add_asg_capacity_provider has
            # somewhere to append the cluster-join script to.
            user_data=ec2.UserData.for_linux(),
        )

        ecs_asg = autoscaling.AutoScalingGroup(
            self,
            "EcsAsg",
            vpc=vpc,
            launch_template=ecs_launch_template,
            min_capacity=asg_min,
            max_capacity=asg_max,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )

        # Managed scaling lets ECS expand the ASG when there's pending
        # task capacity demand and shrink it when instances are idle.
        # `enable_managed_termination_protection=False` matters because
        # CDK defaults to True, which conflicts with `cdk destroy`'s
        # cleanup — instances refuse to terminate and the stack delete
        # hangs. For dev we want destroy to work; for prod, set to True
        # so a misconfigured scale-in doesn't kill an in-flight task.
        capacity_provider = ecs.AsgCapacityProvider(
            self,
            "EcsAsgCapacityProvider",
            auto_scaling_group=ecs_asg,
            enable_managed_scaling=True,
            enable_managed_termination_protection=False,
        )
        self.cluster.add_asg_capacity_provider(capacity_provider)

        # Default capacity provider strategy: services and standalone
        # tasks (e.g. the agent's ecs:RunTask for sandbox tasks) without
        # an explicit strategy use this. `weight=1, base=0` is the
        # simplest "use this provider" form.
        self.cluster.add_default_capacity_provider_strategy(
            [
                ecs.CapacityProviderStrategy(
                    capacity_provider=capacity_provider.capacity_provider_name,
                    weight=1,
                    base=0,
                )
            ]
        )

        # =====================================================================
        # SHARED SECRETS
        # =====================================================================
        self.service_auth_secret = secretsmanager.Secret(
            self,
            "ServiceAuthSecret",
            secret_name=f"DataAnalystAgent/{stage}/ServiceAuthSecret",
            description="Shared secret for X-Service-Auth between Chainlit and the agent",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True,
                password_length=64,
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.github_pat_secret = secretsmanager.Secret(
            self,
            "GithubPatSecret",
            secret_name=f"DataAnalystAgent/{stage}/GithubPat",
            description=(
                "GitHub fine-grained PAT used by the agent's GitHub MCP client. "
                "Populate with: aws secretsmanager put-secret-value "
                "--secret-id DataAnalystAgent/{stage}/GithubPat --secret-string <pat>"
            ),
            secret_string_value=cdk.SecretValue.unsafe_plain_text("REPLACE_ME"),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.chainlit_auth_secret = secretsmanager.Secret(
            self,
            "ChainlitAuthSecret",
            secret_name=f"DataAnalystAgent/{stage}/ChainlitAuthSecret",
            description="Signs Chainlit session cookies; stable across restarts so users stay logged in",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True,
                password_length=64,
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # =====================================================================
        # SANDBOX TASK ROLE + TASK DEF (must precede agent role's PassRole)
        # =====================================================================
        # Logs-only. The sandbox doesn't talk to AWS APIs at all — the
        # agent does the Athena query and POSTs the CSV in over HTTP.
        sandbox_task_role = iam.Role(
            self,
            "SandboxTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Runtime role for sandbox tasks. Logs only - no AWS API access.",
        )

        sandbox_log_group = logs.LogGroup(
            self,
            "SandboxLogGroup",
            log_group_name=f"/ecs/data-analyst-agent/{stage.lower()}/sandbox",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # `family` is the human-stable part of the task definition ARN
        # (the part the agent's pool reads via _family_from_task_definition_arn).
        # Stable across revisions — every `deploy_sandbox.sh` push
        # registers a new revision under the same family.
        sandbox_task_family = f"DataAnalystSandbox-{stage}"

        sandbox_task_def = ecs.Ec2TaskDefinition(
            self,
            "SandboxTaskDef",
            family=sandbox_task_family,
            task_role=sandbox_task_role,
            network_mode=ecs.NetworkMode.AWS_VPC,
        )

        # CPU/memory live on the container in EC2 launch type (vs the
        # task in Fargate). `memory_limit_mib` is a hard limit; the kernel
        # will OOMKill the container at this number — generous default
        # (2 GB) lets pandas/plotly breathe with mid-sized dataframes.
        #
        # We deliberately do NOT mount a tmpfs at /workspace. Earlier
        # iterations did, on the theory that LLM-authored data should
        # live in RAM and never touch disk. In practice the tmpfs is
        # mounted as root, which makes it unwritable for our non-root
        # `runner` user — every /write_files call returned 500. The
        # container's writable layer is just as ephemeral in our
        # task-per-session model (ECS reclaims the disk slice when the
        # task stops) and the Dockerfile already chowns /workspace to
        # the runner user, so plain disk-layer writes Just Work.
        # `init_process_enabled=True` keeps tini as PID 1 so child
        # processes (the IPython kernel) get reaped cleanly on shutdown.
        sandbox_linux_params = ecs.LinuxParameters(
            self,
            "SandboxLinuxParameters",
            init_process_enabled=True,
        )

        sandbox_task_def.add_container(
            SANDBOX_CONTAINER_NAME,
            container_name=SANDBOX_CONTAINER_NAME,
            image=ecs.ContainerImage.from_ecr_repository(sandbox_repo, tag="latest"),
            cpu=sandbox_cpu,
            memory_limit_mib=sandbox_memory_mib,
            essential=True,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="sandbox",
                log_group=sandbox_log_group,
            ),
            port_mappings=[
                ecs.PortMapping(container_port=SANDBOX_HTTP_PORT, protocol=ecs.Protocol.TCP),
            ],
            # SANDBOX_AUTH_TOKEN intentionally NOT set here — the agent's
            # pool injects it per-RunTask via containerOverrides, with a
            # value generated fresh per agent process. Setting it here
            # would bake one token into the task definition.
            environment={
                "SANDBOX_LOG_LEVEL": "INFO",
            },
            linux_parameters=sandbox_linux_params,
        )

        # =====================================================================
        # AGENT TASK ROLE — Bedrock, Athena, Glue, Sandbox lifecycle, secrets
        # =====================================================================
        agent_task_role = iam.Role(
            self,
            "AgentTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Runtime role for the data_analyst_agent ECS task",
        )

        # Glue/S3/Logs/PassRole, lifted from the policy doc.
        glue_policy_json = json.loads(_GLUE_POLICY_PATH.read_text())
        for statement_json in glue_policy_json.get("Statement", []):
            agent_task_role.add_to_policy(iam.PolicyStatement.from_json(statement_json))

        # Bedrock model invocation. Scoped to InvokeModel* for now;
        # tighten to specific model ARNs in prod.
        agent_task_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockModelInvoke",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=["*"],
            )
        )

        # Sandbox lifecycle. Replaces the old AgentCore Code Interpreter
        # block — that namespace is blocked at work, so we run sandboxes
        # ourselves via `ecs:RunTask`.
        agent_task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SandboxEcsLifecycle",
                actions=[
                    "ecs:RunTask",
                    "ecs:StopTask",
                    "ecs:DescribeTasks",
                    "ecs:ListTasks",
                    "ecs:ListTagsForResource",
                ],
                resources=[
                    # Match every revision of the sandbox task def family.
                    f"arn:aws:ecs:{self.region}:{self.account}:task-definition/{sandbox_task_family}:*",
                    # All running tasks under this cluster (the pool's
                    # ListTasks call narrows to the family at the API level).
                    f"arn:aws:ecs:{self.region}:{self.account}:task/{self.cluster.cluster_name}/*",
                    # Cluster ARN itself, for ListTasks scoping.
                    self.cluster.cluster_arn,
                ],
            )
        )

        # TagResource is what the pool calls (implicitly, via RunTask's
        # `tags=[...]`). The condition `ecs:CreateAction=RunTask` pins
        # the action to RunTask context only — even if some future code
        # path tried to use this elsewhere, it'd fail.
        agent_task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SandboxTagOnRunTask",
                actions=["ecs:TagResource"],
                resources=["*"],
                conditions={"StringEquals": {"ecs:CreateAction": "RunTask"}},
            )
        )

        # PassRole on the sandbox task role + the auto-created exec role.
        # Required because RunTask needs to attach those roles to the
        # launched task on behalf of the caller.
        agent_task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SandboxPassRole",
                actions=["iam:PassRole"],
                resources=[
                    sandbox_task_role.role_arn,
                    sandbox_task_def.execution_role.role_arn if sandbox_task_def.execution_role else "*",
                ],
                conditions={
                    "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
                },
            )
        )

        # ec2:DescribeNetworkInterfaces is a fallback path for IP discovery
        # if the task attachment shape ever changes. The pool currently
        # reads the IP straight off DescribeTasks attachments, but having
        # this permission means we can fall back without a redeploy.
        agent_task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SandboxEniDescribe",
                actions=["ec2:DescribeNetworkInterfaces"],
                resources=["*"],  # API doesn't support resource conditions.
            )
        )

        # Athena. Covers all four MCP tool surfaces the agent uses:
        # databases/tables (catalog metadata), query executions,
        # named queries, workgroups. Underlying Glue Data Catalog
        # reads (GetTables, GetPartitions, etc.) come from the Glue
        # JSON policy above.
        agent_task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AthenaFullAccessForDev",
                actions=[
                    # Query lifecycle
                    "athena:StartQueryExecution",
                    "athena:StopQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                    "athena:GetQueryResultsStream",
                    "athena:ListQueryExecutions",
                    "athena:BatchGetQueryExecution",
                    # Database / table / catalog metadata
                    "athena:GetDatabase",
                    "athena:ListDatabases",
                    "athena:GetTableMetadata",
                    "athena:ListTableMetadata",
                    "athena:GetDataCatalog",
                    "athena:ListDataCatalogs",
                    "athena:ListEngineVersions",
                    # Named queries (the saved-SQL store)
                    "athena:CreateNamedQuery",
                    "athena:DeleteNamedQuery",
                    "athena:GetNamedQuery",
                    "athena:UpdateNamedQuery",
                    "athena:ListNamedQueries",
                    "athena:BatchGetNamedQuery",
                    # Workgroups
                    "athena:GetWorkGroup",
                    "athena:ListWorkGroups",
                    "athena:CreateWorkGroup",
                    "athena:UpdateWorkGroup",
                    "athena:DeleteWorkGroup",
                    # Tagging
                    "athena:ListTagsForResource",
                    "athena:TagResource",
                    "athena:UntagResource",
                ],
                resources=["*"],
            )
        )

        # Read the SM secrets at startup.
        self.service_auth_secret.grant_read(agent_task_role)
        self.github_pat_secret.grant_read(agent_task_role)

        # =====================================================================
        # PHOENIX: task role, task def, ALB, service
        # =====================================================================
        # Self-hosted Arize Phoenix for tracing + offline experiments +
        # dataset versioning. One container, persisted to a logical DB
        # on the existing RDS instance (`PHOENIX_DATABASE_NAME`, created
        # post-deploy via `scripts/bootstrap_phoenix_db.sh` — RDS doesn't
        # expose CREATE DATABASE as IaC).
        #
        # Built BEFORE the agent task def so the agent's OTLP env vars
        # can reference `self.phoenix_alb.load_balancer_dns_name`.
        #
        # Auth is OFF for the initial deploy. Access during dev is via
        # `aws ssm start-session ... AWS-StartPortForwardingSessionToRemoteHost`
        # to the Phoenix ALB DNS. Cognito-fronted public UI is a
        # separate follow-up.
        phoenix_task_role = iam.Role(
            self,
            "PhoenixTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Runtime role for the Phoenix observability ECS task",
        )
        db_secret.grant_read(phoenix_task_role)

        phoenix_log_group = logs.LogGroup(
            self,
            "PhoenixLogGroup",
            log_group_name=f"/ecs/data-analyst-agent/{stage.lower()}/phoenix",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        phoenix_task_def = ecs.Ec2TaskDefinition(
            self,
            "PhoenixTaskDef",
            task_role=phoenix_task_role,
            network_mode=ecs.NetworkMode.AWS_VPC,
        )

        phoenix_task_def.add_container(
            "phoenix",
            container_name="phoenix",
            # Pinned tag in PHOENIX_IMAGE_TAG. Bump deliberately —
            # storage migrations run under a write lock at boot.
            image=ecs.ContainerImage.from_registry(f"arizephoenix/phoenix:{PHOENIX_IMAGE_TAG}"),
            cpu=PHOENIX_TASK_CPU,
            memory_limit_mib=PHOENIX_TASK_MEMORY_MIB,
            essential=True,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="phoenix",
                log_group=phoenix_log_group,
            ),
            port_mappings=[
                ecs.PortMapping(container_port=PHOENIX_HTTP_PORT, protocol=ecs.Protocol.TCP),
            ],
            environment={
                # Phoenix bundles UI + OTLP/HTTP ingest at /v1/traces on
                # one port. We don't use the gRPC OTLP port (4317) — the
                # ALB stays simple as HTTP-only.
                "PHOENIX_PORT": str(PHOENIX_HTTP_PORT),
                "PHOENIX_HOST": "0.0.0.0",
                "PHOENIX_POSTGRES_DB": PHOENIX_DATABASE_NAME,
                # Working dir is for transient artifacts only; durable
                # state goes to Postgres. /tmp is writable in the
                # arizephoenix container without extra volumes.
                "PHOENIX_WORKING_DIR": "/tmp/phoenix",
                # Suppress the first-run telemetry prompt.
                "PHOENIX_ENABLE_PROMETHEUS": "false",
            },
            secrets={
                # The RDS-generated secret stores credentials as JSON;
                # we lift individual fields with `field=`. Phoenix accepts
                # these env vars and constructs its SQLAlchemy URL itself.
                "PHOENIX_POSTGRES_HOST": ecs.Secret.from_secrets_manager(
                    db_secret, field="host"
                ),
                "PHOENIX_POSTGRES_PORT": ecs.Secret.from_secrets_manager(
                    db_secret, field="port"
                ),
                "PHOENIX_POSTGRES_USER": ecs.Secret.from_secrets_manager(
                    db_secret, field="username"
                ),
                "PHOENIX_POSTGRES_PASSWORD": ecs.Secret.from_secrets_manager(
                    db_secret, field="password"
                ),
            },
        )

        self.phoenix_alb = elbv2.ApplicationLoadBalancer(
            self,
            "PhoenixAlb",
            vpc=vpc,
            internet_facing=False,
            security_group=phoenix_alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            idle_timeout=cdk.Duration.seconds(120),
        )

        phoenix_listener = self.phoenix_alb.add_listener(
            "PhoenixListener",
            port=ALB_HTTP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,
        )

        self.phoenix_service = ecs.Ec2Service(
            self,
            "PhoenixService",
            cluster=self.cluster,
            task_definition=phoenix_task_def,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[phoenix_task_sg],
            # Phoenix runs SQL migrations under a write lock at container
            # boot; first start can take 60-90s. Give generous grace.
            # Single replica only — concurrent boots would race the lock.
            desired_count=1,
            health_check_grace_period=cdk.Duration.seconds(180),
            min_healthy_percent=0,
            max_healthy_percent=100,
            enable_execute_command=True,
        )

        phoenix_listener.add_targets(
            "PhoenixTargets",
            port=PHOENIX_HTTP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[self.phoenix_service],
            health_check=elbv2.HealthCheck(
                # Phoenix returns 200 on `/healthz` once migrations
                # have completed and the server is accepting traffic.
                path="/healthz",
                healthy_http_codes="200",
                # First-boot migrations can take ~60s. Stretch the
                # threshold so the target group doesn't flap during
                # rolling deploys.
                interval=cdk.Duration.seconds(30),
                timeout=cdk.Duration.seconds(10),
                healthy_threshold_count=2,
                unhealthy_threshold_count=5,
            ),
            deregistration_delay=cdk.Duration.seconds(15),
        )

        # =====================================================================
        # AGENT TASK DEF + ECS SERVICE (Ec2 launch type)
        # =====================================================================
        agent_log_group = logs.LogGroup(
            self,
            "AgentLogGroup",
            log_group_name=f"/ecs/data-analyst-agent/{stage.lower()}/agent",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        agent_task_def = ecs.Ec2TaskDefinition(
            self,
            "AgentTaskDef",
            task_role=agent_task_role,
            network_mode=ecs.NetworkMode.AWS_VPC,
        )

        # Helper: subnet IDs as a CSV string for the agent's
        # SANDBOX_SUBNET_IDS env var. CDK tokenizes the list, so we
        # use Fn.join via cdk.Fn.join.
        private_subnet_ids = vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        ).subnet_ids
        sandbox_subnet_ids_csv = cdk.Fn.join(",", private_subnet_ids)

        agent_task_def.add_container(
            "agent",
            container_name="agent",
            image=ecs.ContainerImage.from_ecr_repository(agent_repo, tag="latest"),
            cpu=AGENT_TASK_CPU,
            memory_limit_mib=AGENT_TASK_MEMORY_MIB,
            essential=True,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="agent",
                log_group=agent_log_group,
            ),
            port_mappings=[
                ecs.PortMapping(container_port=AGENT_HTTP_PORT, protocol=ecs.Protocol.TCP),
            ],
            environment={
                "AWS_REGION": self.region,
                "MODEL_ID": self.node.try_get_context("model_id") or "",
                "GLUE_JOB_ROLE_ARN": self.node.try_get_context("glue_job_role_arn") or "",
                "SCHEDULER_ATHENA_EXEC_ROLE_ARN": self.node.try_get_context("scheduler_athena_exec_role_arn") or "",
                "GLUE_JOB_DEFAULT_SCRIPT_S3": self.node.try_get_context("glue_job_default_script_s3") or "",
                "GLUE_TEMP_DIR": self.node.try_get_context("glue_temp_dir") or "",
                "ATHENA_DATABASE": self.node.try_get_context("athena_database") or "",
                "ATHENA_TABLE": self.node.try_get_context("athena_table") or "",
                "TARGET_REPO_OWNER": self.node.try_get_context("target_repo_owner") or "",
                "TARGET_REPO_NAME": self.node.try_get_context("target_repo_name") or "",
                "TARGET_REPO_DEFAULT_BRANCH": self.node.try_get_context("target_repo_default_branch") or "main",
                "RAW_DATA_BUCKET_S3_URI": self.node.try_get_context("raw_data_bucket_s3_uri") or "",
                "AGENT_LOG_LEVEL": "INFO",
                # ----- Sandbox pool wiring (CDK-resolved, no runtime SSM lookups) -----
                "SANDBOX_CLUSTER_NAME": self.cluster.cluster_name,
                "SANDBOX_TASK_DEFINITION_ARN": sandbox_task_def.task_definition_arn,
                "SANDBOX_SUBNET_IDS": sandbox_subnet_ids_csv,
                "SANDBOX_SECURITY_GROUP_ID": sandbox_task_sg.security_group_id,
                "SANDBOX_POOL_SIZE": str(sandbox_pool_size),
                "SANDBOX_PORT": str(SANDBOX_HTTP_PORT),
                "SANDBOX_CONTAINER_NAME": SANDBOX_CONTAINER_NAME,
                # ----- Phoenix / OTel wiring -----
                # Strands' setup_otlp_exporter() reads the standard
                # OTEL_* vars. http/protobuf because Phoenix takes OTLP
                # over HTTP at /v1/traces on its UI port — no separate
                # gRPC port needed.
                "AGENT_OTLP_ENABLE": "1",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                "OTEL_EXPORTER_OTLP_ENDPOINT": (
                    f"http://{self.phoenix_alb.load_balancer_dns_name}"
                ),
                # 5s default flush is too laggy for snappy "View trace"
                # demos; 1s keeps traces appearing while still batching.
                "OTEL_BSP_SCHEDULE_DELAY": "1000",
                # Identifies the build in every span attribute.
                # `agent_version` context is set by deploy_agent.sh from
                # `git rev-parse --short HEAD`; fallback to "deployed"
                # when not provided so we never emit empty.
                "AGENT_VERSION": (
                    self.node.try_get_context("agent_version") or "deployed"
                ),
            },
            secrets={
                "AGENT_SERVICE_AUTH_SECRET": ecs.Secret.from_secrets_manager(self.service_auth_secret),
                "GITHUB_PAT": ecs.Secret.from_secrets_manager(self.github_pat_secret),
            },
        )

        self.agent_alb = elbv2.ApplicationLoadBalancer(
            self,
            "AgentAlb",
            vpc=vpc,
            internet_facing=False,
            security_group=agent_alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            idle_timeout=cdk.Duration.seconds(120),
        )

        agent_listener = self.agent_alb.add_listener(
            "AgentListener",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,  # SG-controlled, not 0.0.0.0/0
        )

        # Ec2Service uses the cluster's default capacity provider
        # strategy (set above), so no explicit strategy here.
        self.agent_service = ecs.Ec2Service(
            self,
            "AgentService",
            cluster=self.cluster,
            task_definition=agent_task_def,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[agent_task_sg],
            health_check_grace_period=cdk.Duration.seconds(60),
            min_healthy_percent=0,  # allow 0 -> 1 transition without rolling-deploy back-pressure
            max_healthy_percent=200,
            enable_execute_command=True,
        )

        agent_listener.add_targets(
            "AgentTargets",
            port=AGENT_HTTP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[self.agent_service],
            health_check=elbv2.HealthCheck(
                path="/healthz",
                healthy_http_codes="200",
                interval=cdk.Duration.seconds(15),
                timeout=cdk.Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
            deregistration_delay=cdk.Duration.seconds(15),
        )

        # =====================================================================
        # FRONTEND: task role, task def, ALB (HTTPS + Cognito), service
        # =====================================================================
        frontend_task_role = iam.Role(
            self,
            "FrontendTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Runtime role for the Chainlit frontend ECS task",
        )

        db_secret.grant_read(frontend_task_role)
        self.service_auth_secret.grant_read(frontend_task_role)
        self.chainlit_auth_secret.grant_read(frontend_task_role)

        frontend_log_group = logs.LogGroup(
            self,
            "FrontendLogGroup",
            log_group_name=f"/ecs/data-analyst-agent/{stage.lower()}/frontend",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        frontend_task_def = ecs.Ec2TaskDefinition(
            self,
            "FrontendTaskDef",
            task_role=frontend_task_role,
            network_mode=ecs.NetworkMode.AWS_VPC,
        )

        frontend_task_def.add_container(
            "frontend",
            container_name="frontend",
            image=ecs.ContainerImage.from_ecr_repository(frontend_repo, tag="latest"),
            cpu=FRONTEND_TASK_CPU,
            memory_limit_mib=FRONTEND_TASK_MEMORY_MIB,
            essential=True,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="frontend",
                log_group=frontend_log_group,
            ),
            port_mappings=[
                ecs.PortMapping(container_port=FRONTEND_HTTP_PORT, protocol=ecs.Protocol.TCP),
            ],
            environment={
                "AWS_REGION": self.region,
                # Same-stack reference: agent ALB's DNS resolves to the
                # internal IP. http://<dns> with no port = port 80, where
                # the agent's listener forwards to container port 8080.
                "AGENT_BASE_URL": f"http://{self.agent_alb.load_balancer_dns_name}",
                "AGENT_REQUEST_TIMEOUT_SECONDS": "600",
                "DB_SECRET_ARN": db_secret.secret_arn,
                "DEPLOYED_BEHIND_ALB": "1",
            },
            secrets={
                "AGENT_SERVICE_AUTH_SECRET": ecs.Secret.from_secrets_manager(self.service_auth_secret),
                "CHAINLIT_AUTH_SECRET": ecs.Secret.from_secrets_manager(self.chainlit_auth_secret),
            },
        )

        self.frontend_alb = elbv2.ApplicationLoadBalancer(
            self,
            "FrontendAlb",
            vpc=vpc,
            internet_facing=True,
            security_group=frontend_alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            idle_timeout=cdk.Duration.seconds(120),
        )

        self.frontend_certificate = acm.Certificate(
            self,
            "FrontendCertificate",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        self.frontend_alb.add_listener(
            "FrontendHttpRedirect",
            port=ALB_HTTP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,
            default_action=elbv2.ListenerAction.redirect(
                protocol="HTTPS",
                port=str(ALB_HTTPS_PORT),
                permanent=True,
            ),
        )

        frontend_listener = self.frontend_alb.add_listener(
            "FrontendHttpsListener",
            port=ALB_HTTPS_PORT,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            open=False,
            certificates=[
                elbv2.ListenerCertificate.from_certificate_manager(self.frontend_certificate),
            ],
        )

        route53.ARecord(
            self,
            "FrontendAlias",
            zone=hosted_zone,
            record_name=domain_name,
            target=route53.RecordTarget.from_alias(
                route53_targets.LoadBalancerTarget(self.frontend_alb)
            ),
        )

        self.frontend_service = ecs.Ec2Service(
            self,
            "FrontendService",
            cluster=self.cluster,
            task_definition=frontend_task_def,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[frontend_task_sg],
            health_check_grace_period=cdk.Duration.seconds(60),
            min_healthy_percent=0,
            max_healthy_percent=200,
            enable_execute_command=True,
        )

        frontend_target_group = elbv2.ApplicationTargetGroup(
            self,
            "FrontendTargetGroup",
            vpc=vpc,
            port=FRONTEND_HTTP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            targets=[self.frontend_service],
            health_check=elbv2.HealthCheck(
                path="/",
                healthy_http_codes="200",
                interval=cdk.Duration.seconds(15),
                timeout=cdk.Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
            deregistration_delay=cdk.Duration.seconds(15),
            stickiness_cookie_duration=cdk.Duration.hours(1),
        )

        frontend_listener.add_action(
            "FrontendDefaultAction",
            action=elbv2_actions.AuthenticateCognitoAction(
                user_pool=user_pool,
                user_pool_client=user_pool_client,
                user_pool_domain=user_pool_domain,
                next=elbv2.ListenerAction.forward([frontend_target_group]),
                session_timeout=cdk.Duration.hours(1),
            ),
        )

        # =====================================================================
        # SSM PARAMETERS — read by deploy scripts and (where useful) at runtime
        # =====================================================================
        ssm.StringParameter(
            self,
            "ClusterNameParam",
            parameter_name=f"{ssm_prefix}/cluster-name",
            string_value=self.cluster.cluster_name,
        )
        ssm.StringParameter(
            self,
            "AgentServiceNameParam",
            parameter_name=f"{ssm_prefix}/agent/service-name",
            string_value=self.agent_service.service_name,
        )
        ssm.StringParameter(
            self,
            "AgentAlbDnsParam",
            parameter_name=f"{ssm_prefix}/agent/alb-dns",
            string_value=self.agent_alb.load_balancer_dns_name,
        )
        ssm.StringParameter(
            self,
            "ServiceAuthSecretArnParam",
            parameter_name=f"{ssm_prefix}/service-auth-secret-arn",
            string_value=self.service_auth_secret.secret_arn,
        )

        ssm.StringParameter(
            self,
            "FrontendServiceNameParam",
            parameter_name=f"{ssm_prefix}/frontend/service-name",
            string_value=self.frontend_service.service_name,
        )
        ssm.StringParameter(
            self,
            "FrontendAlbDnsParam",
            parameter_name=f"{ssm_prefix}/frontend/alb-dns",
            string_value=self.frontend_alb.load_balancer_dns_name,
        )
        ssm.StringParameter(
            self,
            "DbSecretArnParam",
            parameter_name=f"{ssm_prefix}/db-secret-arn",
            string_value=db_secret.secret_arn,
        )
        ssm.StringParameter(
            self,
            "ChainlitAuthSecretArnParam",
            parameter_name=f"{ssm_prefix}/chainlit-auth-secret-arn",
            string_value=self.chainlit_auth_secret.secret_arn,
        )

        # Sandbox params: not strictly needed at runtime (the agent reads
        # them from env vars CDK injects on the agent task), but useful
        # for `aws ssm get-parameter` from the dev laptop when debugging.
        ssm.StringParameter(
            self,
            "SandboxTaskDefArnParam",
            parameter_name=f"{ssm_prefix}/sandbox/task-definition-arn",
            string_value=sandbox_task_def.task_definition_arn,
        )
        ssm.StringParameter(
            self,
            "SandboxSecurityGroupIdParam",
            parameter_name=f"{ssm_prefix}/sandbox/security-group-id",
            string_value=sandbox_task_sg.security_group_id,
        )
        ssm.StringParameter(
            self,
            "SandboxSubnetIdsParam",
            parameter_name=f"{ssm_prefix}/sandbox/subnet-ids",
            string_value=sandbox_subnet_ids_csv,
        )
        ssm.StringParameter(
            self,
            "SandboxPoolSizeParam",
            parameter_name=f"{ssm_prefix}/sandbox/pool-size",
            string_value=str(sandbox_pool_size),
        )
        ssm.StringParameter(
            self,
            "SandboxPortParam",
            parameter_name=f"{ssm_prefix}/sandbox/port",
            string_value=str(SANDBOX_HTTP_PORT),
        )

        # Phoenix params: dev laptop uses these for SSM port-forwarding
        # to the internal Phoenix ALB during eval / debugging sessions.
        ssm.StringParameter(
            self,
            "PhoenixServiceNameParam",
            parameter_name=f"{ssm_prefix}/phoenix/service-name",
            string_value=self.phoenix_service.service_name,
        )
        ssm.StringParameter(
            self,
            "PhoenixAlbDnsParam",
            parameter_name=f"{ssm_prefix}/phoenix/alb-dns",
            string_value=self.phoenix_alb.load_balancer_dns_name,
        )
        ssm.StringParameter(
            self,
            "PhoenixUiUrlParam",
            parameter_name=f"{ssm_prefix}/phoenix/ui-url",
            string_value=f"http://{self.phoenix_alb.load_balancer_dns_name}",
        )
        ssm.StringParameter(
            self,
            "PhoenixOtlpEndpointParam",
            parameter_name=f"{ssm_prefix}/phoenix/otlp-endpoint",
            string_value=f"http://{self.phoenix_alb.load_balancer_dns_name}",
        )

        # Top-level convenience output: public HTTPS URL behind Cognito.
        cdk.CfnOutput(
            self,
            "FrontendUrl",
            value=f"https://{domain_name}",
            description="Public chat UI; first-time users get bounced through Cognito's hosted login.",
        )
