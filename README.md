# data-analyst-agent-strands

A [Strands](https://strandsagents.com/)-based data analyst agent that lives on AWS:

- Reads from Athena over a Glue Data Catalog you point it at.
- Authors and deploys [AWS Glue Python shell jobs](https://docs.aws.amazon.com/glue/latest/dg/add-job-python.html) into a target repo via PRs.
- Streams answers (text, tables, plots) into a [Chainlit](https://docs.chainlit.io/) chat UI fronted by an ALB + Cognito SSO.
- Runs ad-hoc Python (pandas / matplotlib / plotly) inside an isolated AgentCore Code Interpreter sandbox.

The behavioural details — system prompt, skills, tool design, target Glue repo layout — live in [`agent/README.md`](agent/README.md).

## Repo layout

```
agent/             — the agent package: prompts, tools, hooks, MCP wiring,
                     server entrypoint, target Glue-repo template, skills
agent_server/      — shared FastAPI scaffold (sessions, streaming, display
                     tools) the agent's `server/main.py` plugs into
frontend/          — Chainlit chat UI; talks to the agent over SSE
infra/             — AWS CDK app: Network / Data / Ecr / Auth / Compute stacks
scripts/           — bootstrap, deploy, teardown, local-stack, sample data
tests/             — pytest suite for agent tools
```

## Getting started

For the full deploy walkthrough (Route 53 hosted zone, Cognito SSO, first deploy via `bootstrap.sh`, Glue prerequisites, target-repo bootstrap, GitHub PAT, etc.), see [`agent/README.md`](agent/README.md).

Short version, once AWS account is bootstrapped and the hosted zone is in place:

```bash
aws sso login
./scripts/bootstrap.sh                    # first-time deploy
aws secretsmanager put-secret-value \     # one-time GitHub PAT
  --secret-id DataAnalystAgent/Dev/GithubPat \
  --secret-string "<pat>"

# Roll the agent service to pick up the new secret
CLUSTER=$(aws ssm get-parameter --region eu-central-1 \
  --name /data-analyst-agent/dev/cluster-name \
  --query Parameter.Value --output text)
AGENT_SVC=$(aws ssm get-parameter --region eu-central-1 \
  --name /data-analyst-agent/dev/agent/service-name \
  --query Parameter.Value --output text)

aws ecs update-service \
  --region eu-central-1 \
  --cluster "$CLUSTER" \
  --service "$AGENT_SVC" \
  --force-new-deployment >/dev/null

aws ecs wait services-stable \
  --region eu-central-1 \
  --cluster "$CLUSTER" \
  --services "$AGENT_SVC"
echo "agent rolled"

# Add yourself as a Cognito user (admin-invite by email)
USER_POOL_ID=$(aws ssm get-parameter --region eu-central-1 \
  --name /data-analyst-agent/dev/cognito/user-pool-id \
  --query Parameter.Value --output text)
aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username <YOUR_EMAIL_ADDRESS> \
  --user-attributes Name=email,Value=<YOUR_EMAIL_ADDRESS> Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

Visit `https://<your-domain>` to chat with the deployed agent.

For local dev:

```bash
./scripts/run_local_stack.sh   # Phoenix + Postgres + agent + Chainlit
```

## Cost ballpark (idle)

~$100–125/mo with the dev stack fully deployed: NAT gateway $32 + RDS $13 + 3 ALBs $48 (frontend + agent + gateway) + storage/secrets $3, ECS task hours +$15–30. Bedrock + Athena pay-per-use on top. Tear down between demos:

```bash
./scripts/teardown_dev_stack.sh
```

## Technical details

Two pieces of this stack are easy to under-design and worth explaining in detail because the trade-offs apply to plenty of other stateful-LLM-agent setups too.

### Per-session code-execution sandboxes

**Problem.** The agent runs LLM-authored Python (pandas, plotly, matplotlib) against data the user is currently analysing. That code can: read & write arbitrary files, hold a kernel's worth of state across turns, install packages, leak credentials, and consume non-trivial memory. We need this to be isolated *per chat session*, fast to claim on first use, cheap to release on close, and tightly scoped at the IAM layer.

**Design.** Each session gets its own ECS task running a kernel server (`sandbox/Dockerfile`). The agent talks to it over HTTP on a private VPC IP — no ALB in front because there's exactly one client per task (the agent that claimed it). Lifecycle:

1. **Warm pool.** `SandboxPool` (in `agent_server/`) keeps a small set of pre-warmed sandbox tasks idle, registered by ARN. Pool size is a CDK context flag (`sandbox_pool_size`, default 1) sized against ASG capacity.
2. **Claim.** On first turn of a session, the agent atomically claims a sandbox from the pool and refills asynchronously via `ecs:RunTask`. Cold-claim path runs only when the pool is empty.
3. **Release.** Session close (idle TTL or explicit `DELETE /v1/sessions/{id}`) stops the task. The next pool refill picks up the freed slot.
4. **IAM.** Sandbox task role has *zero* access to the rest of the account — no S3, no Bedrock, no Glue. The agent task role has `ecs:RunTask`, `ecs:StopTask`, `ecs:DescribeTasks` scoped to the sandbox task definition family ARN. SG ingress only allows the agent task SG.

**Why this shape vs. alternatives.**

- **Lambda per execution** — cold-start per turn (1–3s), 15-min ceiling, no kernel state across turns, payload-size hurts large dataframes. Worse fit for an analyst use case where the user runs `df.head()` and then `df.describe()` and expects the kernel to remember `df`.
- **Single shared sandbox process** — fast, but cross-session state leaks turn into outright correctness bugs (one user's `df` shadowing another's).
- **AgentCore Code Interpreter** — Bedrock-managed. Used to be the canonical answer. Issue: at scale-out time, AgentCore's billing model and tenant isolation guarantees aren't a fit for everyone; rolling our own keeps the cost predictable and the boundary explicit.

**Known limit.** Cold-claim when the pool is empty takes 20–40s while ECS pulls the image and starts the task. The pool exists to make this an outlier rather than the common case. Bump `sandbox_pool_size` to N if N concurrent fresh sessions need to feel instant.

### Session-affinity gateway (HAProxy)

**Problem.** The agent service is stateful per session. Once a session lands on task A, subsequent turns *must* keep landing on task A — otherwise the new task has no in-memory record of the session and starts a fresh one (cold sandbox claim, empty conversation, MCP boot). With the default ALB round-robin, a 2-task deploy loses session state ~50% of turns, and within 5 turns ~97% of sessions are broken.

**Design.**

```
chainlit / eval-runner
        │ POST /v1/chat   (X-Session-Id header)
        ▼
   gateway ALB (internal)
        │
        ▼
   HAProxy tasks (ECS, gateway/Dockerfile)
        │  consistent hash on X-Session-Id
        │  backend pool resolved from Cloud Map DNS
        ▼
   agent tasks (registered at agent.dataanalyst.local)
        │
        ▼  per-session HTTP to claimed sandbox task IP
   sandbox tasks
```

- `gateway/haproxy.cfg` does `balance hdr(X-Session-Id)` with `hash-type consistent`. ~30 lines, no exotic features.
- The agent ECS service registers each task IP under `agent.dataanalyst.local` via AWS Cloud Map. HAProxy resolves the name on a 10s TTL and reshapes the backend ring as tasks come and go.
- HAProxy active-health-checks each backend every 5s, ejects within ~15s of failure.
- The agent ALB still exists — it's used by ECS for deploy gating and by SSM port-forward for ad-hoc debug — but production `/v1/chat` traffic flows through the gateway, not the ALB.

**Failure behaviour.**

- **Healthy steady state:** sessions stick to their assigned task 100%.
- **Task disappears (deploy, crash, scale-in):** HAProxy ejects within ~15s. Sessions on the dead task get re-hashed to a surviving task. *Those sessions start fresh on the new task* — in-memory conversation + claimed sandbox + MCP state are lost. Chat history (persisted in Chainlit's Postgres data layer) is preserved, so the user-visible thread isn't broken.
- **Task appears (deploy, scale-up):** HAProxy adds to the ring. Consistent hashing minimises reshuffling — only ~1/N sessions move on each add or remove, instead of all of them.
- **Gateway task dies:** ALB ejects it; remaining gateway tasks continue serving.

**Why this shape vs. alternatives.**

- **ALB stickiness cookies** — ALB sets its own cookie and routes by it. Non-browser clients (eval runner, server-to-server) don't persist cookies, and stickiness is per LB-target, not per-application-session-id. Wrong tool.
- **Hash-routing in the client** — Chainlit / eval-runner hash session_id against the live task list themselves. Possible but couples every client to ECS service discovery internals.
- **ECS Service Connect** (Envoy sidecars on every client) — supports consistent hash on header natively. More AWS-managed, more moving parts (sidecar per client task), and the routing-policy config is per-cluster rather than per-service. Reasonable choice if you're already running App Mesh / Service Connect across the cluster.
- **Custom FastAPI / Go proxy** — flexible, but every operational quirk is now ours to debug. HAProxy gives this away for free.
- **Shared session state in Redis / Postgres** — eliminates the affinity requirement entirely. Right answer for *zero-loss-on-task-death* requirements, but means re-architecting how the agent (and Strands' MCP subprocess lifecycle) carries state. Multi-day piece of work; out of scope here. The gateway pattern gets us ~95% of the value at ~5% of the cost.

**Cost.** One extra internal ALB (~$22/mo) and a small HAProxy task (128 CPU units, 256 MiB — negligible). No per-request cost.

## Heritage

This agent originated in [`langchain-strands-aws-comparison`](https://github.com/glebpnkv/langchain-strands-aws-comparison) as `agents/strands_glue_pipeline_agent/`, then split out here so it can iterate independently and own its own CI.
