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

~$80–100/mo with the dev stack fully deployed: NAT gateway $32 + RDS $13 + 2 ALBs $32 + storage/secrets $3, ECS task hours +$15–30. Bedrock + Athena pay-per-use on top. Tear down between demos:

```bash
./scripts/teardown_dev_stack.sh
```

## Heritage

This agent originated in [`langchain-strands-aws-comparison`](https://github.com/glebpnkv/langchain-strands-aws-comparison) as `agents/strands_glue_pipeline_agent/`, then split out here so it can iterate independently and own its own CI.
