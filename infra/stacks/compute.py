"""Compute stack: ECS cluster + agent service + frontend service + 2 ALBs.

Both halves of the runtime live here. The frontend's `AGENT_BASE_URL`
is the agent ALB's DNS name — set as a same-stack reference, no cross-
stack export/import needed.

What lives here:
  - Single Fargate cluster (both services use it).
  - Service-to-service auth secret in Secrets Manager. Auto-generated;
    plaintext never appears anywhere. Agent reads it to validate the
    X-Service-Auth header; frontend reads it to attach the header.
  - Chainlit auth secret (signs Chainlit session cookies). Auto-generated;
    must be stable across restarts or users get logged out.
  - Placeholder GitHub PAT secret. CDK creates it with a placeholder;
    user populates the real PAT once via
    `aws secretsmanager put-secret-value`.

  - Agent task IAM role with the Glue/S3/Logs policy from
    infra/policies/data_analyst_access.json plus Bedrock
    InvokeModel, AgentCore Code Interpreter, and Athena (the standing
    TODO from infra/policies/README.md is now closed for this use case).
  - Agent task definition (0.5 vCPU / 1 GB Fargate), 14-day log retention.
  - Agent ECS service, desired_count=0 (first cdk deploy succeeds before
    any image push); Phase 5's deploy_agent.sh bumps to 1.
  - Internal ALB on port 80, target group health-checking /healthz on
    container port 8080.

  - Frontend task IAM role with read access to the DB secret +
    service-auth secret + chainlit-auth secret.
  - Frontend task definition (0.25 vCPU / 0.5 GB Fargate, smaller than
    agent — Chainlit is lighter than the LLM event loop), 14-day logs.
  - Frontend ECS service, desired_count=0 (same first-deploy logic).
  - Internal ALB on port 80, target group health-checking / on container
    port 8000. Stickiness enabled (Chainlit uses websockets).

  - Cluster name + service names + ALB DNS + repo URIs + secret ARNs all
    written to SSM Parameter Store, so Phase 5's deploy scripts can read
    them with one `aws ssm get-parameter` call each.
"""

import json
from pathlib import Path

import aws_cdk as cdk
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
)

# Path to the Glue/S3/Logs/PassRole policy doc lifted from the original
# AgentCore deploy script. Authoritative source for the agent's deployed
# IAM permissions; keeping it as JSON instead of CDK code lets the same
# document be reused in other tooling (e.g. Terraform if we ever add it).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_GLUE_POLICY_PATH = _REPO_ROOT / "infra" / "policies" / "data_analyst_access.json"

AGENT_TASK_CPU = 512  # 0.5 vCPU
AGENT_TASK_MEMORY_MIB = 1024  # 1 GB
FRONTEND_TASK_CPU = 256  # 0.25 vCPU
FRONTEND_TASK_MEMORY_MIB = 512  # 0.5 GB


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
        agent_repo: ecr.IRepository,
        frontend_repo: ecr.IRepository,
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

        # --- Cluster -------------------------------------------------------
        self.cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.DISABLED,
        )

        # --- Service-to-service auth secret -------------------------------
        # Generated string, passed as the X-Service-Auth header value.
        # Agent grants itself read access here; frontend will do the same
        # in Phase 4f.
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

        # --- GitHub PAT placeholder ----------------------------------------
        # We can't generate this — it's a token issued by GitHub. CDK
        # creates the SM entry with a placeholder; the user populates the
        # real value once before the agent's GitHub-using flows are
        # exercised. The agent's `make_github_mcp_client` raises clearly
        # if GITHUB_PAT is empty, so a missing real value fails loud
        # rather than silently.
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

        # --- Agent task IAM role ------------------------------------------
        agent_task_role = iam.Role(
            self,
            "AgentTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Runtime role for the data_analyst_agent ECS task",
        )

        # Glue/S3/Logs/PassRole, lifted verbatim from the previous AgentCore
        # deploy script. Convert each Statement entry into a PolicyStatement
        # and attach so the Role construct picks them up like any other
        # inline policy.
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

        # AgentCore Code Interpreter sandbox (used by the display tools
        # and the in-agent code runs). Broad for dev; narrow to specific
        # interpreter resources in prod.
        agent_task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreCodeInterpreter",
                actions=[
                    "bedrock-agentcore:CreateCodeInterpreter",
                    "bedrock-agentcore:GetCodeInterpreter",
                    "bedrock-agentcore:ListCodeInterpreters",
                    "bedrock-agentcore:StartCodeInterpreterSession",
                    "bedrock-agentcore:StopCodeInterpreterSession",
                    "bedrock-agentcore:InvokeCodeInterpreter",
                ],
                resources=["*"],
            )
        )

        # Athena. Covers all four MCP tool surfaces the agent uses:
        # databases/tables (catalog metadata), query executions,
        # named queries, workgroups. Underlying Glue Data Catalog
        # reads (GetTables, GetPartitions, etc.) come from the Glue
        # JSON policy above — Athena's ListTableMetadata calls into
        # Glue and needs both sides' permissions.
        #
        # Resource: "*" is dev-scoped. For prod, narrow to specific
        # workgroup ARNs (athena:*) and catalog ARNs (athena:GetDataCatalog
        # etc.) plus the corresponding glue:* on specific catalogs/
        # databases.
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
                    # Database / table / catalog metadata — these
                    # are Athena's catalog APIs (separate from Glue)
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

        # Read the two SM secrets at startup.
        self.service_auth_secret.grant_read(agent_task_role)
        self.github_pat_secret.grant_read(agent_task_role)

        # --- Agent log group ---------------------------------------------
        agent_log_group = logs.LogGroup(
            self,
            "AgentLogGroup",
            log_group_name=f"/ecs/data-analyst-agent/{stage.lower()}/agent",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # --- Agent task definition ---------------------------------------
        agent_task_def = ecs.FargateTaskDefinition(
            self,
            "AgentTaskDef",
            cpu=AGENT_TASK_CPU,
            memory_limit_mib=AGENT_TASK_MEMORY_MIB,
            task_role=agent_task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        # ECS pulls images via the *execution* role, which CDK
        # auto-creates and auto-grants pull on when we attach an
        # `ecs.ContainerImage.from_ecr_repository` container below.
        # No explicit grant_pull call needed.

        # Agent container. Image tag :latest is what Phase 5's deploy
        # script will push. Until then the service has desired_count=0
        # so this isn't pulled.
        agent_task_def.add_container(
            "agent",
            container_name="agent",
            image=ecs.ContainerImage.from_ecr_repository(agent_repo, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="agent",
                log_group=agent_log_group,
            ),
            port_mappings=[
                ecs.PortMapping(container_port=AGENT_HTTP_PORT, protocol=ecs.Protocol.TCP),
            ],
            # Non-secret configuration. Empty defaults so synth + first
            # deploy work without context flags; populate via cdk
            # context (see infra/cdk.json or --context CLI flag) before
            # exercising the GitHub flows.
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
            },
            # Secrets land as env vars too, but ECS injects them at
            # task launch from SM — they never appear in the task
            # definition JSON.
            secrets={
                "AGENT_SERVICE_AUTH_SECRET": ecs.Secret.from_secrets_manager(self.service_auth_secret),
                "GITHUB_PAT": ecs.Secret.from_secrets_manager(self.github_pat_secret),
            },
        )

        # --- Internal ALB --------------------------------------------------
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

        # --- ECS service --------------------------------------------------
        # desired_count=0 lets the stack deploy cleanly before any image
        # exists. Phase 5's deploy_agent.sh bumps to 1 after first push.
        self.agent_service = ecs.FargateService(
            self,
            "AgentService",
            cluster=self.cluster,
            task_definition=agent_task_def,
            # Intentionally omit desired_count: deploy.sh manages it
            # (bumps from CFN's default of 1 if needed; subsequent
            # cdk deploys don't reset it because CDK doesn't write the
            # field). First deploy ever has tasks failing to start
            # for ~minutes until images are pushed; that's expected
            # and harmless.
            assign_public_ip=False,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[agent_task_sg],
            health_check_grace_period=cdk.Duration.seconds(60),
            min_healthy_percent=0,  # allow 0 -> 1 transition without rolling-deploy back-pressure
            max_healthy_percent=200,
            # Enables `aws ecs execute-command` and SSM port-forwarding
            # through running tasks. Lets you reach the agent's /healthz
            # from your laptop without a bastion or VPN. CDK auto-grants
            # the SSM channel permissions on the task role.
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

        # --- SSM outputs ---------------------------------------------------
        # Phase 5 deploy scripts read these. SSM beats CFN outputs because
        # `aws ssm get-parameter` is one call with a known path — no need
        # to know stack names or parse CloudFormation responses.
        ssm_prefix = f"/data-analyst-agent/{stage.lower()}"

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
        # Note: agent/repo-uri is written by the Ecr stack so that
        # bootstrap.sh can find it after step 1 (before Compute
        # deploys).
        ssm.StringParameter(
            self,
            "ServiceAuthSecretArnParam",
            parameter_name=f"{ssm_prefix}/service-auth-secret-arn",
            string_value=self.service_auth_secret.secret_arn,
        )

        # =====================================================================
        # FRONTEND HALF (Phase 4f)
        # =====================================================================

        # --- Chainlit auth secret -----------------------------------------
        # Chainlit uses this to sign session cookies. Must be stable across
        # restarts — auto-generate once, store in SM, frontend reads it as
        # CHAINLIT_AUTH_SECRET on every container start.
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

        # --- Frontend task IAM role ---------------------------------------
        frontend_task_role = iam.Role(
            self,
            "FrontendTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Runtime role for the Chainlit frontend ECS task",
        )

        # The frontend reads three SM secrets at startup: DB credentials
        # (to build DATABASE_URL and apply schema), the service-auth
        # secret (to attach as X-Service-Auth on outbound calls), and
        # the Chainlit-auth secret (cookie signing key). All three
        # injected as ECS secrets below; the explicit grant_read calls
        # here are belt-and-braces in case task-role usage broadens.
        db_secret.grant_read(frontend_task_role)
        self.service_auth_secret.grant_read(frontend_task_role)
        self.chainlit_auth_secret.grant_read(frontend_task_role)

        # --- Frontend log group -------------------------------------------
        frontend_log_group = logs.LogGroup(
            self,
            "FrontendLogGroup",
            log_group_name=f"/ecs/data-analyst-agent/{stage.lower()}/frontend",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # --- Frontend task definition -------------------------------------
        frontend_task_def = ecs.FargateTaskDefinition(
            self,
            "FrontendTaskDef",
            cpu=FRONTEND_TASK_CPU,
            memory_limit_mib=FRONTEND_TASK_MEMORY_MIB,
            task_role=frontend_task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        # The frontend's entrypoint reads DB_SECRET_ARN, fetches the JSON
        # secret payload via boto3 at container start, builds DATABASE_URL,
        # applies chainlit_schema.sql idempotently, then exec's chainlit.
        # That entrypoint script is added to frontend/ in a follow-up
        # commit ("Frontend container: schema-bootstrap entrypoint").
        frontend_task_def.add_container(
            "frontend",
            container_name="frontend",
            image=ecs.ContainerImage.from_ecr_repository(frontend_repo, tag="latest"),
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
                # The schema-bootstrap entrypoint reads this and builds
                # DATABASE_URL from the JSON-shaped DB secret.
                "DB_SECRET_ARN": db_secret.secret_arn,
                # Tells frontend/app.py to use header_auth_callback (read
                # the Cognito JWT from x-amzn-oidc-data) instead of the
                # local password_auth_callback (admin/admin).
                "DEPLOYED_BEHIND_ALB": "1",
            },
            secrets={
                "AGENT_SERVICE_AUTH_SECRET": ecs.Secret.from_secrets_manager(self.service_auth_secret),
                "CHAINLIT_AUTH_SECRET": ecs.Secret.from_secrets_manager(self.chainlit_auth_secret),
            },
        )

        # --- Frontend ALB (public, HTTPS, Cognito-fronted) ----------------
        # Public-facing because Cognito's hosted UI redirects to the ALB
        # over the public internet; an internal ALB couldn't receive the
        # callback. SG (frontend_alb_sg) opens 443 + 80 to anywhere — the
        # actual access control happens at the listener via
        # authenticate-cognito.
        self.frontend_alb = elbv2.ApplicationLoadBalancer(
            self,
            "FrontendAlb",
            vpc=vpc,
            internet_facing=True,
            security_group=frontend_alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            idle_timeout=cdk.Duration.seconds(120),
        )

        # ACM cert for the FQDN, DNS-validated via the delegated Route 53
        # zone. CDK auto-creates the validation CNAME records in the zone
        # and waits for ACM to issue (5-30 min on first deploy).
        self.frontend_certificate = acm.Certificate(
            self,
            "FrontendCertificate",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        # Plain HTTP listener — only purpose is to permanent-redirect
        # to HTTPS. Real traffic only ever uses 443.
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

        # HTTPS listener with the authenticate-cognito action wrapping
        # the forward to the frontend target group. Every request (other
        # than the ALB's internal /oauth2/idpresponse callback path,
        # which it handles automatically) must pass Cognito auth before
        # reaching Chainlit. Target-group health checks come from the
        # ALB internally and bypass listener actions, so /healthz works
        # without auth.
        frontend_listener = self.frontend_alb.add_listener(
            "FrontendHttpsListener",
            port=ALB_HTTPS_PORT,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            open=False,
            certificates=[
                elbv2.ListenerCertificate.from_certificate_manager(self.frontend_certificate),
            ],
        )

        # Route 53 alias record pointing the FQDN at the ALB. CDK creates
        # an A-record-with-alias which is free (no DNS query charges
        # for AWS-internal targets).
        route53.ARecord(
            self,
            "FrontendAlias",
            zone=hosted_zone,
            record_name=domain_name,
            target=route53.RecordTarget.from_alias(
                route53_targets.LoadBalancerTarget(self.frontend_alb)
            ),
        )

        # --- Frontend ECS service -----------------------------------------
        self.frontend_service = ecs.FargateService(
            self,
            "FrontendService",
            cluster=self.cluster,
            task_definition=frontend_task_def,
            # Same as the agent service: omit desired_count so cdk deploys
            # don't reset whatever deploy.sh / `aws ecs update-service`
            # set it to. See the agent_service block above for the full
            # rationale.
            assign_public_ip=False,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[frontend_task_sg],
            health_check_grace_period=cdk.Duration.seconds(60),
            min_healthy_percent=0,
            max_healthy_percent=200,
            # Same reason as the agent service — enables SSM port-forward
            # into a running frontend task so you can open the UI from
            # your laptop. Without this, the internal ALB is literally
            # unreachable from outside the VPC.
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
                # Chainlit's index page returns 200 for an authenticated
                # OR unauthenticated user (login form is the body when
                # unauthenticated), so / is a fine health-check target.
                path="/",
                healthy_http_codes="200",
                interval=cdk.Duration.seconds(15),
                timeout=cdk.Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
            deregistration_delay=cdk.Duration.seconds(15),
            # Chainlit uses websockets for streaming; same browser tab
            # must keep hitting the same task. LB-cookie stickiness for
            # an hour is plenty for any single chat session.
            stickiness_cookie_duration=cdk.Duration.hours(1),
        )

        # Wire the listener default action: authenticate via Cognito,
        # then forward to the frontend target group. ALB handles the
        # OAuth code-grant dance automatically using the user pool +
        # client + domain.
        frontend_listener.add_action(
            "FrontendDefaultAction",
            action=elbv2_actions.AuthenticateCognitoAction(
                user_pool=user_pool,
                user_pool_client=user_pool_client,
                user_pool_domain=user_pool_domain,
                next=elbv2.ListenerAction.forward([frontend_target_group]),
                # Session lasts an hour — matches the access token TTL
                # in the Cognito client. After that the user gets
                # bounced back to Cognito for a fresh token.
                session_timeout=cdk.Duration.hours(1),
            ),
        )

        # --- Frontend SSM outputs ----------------------------------------
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
        # Note: frontend/repo-uri is written by the Ecr stack (see
        # AgentRepoUriParam comment above for the rationale).
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

        # --- Top-level convenience output ---------------------------------
        # Public HTTPS URL behind Cognito auth.
        cdk.CfnOutput(
            self,
            "FrontendUrl",
            value=f"https://{domain_name}",
            description="Public chat UI; first-time users get bounced through Cognito's hosted login.",
        )
