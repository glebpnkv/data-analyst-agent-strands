# data-analyst-agent — hackathon baseline

**This branch (`hackathon/baseline-implementation`) is a stripped-down rebuild for AWS Workshop Studio accounts where Glue, Athena, Cognito, Route 53 and CDK-style IAM role creation are all blocked.** It is not intended to merge into `main`. The full-fat ECS / Cognito / Glue version still lives on `main`.

## What's in this build

- The agent runs on **Bedrock AgentCore Runtime** (managed container service).
- The Python sandbox is **AgentCore Code Interpreter** — no self-hosted ECS.
- A **Chainlit** frontend runs **locally on your laptop** and calls the deployed runtime via `bedrock-agentcore.invoke_agent_runtime`. No public URL, no Cognito, no Route 53.
- Data lives in **three S3 buckets** (raw / processed / gold). Everything is CSV — no Glue catalog, no Athena.
- The agent can **deploy AWS Lambda pipelines** authored from chat. Because the runtime role's permissions boundary forbids `iam:*` and most `lambda:*`, the agent stages a pipeline spec in S3 and the **Chainlit frontend completes the deploy** using the more permissive credentials on your laptop.
- Plots stream back to the chat via the same S3-rendezvous trick (PNGs and interactive Plotly charts).

## Prerequisites

- An AWS Workshop Studio account (Bedrock model access enabled — including a Sonnet inference profile in `us-east-1`).
- A local AWS CLI profile pointing at it. The setup below assumes `AWS_PROFILE=hackathon`.
- `uv`, Docker Desktop (with `buildx`), and `python ≥ 3.12`.

## First-time setup

```bash
# 1. Install deps
uv sync

# 2. Create the three S3 buckets, ECR repo, and the agent's IAM role
AWS_PROFILE=hackathon uv run python scripts/hackathon_bootstrap.py

# 3. Seed the gold bucket with two demo datasets
AWS_PROFILE=hackathon uv run python scripts/hackathon_seed_gold.py

# 4. Build + push the agent image, then create the AgentCore Runtime
AWS_PROFILE=hackathon uv run python scripts/hackathon_deploy.py
```

The bootstrap script's last line is a block of `export ...` — copy those into your shell, you'll need them in step 5.

The deploy script prints the runtime ARN at the end. Save it as `AGENT_RUNTIME_ARN`. Status is `CREATING` for 1–3 minutes; check with:

```bash
AWS_PROFILE=hackathon aws bedrock-agentcore-control get-agent-runtime \
  --region us-east-1 \
  --agent-runtime-id <runtime-id-from-the-arn> \
  --query status --output text
```

## Running the chat UI

```bash
AWS_PROFILE=hackathon \
AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/data_analyst_agent-XXXXX \
BUCKET_RAW=hackathon-da-raw-<acct>-us-east-1 \
BUCKET_PROCESSED=hackathon-da-processed-<acct>-us-east-1 \
BUCKET_GOLD=hackathon-da-gold-<acct>-us-east-1 \
uv run chainlit run frontend/hackathon_app.py -w
```

Opens at <http://localhost:8000>. Use the paper-clip to attach a CSV/Excel — it's pushed to the raw bucket and the agent is told the S3 key.

## Iteration loop

| You changed… | Do this |
|---|---|
| `frontend/hackathon_app.py` | Save — Chainlit's `-w` reloads automatically. |
| `agent/agent.py` (or anything in the container) | `uv run python scripts/hackathon_deploy.py`, wait for runtime status `READY`. |
| `pyproject.toml` (laptop deps only — e.g. plotly pin) | `uv sync`, restart Chainlit. |
| `scripts/hackathon_bootstrap.py` (IAM, buckets) | Re-run the bootstrap script — it's idempotent and re-applies the inline policy. |

## What's blocked and what we worked around

The Workshop Studio account's `WSParticipantRole` has these blocks. Spelled out so a colleague can sanity-check before debugging:

- `glue:CreateDatabase` / write — agent doesn't use Glue. CSV-only via S3.
- `athena:StartQueryExecution` — same; in-sandbox pandas instead of Athena.
- `route53:*`, ACM cert validation — no custom domain. Chainlit on `localhost`.
- `iam:CreateRole` for arbitrary names — only `aiagent-*` / `mcp-*` / `backoffice-*` with the `workshop-boundary` permissions boundary. We name everything `aiagent-*`.

The `workshop-boundary` itself caps the runtime role's effective permissions further:

- Denies `iam:*` outright, allows only `lambda:InvokeFunction` (no `CreateFunction`/`ListFunctions`/etc).
- So the agent's `deploy_pipeline_as_lambda` and `list_pipelines` tools route through S3 and let the Chainlit host (running as `WSParticipantRole`, which DOES have those perms) do the actual create/update.

The pandas Lambda layer (`AWSSDKPandas-Python312`) is also blocked from cross-account access on this account, so deployed Lambdas have only stdlib + boto3. Use the `csv` module for pipeline logic; pandas work happens in the AgentCore Code Interpreter sandbox where it's pre-installed.

## Repo layout

```
agent/
  agent.py                  — AgentCore Runtime entrypoint + tools
  Dockerfile.agentcore      — container image for the runtime
  skills/
    build-pipeline/         — bronze/silver/gold pipeline workflow skill
    sandbox-artifacts/      — display-tool conventions (paths, Plotly tables)
frontend/
  hackathon_app.py          — Chainlit UI + host-side Lambda deployer + image renderer
  chainlit_schema_sqlite.sql — schema for the local conversation-history DB
public/
  sidebar.js, stylesheet.css — right-hand "Datasets" sidebar
scripts/
  hackathon_bootstrap.py    — buckets, ECR repo, IAM role
  hackathon_seed_gold.py    — two demo CSVs into the gold bucket
  hackathon_deploy.py       — build, push, CreateAgentRuntime / UpdateAgentRuntime
.chainlit/config.toml       — Chainlit config (sidebar custom_js wired here)
```

That's the whole live tree. (Earlier commits on this branch carried a lot of `main`-branch dead code — `agent_server/`, `infra/`, `sandbox/`, `agent/server/`, etc. — all removed in the submission tidy-up.)

## Demo flow

1. *"What datasets do you have?"* → agent calls `list_s3_dataset("gold")`. Sidebar also lists CSVs across all three buckets.
2. *"Do EDA on the monthly sales data."* → loads, runs pandas, shows summary stats and a Plotly chart.
3. Drop a CSV via the paper-clip → agent picks it up from `raw/uploads/<id>/<filename>`.
4. *"Build me a gold pipeline for this CSV."* → agent loads the `build-pipeline` skill, prototypes the transform in the sandbox (stdlib + boto3 only), queues a spec to `processed/_pipelines/pending/`. Frontend posts *"✅ Deployed `aiagent-lambda-gold-...`"* within seconds, then auto-prompts the agent to invoke + sample the output. No second user message needed.

## Logs

| Where | Command |
|---|---|
| Chainlit (local) | The terminal you ran `chainlit run` in. |
| Agent (AgentCore Runtime) | `aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT --region us-east-1 --since 30m --follow` |
| Deployed Lambda pipelines | `aws logs tail /aws/lambda/<function-name> --region us-east-1 --since 30m --follow` (log group is auto-created on first invoke) |

The AgentCore Code Interpreter sandbox itself is opaque — its internals aren't surfaced in CloudWatch. The agent's logs in (2) are the closest you get.
