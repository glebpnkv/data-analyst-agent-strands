"""Network stack: VPC + security groups.

Creates the foundation every other stack attaches to:

- A 2-AZ VPC with one public subnet and one private-with-egress subnet
  per AZ. 2 AZs is the floor for RDS subnet groups and ALBs (both refuse
  to come up with fewer), so we'd need it even if ECS could get away
  with one.
- One NAT gateway (down from the CDK default of one per AZ). This is a
  conscious dev-only cost trade-off: ~$32/mo saved at the price of
  outbound from the second AZ failing if the AZ holding the NAT
  blackholes. For prod, set `nat_gateways=2` (or use NAT Instances /
  VPC endpoints).
- Five security groups, one per network role. Their pairwise ingress
  rules are wired here too — the data and compute stacks just consume
  the SGs, they don't add rules.

The VPC and all SGs are exposed as public attributes so subsequent
stacks (passed via app.py) can attach to them directly. CDK auto-
creates the cross-stack exports/imports; we don't manage them by hand.
"""

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

# Service ports — kept here so SGs and the compute stack agree.
# The frontend ALB now serves the public on 443 (HTTPS, behind Cognito);
# port 80 stays open only to redirect plain-HTTP visitors to HTTPS.
# The agent ALB stays internal HTTP-only on 80. SGs MUST gate the ALB
# ingress on the listener port (80 / 443), not the container port —
# otherwise traffic that reaches the ALB is silently dropped at the SG
# before the listener ever sees it.
ALB_HTTP_PORT = 80
ALB_HTTPS_PORT = 443
FRONTEND_HTTP_PORT = 8000
AGENT_HTTP_PORT = 8080
SANDBOX_HTTP_PORT = 8081
PHOENIX_HTTP_PORT = 6006  # Phoenix UI + OTLP/HTTP ingest on /v1/traces
POSTGRES_PORT = 5432


class NetworkStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # ---- Security groups ------------------------------------------------
        # Created without ingress rules first so we can reference each other
        # when wiring rules below (avoids circular construct dependencies).

        self.frontend_alb_sg = ec2.SecurityGroup(
            self,
            "FrontendAlbSg",
            vpc=self.vpc,
            description="Frontend ALB - accepts user traffic on the Chainlit port",
            allow_all_outbound=True,
        )

        self.frontend_task_sg = ec2.SecurityGroup(
            self,
            "FrontendTaskSg",
            vpc=self.vpc,
            description="Frontend ECS tasks (Chainlit). Reachable from the frontend ALB only.",
            allow_all_outbound=True,
        )

        self.agent_alb_sg = ec2.SecurityGroup(
            self,
            "AgentAlbSg",
            vpc=self.vpc,
            description="Agent (internal) ALB - accepts traffic from frontend tasks only",
            allow_all_outbound=True,
        )

        self.agent_task_sg = ec2.SecurityGroup(
            self,
            "AgentTaskSg",
            vpc=self.vpc,
            description="Agent ECS tasks (FastAPI). Reachable from the agent ALB only.",
            allow_all_outbound=True,
        )

        # Sandbox tasks live behind no ALB — the agent claims them
        # directly via private IP and ecs:RunTask. Only the agent task
        # SG can reach them. Outbound is open so the sandbox can pull
        # PyPI deps if SANDBOX_INSTALL_PACKAGES_ENABLED ever flips on,
        # and so the kernel can phone home for any user-requested
        # network operation; in practice the LLM-authored code is the
        # only thing that ever uses it.
        self.sandbox_task_sg = ec2.SecurityGroup(
            self,
            "SandboxTaskSg",
            vpc=self.vpc,
            description="Sandbox ECS tasks. Reachable only from agent tasks; never an ALB.",
            allow_all_outbound=True,
        )

        self.rds_sg = ec2.SecurityGroup(
            self,
            "RdsSg",
            vpc=self.vpc,
            description="RDS Postgres for Chainlit Data Layer + Phoenix. Reachable from frontend + phoenix tasks.",
            allow_all_outbound=False,
        )

        # Phoenix: self-hosted Arize Phoenix for tracing + offline
        # experiments. Internal ALB only — UI access during dev is via
        # SSM port-forward; public Cognito-fronted access is a separate
        # workstream. OTLP/HTTP traces from the agent come in via the
        # same ALB at /v1/traces.
        self.phoenix_alb_sg = ec2.SecurityGroup(
            self,
            "PhoenixAlbSg",
            vpc=self.vpc,
            description="Phoenix internal ALB. Accepts OTLP traces from agent tasks + UI from frontend tasks.",
            allow_all_outbound=True,
        )

        self.phoenix_task_sg = ec2.SecurityGroup(
            self,
            "PhoenixTaskSg",
            vpc=self.vpc,
            description="Phoenix ECS task. Reachable from the Phoenix ALB only; egresses to RDS for storage.",
            allow_all_outbound=True,
        )

        # Session-affinity gateway (HAProxy). Sits between frontend
        # tasks and agent tasks; routes /v1/chat by consistent hash on
        # the X-Session-Id header so the same session always lands on
        # the same agent task while it's healthy.
        self.gateway_alb_sg = ec2.SecurityGroup(
            self,
            "GatewayAlbSg",
            vpc=self.vpc,
            description="Gateway (HAProxy) ALB. Accepts traffic from frontend tasks + VPC for SSM port-forward.",
            allow_all_outbound=True,
        )

        self.gateway_task_sg = ec2.SecurityGroup(
            self,
            "GatewayTaskSg",
            vpc=self.vpc,
            description="Gateway (HAProxy) ECS tasks. Reachable from gateway ALB; talks to agent tasks directly via Cloud Map.",
            allow_all_outbound=True,
        )

        # ---- Pairwise ingress rules ----------------------------------------
        # Trust path (ALBs listen on 80, container ports differ):
        #   user --[ALB_HTTP_PORT=80]--> frontend_alb_sg
        #   frontend_alb_sg --[FRONTEND_HTTP_PORT=8000]--> frontend_task_sg
        #   frontend_task_sg --[ALB_HTTP_PORT=80]--> agent_alb_sg
        #   agent_alb_sg --[AGENT_HTTP_PORT=8080]--> agent_task_sg
        #   frontend_task_sg --[POSTGRES_PORT=5432]--> rds_sg

        # The frontend ALB is now internet-facing (Cognito handles
        # authentication on the listener) — open 443 to anywhere. Port
        # 80 is open only so the ALB's HTTP listener can redirect to
        # HTTPS; we never serve real content over plain HTTP.
        self.frontend_alb_sg.add_ingress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(ALB_HTTPS_PORT),
            description="Public HTTPS to frontend ALB (auth via Cognito at the listener)",
        )
        self.frontend_alb_sg.add_ingress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="Public HTTP to frontend ALB (redirected to HTTPS)",
        )

        # ALB-to-task ingress is on the *container* port (frontend ALB
        # forwards 80 -> 8000).
        self.frontend_task_sg.add_ingress_rule(
            peer=self.frontend_alb_sg,
            connection=ec2.Port.tcp(FRONTEND_HTTP_PORT),
            description="Frontend ALB to Chainlit tasks",
        )

        # Frontend tasks reach the agent ALB on the listener port.
        self.agent_alb_sg.add_ingress_rule(
            peer=self.frontend_task_sg,
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="Frontend tasks to agent ALB",
        )

        # Agent ALB-to-task is on the container port (8080).
        self.agent_task_sg.add_ingress_rule(
            peer=self.agent_alb_sg,
            connection=ec2.Port.tcp(AGENT_HTTP_PORT),
            description="Agent ALB to agent tasks",
        )

        # Dev: VPC-internal sources reach the agent ALB. Mirrors the
        # Phoenix ALB rule — covers the SSM port-forward path through
        # ECS EC2 hosts (used by the eval runner and ad-hoc curl
        # against /v1/chat). Agent ALB is internal-only (private
        # subnets, internet_facing=False); the VPC is the network
        # boundary, and X-Service-Auth is the app-layer authn that
        # protects /v1/chat regardless of who can open a TCP socket.
        self.agent_alb_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="Dev: VPC-internal sources reach agent ALB (SSM port-forward via ECS EC2 hosts)",
        )

        # Sandbox tasks accept HTTP only from agent tasks (no ALB).
        self.sandbox_task_sg.add_ingress_rule(
            peer=self.agent_task_sg,
            connection=ec2.Port.tcp(SANDBOX_HTTP_PORT),
            description="Agent tasks to sandbox tasks (per-session HTTP)",
        )

        self.rds_sg.add_ingress_rule(
            peer=self.frontend_task_sg,
            connection=ec2.Port.tcp(POSTGRES_PORT),
            description="Frontend tasks to Postgres (Chainlit Data Layer)",
        )

        # Phoenix wiring. The trust path mirrors the agent ALB:
        #   agent_task --[ALB_HTTP_PORT=80]--> phoenix_alb_sg     (OTLP)
        #   frontend_task --[ALB_HTTP_PORT=80]--> phoenix_alb_sg  (UI deep links)
        #   any VPC-internal source --[ALB_HTTP_PORT=80]--> phoenix_alb_sg
        #       (covers SSM port-forward sessions through ECS EC2 hosts,
        #        which run with the ASG's auto-created SG that we don't
        #        otherwise enumerate. ALB is internal-only — the VPC is
        #        already the security boundary.)
        #   phoenix_alb_sg --[PHOENIX_HTTP_PORT=6006]--> phoenix_task_sg
        #   phoenix_task_sg --[POSTGRES_PORT=5432]--> rds_sg
        self.phoenix_alb_sg.add_ingress_rule(
            peer=self.agent_task_sg,
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="Agent tasks send OTLP traces to Phoenix ALB",
        )
        self.phoenix_alb_sg.add_ingress_rule(
            peer=self.frontend_task_sg,
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="Frontend tasks reach Phoenix UI for deep-link rendering",
        )
        self.phoenix_alb_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="Dev: VPC-internal sources reach Phoenix UI (SSM port-forward via ECS EC2 hosts)",
        )
        self.phoenix_task_sg.add_ingress_rule(
            peer=self.phoenix_alb_sg,
            connection=ec2.Port.tcp(PHOENIX_HTTP_PORT),
            description="Phoenix ALB to Phoenix task (HTTP + OTLP on same port)",
        )
        self.rds_sg.add_ingress_rule(
            peer=self.phoenix_task_sg,
            connection=ec2.Port.tcp(POSTGRES_PORT),
            description="Phoenix task to Postgres (Phoenix-owned logical DB)",
        )

        # Gateway wiring. The trust path replaces direct frontend→agent
        # ALB calls (which round-robined) with consistent-hash routing
        # through the gateway:
        #   frontend_task --[ALB_HTTP_PORT=80]--> gateway_alb_sg
        #   gateway_alb_sg --[ALB_HTTP_PORT=80]--> gateway_task_sg
        #   gateway_task_sg --[AGENT_HTTP_PORT=8080]--> agent_task_sg
        #     (gateway resolves agent task IPs via Cloud Map DNS,
        #      bypassing the agent ALB which only does round-robin)
        #   any VPC source --[ALB_HTTP_PORT=80]--> gateway_alb_sg
        #     (SSM port-forward for eval runner, mirrors agent/phoenix)
        # NB: the frontend_task → agent_alb rule above stays in place
        # so direct-to-agent debug paths still work; production traffic
        # now flows through the gateway.
        self.gateway_alb_sg.add_ingress_rule(
            peer=self.frontend_task_sg,
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="Frontend tasks to gateway ALB (production /v1/chat path)",
        )
        self.gateway_alb_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="Dev: VPC-internal sources reach gateway ALB (SSM port-forward via ECS EC2 hosts)",
        )
        self.gateway_task_sg.add_ingress_rule(
            peer=self.gateway_alb_sg,
            connection=ec2.Port.tcp(ALB_HTTP_PORT),
            description="Gateway ALB to gateway (HAProxy) tasks",
        )
        # ALB target health check hits HAProxy's stats listener on
        # 8404 (which serves /healthz). Without this rule the check
        # times out and ECS kills the task in a loop — first deploy
        # symptom is GatewayService/Service stuck in CREATE_IN_PROGRESS
        # with "target ... unhealthy due to (reason Request timed out)"
        # in the service events.
        self.gateway_task_sg.add_ingress_rule(
            peer=self.gateway_alb_sg,
            connection=ec2.Port.tcp(8404),
            description="Gateway ALB to gateway tasks (HAProxy stats + /healthz)",
        )
        self.agent_task_sg.add_ingress_rule(
            peer=self.gateway_task_sg,
            connection=ec2.Port.tcp(AGENT_HTTP_PORT),
            description="Gateway tasks to agent tasks (Cloud-Map-resolved, bypassing agent ALB)",
        )

        # ---- Outputs (visible in CloudFormation console) -------------------
        cdk.CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        cdk.CfnOutput(
            self,
            "VpcCidr",
            value=self.vpc.vpc_cidr_block,
            description="Whitelist this CIDR for any corp-network ingress allowances",
        )
