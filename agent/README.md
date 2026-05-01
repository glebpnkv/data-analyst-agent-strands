# Strands Glue Pipeline Agent

A Strands-based agent that builds, tests, and delivers AWS Glue Python Shell jobs. It iterates on jobs in a dev AWS account, commits the production version to a pre-specified GitHub repo, and opens a PR once the wheel-based production run succeeds.

The agent runs as a FastAPI service behind a Chainlit chat UI, with Phoenix for LLM tracing. Local development uses `docker compose`-style containers; the deployed version uses ECS Fargate provisioned by CDK Python.

## Requirements

**This agent requires a GitHub fine-grained Personal Access Token.** Startup will fail or the first GitHub tool call will error without one. See [Setup](#setup) below.

Other prerequisites:
- AWS credentials for a dev account (profile configured in `~/.aws/config` or via env vars).
- Python 3.13+ and `uv` to install dependencies.
- Outbound HTTPS to `api.githubcopilot.com` (the agent uses GitHub's hosted MCP server; no local install).
- Access to Bedrock in the configured region for the chosen model.
- For deployment: Docker Desktop, the AWS CDK CLI (`npm install -g aws-cdk`), and the Session Manager plugin (`brew install --cask session-manager-plugin` on macOS).

## How it works (end-to-end flow)

1. **Input source selection** — the agent chooses between the Athena flow and the raw-S3 flow based on what the user provides (or on `ATHENA_DATABASE` / `RAW_DATA_BUCKET_S3_URI`).
2. **Phase A — scratch iteration and commit**
   - **A.1** The agent writes a loose `.py` script, uploads it to S3, creates a Glue job named `scratch-<conversation-id>-*`, and iterates in the dev AWS account until the run succeeds. The agent creates these scratch jobs directly through MCP.
   - **A.2** On success, the agent lays the code out per the `project-structure` skill, adds or updates the job's entry in `glue-jobs.yaml`, creates a feature branch in the target GitHub repo, and pushes the files.
   - **A.3** The agent **ends the turn** and asks the user to resume once GitHub Actions (`test` → `build-wheels` → `deploy`) has gone green.
3. **Phase B — production verification and PR** (on resume)
   - **B.1** The agent verifies CI is green via the GitHub MCP workflow-run tools. If CI failed, it surfaces the failing job logs and stops.
   - **B.2** The agent looks up the Glue job created by CI (not by the agent) and runs `start-job-run` to verify it works with the deployed wheel. If it fails, the agent surfaces the diagnostics and stops — no PR.
   - **B.3** Only after the verification run succeeds, the agent opens a PR against the default branch. Turn ends. The agent does not watch for merge.

The division of responsibilities is deliberate: **the agent creates Glue jobs only in Phase A.1 (scratch)**. In Phase B, `deploy/deploy.py` in the target repo creates them via boto3 from `glue-jobs.yaml`.

## Setup

### 1. Set up a target code repo for the agent

The agent commits generated job code into a separate GitHub repo that you control — not into this repo. The directory [`target_repo_template/`](./target_repo_template/) in this project is a **reference template**: it holds the canonical layout (`jobs/`, `glue-jobs.yaml`, `deploy/deploy.py`, the `build-and-deploy.yml` CI pipeline) that the agent's `project-structure` skill expects. Treat it as a starter scaffold to copy into a fresh repo, not as something the agent commits to directly.

To bootstrap a target repo:

1. Create an empty GitHub repo (e.g. `your-org/glue-jobs`). This is the repo the agent will push branches and PRs to.
2. Clone it locally, then copy the template scaffold in:

   ```bash
   # From within an empty clone of your target repo:
   rsync -a --exclude='.git' /path/to/data-analyst-agent-strands/agent/target_repo_template/ ./
   git add .
   git commit -m "Bootstrap repo layout"
   git push
   ```

3. Configure the CI secrets listed in `target_repo_template/README.md` (AWS creds, asset bucket, Glue role ARN). Then push a throwaway branch to confirm the `build-and-deploy.yml` pipeline (`test` → `build-wheels` → `deploy`) goes green end-to-end before running the agent against the repo. A green run on an empty manifest is expected to upload no wheels — that's fine; the first real job push will produce them.

Once that repo exists and CI is green, continue with the PAT in step 2.

### 2. Create a GitHub fine-grained PAT

Fine-grained PATs cannot be created via CLI — GitHub only allows creation through the web UI. Follow these steps exactly:

1. Go to <https://github.com/settings/personal-access-tokens/new>.
2. **Token name**: anything memorable (e.g. `strands-glue-pipeline-agent-local`).
3. **Resource owner**: the owner of the target repo from step 1. If the repo is in an organisation, select that org — the org must allow fine-grained PATs (Org Settings → Third-party Access → Personal access tokens).
4. **Expiration**: up to 1 year. Set a calendar reminder to rotate.
5. **Repository access**: choose **Only select repositories** and pick the single target repo from step 1.
6. **Repository permissions** — set exactly these, leave everything else at `No access`:
   - **Contents**: `Read and write`
   - **Metadata**: `Read-only` (required, auto-enabled)
   - **Pull requests**: `Read and write`
7. **Account permissions**: leave all at `No access`.
8. Click **Generate token**. Copy the token immediately — it is shown only once.

### 3. Configure `.env`

Copy the template and fill it in:

```bash
cp agent/.env.template agent/.env
```

All values in `.env.template` are documented inline. Key variables:

| Variable | Required | Purpose |
|---|---|---|
| `AWS_PROFILE` / `AWS_REGION` | Yes | Dev AWS account credentials. |
| `MODEL_ID` | Yes | Bedrock model ID. |
| `GLUE_JOB_ROLE_ARN` | Yes | IAM role Glue assumes. |
| `GITHUB_PAT` | Yes | The fine-grained PAT from step 2. |
| `TARGET_REPO_OWNER` / `TARGET_REPO_NAME` | Yes | Owner and repo name from step 1. |
| `TARGET_REPO_DEFAULT_BRANCH` | No | Default `main`. |
| `ATHENA_DATABASE` / `ATHENA_TABLE` | Optional | Default Athena input; user can override per conversation. |
| `RAW_DATA_BUCKET_S3_URI` | Optional | Default raw-S3 input; enables the raw-S3 flow when no Athena source is given. |

### 4. Install dependencies

```bash
# from repo root
uv sync
```

The GitHub MCP server is GitHub's hosted service at `https://api.githubcopilot.com/mcp/` — no local install. The agent authenticates with the fine-grained PAT from `.env`.

### 5. Set up AWS Glue prerequisites

See `setup_glue_job_prereqs.sh` for IAM roles, S3 staging locations, and Bedrock access. Run it once per dev account.

## Running locally

### Architecture

```
Browser ──▶ Chainlit  ──HTTP+SSE──▶ FastAPI agent service  ──▶ Bedrock / Athena / Glue / sandbox
   :8000                              :8080                       (your AWS account)
```

- `frontend/` — Chainlit chat UI. Consumes the `v1` SSE protocol (`text.delta`, `tool.start/end`, `ui.dataframe`, `ui.plotly`, `ui.image`, `done`, `error`) and renders tool steps, tables (`cl.Dataframe`), Plotly charts (`cl.Plotly`), and images (`cl.Image`) inline in the chat.
- `agent_server/` — shared FastAPI scaffold: routing, auth middleware, SSE plumbing, in-memory session registry, Strands→SSE event reducer, and the `display_dataframe` / `display_plotly` / `display_image` tool factories. Adding a new agent is a ~30-line `server/main.py` that supplies an agent factory and reuses everything else.
- `agent/server/main.py` — the agent's thin wrapper around `agent_server`. Wires up sandbox text and image loaders so display tools resolve sandbox file paths server-side rather than pulling bytes through the LLM context.

### Full stack

One script brings up the FastAPI service, Postgres for thread persistence, the Chainlit UI, and Phoenix for LLM tracing:

```bash
aws sso login
./scripts/run_local_stack.sh    # from the repo root
```

| Surface         | URL                       |
|-----------------|---------------------------|
| Chainlit (chat) | http://127.0.0.1:8000     |
| Phoenix (UI)    | http://127.0.0.1:6006     |
| Agent API       | http://127.0.0.1:8080     |

`Ctrl-C` in the same terminal stops everything cleanly. Phoenix data persists across restarts in a named docker volume; the agent and frontend each write logs to `runs/<service>/<timestamp>/`.

To skip Phoenix or use a hosted backend (Langfuse, Honeycomb, anything OTLP-shaped):

```bash
SKIP_PHOENIX=1 OTEL_EXPORTER_OTLP_ENDPOINT=https://your-collector \
  AGENT_OTLP_ENABLE=1 ./scripts/run_local_stack.sh
```

### Debugging individual services

When a piece is misbehaving and you want to iterate on just it, the local-stack script is overkill. Bring up only what you need:

```bash
./scripts/run_agent_local.sh                              # agent FastAPI
./scripts/run_frontend_local.sh                            # Chainlit
```

These omit Phoenix entirely — set `OTEL_EXPORTER_OTLP_ENDPOINT` and `AGENT_OTLP_ENABLE=1` yourself if you want OTLP export.

### Observability

Every boot of the agent service writes to a fresh per-process directory:

```
runs/<service-name>/<YYYYmmddTHHMMSS>/
├── server.log              # all logs at AGENT_LOG_LEVEL (default INFO)
└── strands_traces.jsonl    # Strands' OpenTelemetry spans, one JSON per line
```

When `AGENT_OTLP_ENABLE=1` (the local-stack script sets this), spans are also pushed to `OTEL_EXPORTER_OTLP_ENDPOINT` over OTLP HTTP/protobuf — Phoenix by default. The `openinference-instrumentation-bedrock` package emits OpenInference-conformant LLM child spans nested under Strands' agent-level parents, so Phoenix's UI shows model name, prompt / completion / total token counts, and computed cost per call.

**Phoenix persistence.** Phoenix stores its SQLite DB inside the named docker volume `phoenix-agent-traces-data`, so traces persist across restarts of the local stack. To wipe history:

```bash
docker volume rm phoenix-agent-traces-data
```

### One-shot CLI (no UI, no service)

`main.py` is still here for ad-hoc CLI runs:

```bash
cd agent
uv run python main.py --prompt "Build a Glue job that averages daily orders from myschema.orders and writes to s3://my-outputs/daily-avg/"
```

### Useful env knobs

| Variable                       | Default                  | Purpose |
|--------------------------------|--------------------------|---------|
| `AGENT_LOG_LEVEL`              | `INFO`                   | Set to `DEBUG` to surface httpx/botocore noise |
| `AGENT_RUN_DIR`                | `runs`                   | Base dir for per-process subdirs |
| `AGENT_OTLP_ENABLE`            | unset                    | `1` to enable OTLP export |
| `OTEL_EXPORTER_OTLP_ENDPOINT`  | unset                    | OTLP HTTP endpoint, e.g. `http://127.0.0.1:6006` |
| `AGENT_SERVICE_AUTH_SECRET`    | unset                    | Shared secret between Chainlit and the agent. Unset = unauthenticated local dev. |
| `PHOENIX_PORT`                 | `6006`                   | Phoenix UI + OTLP receiver |
| `AGENT_PORT` / `FRONTEND_PORT` | `8080` / `8000`          | If you need to dodge a port collision |

## Skills

The agent auto-loads every skill under `skills/`:

- **`athena-query-execution`** — SQL-only pipelines run via Athena.
- **`glue-python-shell-job`** — two-phase (scratch via MCP + commit-driven deploy) Glue Python Shell job flow.
- **`s3-raw-data-ingestion`** — discovery flow for raw data in S3 that isn't catalogued in Athena.
- **`sandbox-artifacts`** — convention for surfacing analysis outputs (charts, tables, images) inline in the chat without flooding the model context.
- **`project-structure`** — directory layout, package naming, and entrypoint conventions for committed code. Points at the canonical scaffold in `target_repo_template/`.

## Deploying to AWS

Infrastructure lives under [`../../infra/`](../../infra/) (CDK Python). First-time bootstrap is [`../../scripts/bootstrap.sh`](../../scripts/bootstrap.sh); subsequent application image rollouts go through [`../../scripts/deploy.sh`](../../scripts/deploy.sh). Teardown is a single script.

### Architecture (deployed)

```
Browser ─HTTPS─▶ Public ALB (Cognito auth) ─▶ Chainlit ECS service ─▶ RDS Postgres
                                                       │
                                                       │  HTTP+SSE (X-Service-Auth)
                                                       ▼
                                          Internal ALB ─▶ Agent ECS service ─▶ Bedrock / Athena / Glue / sandbox
```

Five CDK stacks, all named `DataAnalystAgent-<Layer>-Dev`:

- **Network** — VPC (10.0.0.0/16), 2 AZs, 1 NAT gateway, 5 security groups.
- **Data** — RDS Postgres `db.t4g.micro`, single-AZ, 20 GB GP3, master credentials in Secrets Manager.
- **Ecr** — two ECR repos (`data-analyst-agent/agent`, `data-analyst-agent/frontend`) with 10-image lifecycle policy.
- **Auth** — Cognito User Pool + User Pool Client + hosted-UI domain. Self-signup off; admins invite users via `admin-create-user`.
- **Compute** — Fargate cluster, agent + frontend task definitions / services, the public HTTPS frontend ALB (Cognito-fronted) + internal HTTP agent ALB, ACM cert, Route 53 alias record, three SM secrets, IAM roles.

Cost ballpark when fully deployed (idle): ~$80–100/mo.
- NAT gateway $32 + RDS $13 + 2 ALBs $32 + storage/secrets $3.
- ECS task hours roughly +$15–30/mo with desiredCount=1.
- Bedrock and Athena pay-per-use on top.

### One-time setup per AWS account + region

```bash
cd infra
cdk bootstrap aws://<account-id>/<region>
```

(account-id from `aws sts get-caller-identity --query Account --output text`). Creates a small set of supporting resources CDK needs (S3 asset bucket, IAM deploy role, SSM version param). ~$0/mo idle.

### First deploy — full sequence

The first deploy uses `bootstrap.sh`, which sequences the chicken-and-egg between ECR (must exist before we can push images) and the Compute stack (creates ECS services that pull `:latest` and block CFN until the service stabilizes — which can't happen against an empty ECR).

```bash
aws sso login

# 1. One command. Internally: cdk deploy Network/Data/Ecr/Auth ->
#    docker build + push agent + frontend images -> cdk deploy
#    Compute. Takes ~20-40 min on a clean account, dominated by RDS
#    provisioning and ACM cert validation.
./scripts/bootstrap.sh

# 2. Populate the GitHub PAT secret (only needed once; re-run only on rotation).
aws secretsmanager put-secret-value \
  --secret-id DataAnalystAgent/Dev/GithubPat \
  --secret-string "<your-fine-grained-pat>"

# 3. Add yourself (and any other stakeholders) as a Cognito user.
#    See "Setting up a domain and SSO" below for invite/disable/reset.
```

If `bootstrap.sh` fails partway through, just re-run it — each phase is idempotent (CDK stack ops are no-ops when nothing changed; image build+push is cheap with Docker layer cache).

### Subsequent deploys

Re-run the script targeting just what you changed:

```bash
./scripts/deploy.sh agent       # only the agent FastAPI service
./scripts/deploy.sh frontend    # only the Chainlit frontend
./scripts/deploy.sh all         # both
```

Each invocation rolls the service over to the new image with `--force-new-deployment` and waits for ECS to report it stable. Tag includes a `-dirty` suffix when the working tree has uncommitted changes, so a running task's tag is always traceable.

### Reaching the deployed UI

After `cdk deploy --all` completes and the SSO setup (see below) has issued the cert + DNS, the chat lives at `https://<domain_name>`. Just visit the URL — Cognito's hosted login page intercepts unauthenticated visits, redirects authenticated ones back to Chainlit.

For the **agent service** (internal ALB, no Cognito), if you ever need to hit `/healthz` or `/v1/chat` directly from your laptop for debugging, the SSM port-forward pattern still works:

```bash
CLUSTER=$(aws ssm get-parameter --name /data-analyst-agent/dev/cluster-name --query Parameter.Value --output text)
AGENT_SVC=$(aws ssm get-parameter --name /data-analyst-agent/dev/agent/service-name --query Parameter.Value --output text)
TASK_ARN=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$AGENT_SVC" --query 'taskArns[0]' --output text)
TASK_ID=$(echo "$TASK_ARN" | awk -F'/' '{print $NF}')
RUNTIME_ID=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" --query 'tasks[0].containers[0].runtimeId' --output text)

aws ssm start-session \
  --target "ecs:${CLUSTER}_${TASK_ID}_${RUNTIME_ID}" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8080"],"localPortNumber":["8080"]}'
```

Then `curl http://localhost:8080/healthz`.

### Tearing down

The deployed dev stack costs roughly $80–100/mo idle. When you're not actively demoing, tear it all down:

```bash
./scripts/teardown_dev_stack.sh
```

Lists the live `DataAnalystAgent-*-Dev` stacks, asks for `yes`, runs `cdk destroy --all --force`, and verifies nothing remains. RDS data and ECR images go with the stacks — fine for dev, not for prod. Re-deploying after a teardown is the same as a clean first deploy: `./scripts/bootstrap.sh`.

### Setting up a domain and SSO (Cognito + ALB)

Cognito + ALB authentication requires HTTPS, which requires a domain you control. The CDK stacks expect a Route 53 **public hosted zone** for that domain (or a delegated subdomain) to already exist; once it does, everything else is provisioned automatically by `cdk deploy`.

#### Provisioning the hosted zone

##### Option A: buy a fresh domain through Route 53

Cheapest end-to-end if you have no domain today. ~$13–30 for the first year depending on TLD.

1. AWS console → **Route 53** → **Registered domains** → **Register domain**.
2. Search for an available name (`.com` is ~$13/year).
3. Fill in WHOIS contact info; toggle **Privacy protection** (free).
4. Pay. Registration completes in 5–10 minutes for most TLDs.
5. Route 53 auto-creates a public hosted zone for the domain.
6. Note the hosted zone ID (Route 53 → Hosted zones → click zone → `Z…` value at the top).

##### Option B: delegate a subdomain from an existing registrar

If you already own a domain elsewhere (Squarespace, GoDaddy, Cloudflare, etc.) and only want to use a subdomain (e.g. `dataagent.<yourdomain>`) for the demo, delegate just that subdomain. Email and the apex stay where they are.

1. AWS console → **Route 53** → **Hosted zones** → **Create hosted zone**.
2. Domain name: your chosen subdomain (e.g. `dataagent.example.com`). Type: **Public hosted zone**.
3. Open the new zone. Note the four `awsdns-*` nameserver values.
4. At your existing registrar, find the DNS records UI for the parent domain. Add four NS records:
   - **Type**: `NS`
   - **Name** (or "Host"): just the subdomain prefix (e.g. `dataagent`), not the full FQDN
   - **Nameserver** (or "Data"): one `ns-XXXX.awsdns-XX.<tld>` value per record
   - **TTL**: default
5. Verify with `dig +short NS dataagent.example.com` — should return the four `awsdns-*` values within 30–60 min.
6. Note the hosted zone ID (`Z…`).

#### Pin the hosted zone in `cdk.json`

Open [`infra/cdk.json`](../../infra/cdk.json) and set:

```jsonc
{
  "context": {
    ...
    "hosted_zone_id": "Z01234567ABCDEFGH",
    "domain_name": "dataagent.example.com"
  }
}
```

`domain_name` is the FQDN the chat UI lives at — typically the apex of your delegated zone, but can be a subdomain inside it (e.g. `chat.dataagent.example.com`) if you want.

#### What `cdk deploy` does for SSO

With those two values pinned, the next `cdk deploy --all` provisions:

- **ACM certificate** for `domain_name`, DNS-validated against the hosted zone (CDK adds the validation records automatically; first deploy waits 5–30 min for ACM to issue).
- **Cognito User Pool** (`DataAnalystAgent-Auth-Dev`) with self-signup off, email username, email verification required, no MFA. Default Cognito sender for invitation emails.
- **Cognito User Pool Client** registered with the ALB callback URL `https://<domain_name>/oauth2/idpresponse`.
- **Cognito hosted UI domain** at `dataanalystagent-dev-<account-id>.auth.<region>.amazoncognito.com`.
- **Frontend ALB switched to internet-facing**, in public subnets. HTTPS listener on 443 with `authenticate-cognito` action, plain HTTP listener on 80 that 301-redirects to HTTPS.
- **Route 53 A-record (alias)** pointing `domain_name` at the ALB.
- **Frontend container env** gets `DEPLOYED_BEHIND_ALB=1`, which switches Chainlit's auth from `password_auth_callback` (admin/admin, local) to `header_auth_callback` (Cognito JWT in `x-amzn-oidc-data`, deployed).

After deploy, the chat UI lives at `https://<domain_name>`. Anyone hitting it gets bounced to the Cognito hosted login page; only authenticated users reach Chainlit.

#### Adding and removing demo users

```bash
# Look up the pool ID once
POOL_ID=$(aws ssm get-parameter --name /data-analyst-agent/dev/cognito/user-pool-id \
  --query Parameter.Value --output text)

# Invite a stakeholder — they get an email with a temporary password.
aws cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" \
  --username alice@example.com \
  --user-attributes Name=email,Value=alice@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

What happens on the user's side:

1. Cognito emails them a temporary password from `no-reply@verificationemail.com`. Tell them to expect it; the sender looks suspicious to a fresh inbox and may land in spam.
2. They visit `https://<domain_name>`, get redirected to the Cognito hosted login page.
3. They sign in with email + temp password.
4. Cognito forces a password change on first login. They set a permanent password subject to the password policy (12 chars, mixed case, digits).
5. Cognito redirects back to the ALB; the ALB sets a session cookie; Chainlit accepts the request and shows the chat UI.

Other lifecycle commands:

```bash
# Disable a user (reversible — revokes access, keeps the account).
aws cognito-idp admin-disable-user --user-pool-id "$POOL_ID" --username alice@example.com

# Re-enable.
aws cognito-idp admin-enable-user --user-pool-id "$POOL_ID" --username alice@example.com

# Permanently remove.
aws cognito-idp admin-delete-user --user-pool-id "$POOL_ID" --username alice@example.com

# List all users in the pool.
aws cognito-idp list-users --user-pool-id "$POOL_ID" \
  --query 'Users[].{email:Username,status:UserStatus,enabled:Enabled}' --output table

# Force-reset a forgotten password — sends a fresh temporary password.
aws cognito-idp admin-reset-user-password --user-pool-id "$POOL_ID" --username alice@example.com
```

Cognito is free for the first 50,000 monthly active users, so for a stakeholder demo there's no per-user cost. Adds ~$0.50/month for the Route 53 hosted zone (only cost beyond what's already in the deployed stack).

## Known limitations

These are deliberate simplifications, not bugs. They are the next items to address.

### Persistence (Phase 3b)

- **Resumed threads can't continue the conversation.** Refreshing the page or reopening a thread from the sidebar restores the displayed history from Postgres, but the agent service is unaware of the resumed thread — follow-up questions land on a fresh agent session with no prior context. Two pieces still need wiring: (a) `@cl.on_chat_resume` on the frontend to link the resumed thread back to its `agent_session_id`, and (b) the agent service either becoming stateless (frontend ships full message history per request) or persisting `SessionRegistry` state across restarts. Tracked for a future phase; for now treat resumed threads as read-only history.
- **Inline element bytes don't replay on resume.** `cl.Image` / `cl.Plotly` / `cl.Dataframe` content for resumed threads needs an S3-backed `storage_provider` on `SQLAlchemyDataLayer`; without one, resumed threads show the structure (text, tool steps) but not the rendered charts/tables. Comes with the SSO/HTTPS work.

### Agent behaviour

- **The agent does not poll GitHub Actions for the CI pipeline.** After pushing to the feature branch it ends the turn and asks the user to resume once CI is green. Automating this is a drop-in upgrade once the CI pipeline is stable.
- **The agent does not watch for PR merge.** Its turn ends at "PR open + Phase B verification run succeeded." Post-merge verification is a fresh conversation.
- **The CI pipeline in the target repo (`.github/workflows/build-and-deploy.yml`) uses static AWS access keys.** OIDC federation is the intended production upgrade.
- **PySpark jobs are not yet supported.** The agent handles Python Shell jobs only. A dedicated PySpark skill will be added in a separate branch. The reference PySpark scaffold that previously lived at `target_repo_template/jobs/example_pyspark/` has been removed until then.
- **Tool-spam guarding is narrow.** `GlueJobRunPollThrottleHook` throttles repeated `get-job-run` polls across the MCP tool and the `awsapi_call_aws` CLI fallback, but it only covers that one operation. The model has been observed to spam other identical tool calls (e.g. repeated `aws glue get-job-run` or `aws athena get-query-execution` via arbitrary paths) while claiming in its text that it's waiting. The next step is a more general pattern — likely either (a) a generic "identical `(tool_name, canonicalised_args)` within N seconds → sleep" hook as defence-in-depth, or (b) an industry pattern such as token-bucket rate limiters per tool class, AWS SDK-style exponential backoff at the hook layer, or explicit "wait N seconds" synthetic tool calls that the model must make between polls. Needs research before committing to a design.
