# `infra/` — CDK Python infrastructure

Provisions the AWS resources that host the deployed stack:

```
DataAnalystAgent-Network-Dev   (VPC + SGs)
DataAnalystAgent-Data-Dev      (RDS Postgres + secrets)
DataAnalystAgent-Ecr-Dev       (ECR repos for agent + frontend)
DataAnalystAgent-Compute-Dev   (ECS cluster, services, ALBs)
```

Today this is a skeleton — only the network stack exists, and it's
empty. Real resources land in subsequent phases (see [PLAN.md](../PLAN.md)).

## What CDK is, in one paragraph

[AWS CDK](https://aws.amazon.com/cdk/) is "infrastructure as code in a
real programming language." You write Python (or TypeScript/Java/etc),
CDK *synthesises* it into CloudFormation JSON, and the CDK CLI submits
that JSON to CloudFormation which provisions / updates / deletes
resources. The advantages over hand-written CloudFormation: typed
constructs, reusable modules, IDE autocompletion, ordinary control
flow (`if`, `for`) instead of YAML loops.

## One-time setup

You need three things on your laptop:

1. **AWS credentials with admin-ish access**, e.g. via SSO:
   ```bash
   aws sso login
   ```
2. **The CDK CLI** (Node.js-based):
   ```bash
   npm install -g aws-cdk
   cdk --version    # confirms install
   ```
   If you'd rather not install globally, replace `cdk` with `npx aws-cdk`
   in every command below.
3. **`uv sync`** in the repo root — pulls in `aws-cdk-lib` and
   `constructs`, the Python libraries the stacks import.

### Bootstrap the AWS account (one-time per account / region)

CDK needs a small set of supporting resources in your account before it
can deploy anything (an S3 bucket for assets, an IAM role for
deployments, etc). Create them once:

```bash
cd infra
cdk bootstrap aws://<account-id>/<region>
```

The account-id comes from `aws sts get-caller-identity --query Account --output text`;
region defaults to `eu-central-1`.

## Day-to-day commands

All run from `infra/`:

```bash
cd infra

cdk synth                     # generate CloudFormation, no AWS calls (FREE)
cdk diff DataAnalystAgent-Network-Dev    # show what would change
cdk deploy DataAnalystAgent-Network-Dev  # actually provision/update (starts costing money)
cdk destroy DataAnalystAgent-Network-Dev # tear it down
cdk ls                        # list stacks
```

`cdk synth` is the safe preview command — it produces the
CloudFormation under `cdk.out/` without touching AWS. Run it after every
edit to catch errors before deploy. **No AWS costs accrue until you run
`cdk deploy` for the first time.**

## Tearing the whole dev stack down

The deployed dev stack has ongoing costs (NAT gateway ≈ $32/mo, RDS
instance, ALBs, ECS task hours). When you're not actively demoing,
tear it all down:

```bash
./scripts/teardown_dev_stack.sh
```

This lists the live `DataAnalystAgent-*-Dev` CloudFormation stacks, asks for
confirmation, runs `cdk destroy --all --force`, and verifies nothing
remains. RDS data and ECR images go with the stacks — fine for dev,
not for prod.

Re-deploying after a teardown is just `cdk deploy --all` (or, once
Phase 5 lands, `./scripts/deploy_*.sh`) and starts fresh.

## Cost tagging

Every resource the stacks create is tagged with:

| Tag | Value |
|---|---|
| `Project` | `DataAnalystAgent` |
| `Stage` | `Dev` |
| `ManagedBy` | `cdk` |

Set up [Cost Allocation Tags](https://console.aws.amazon.com/billing/home#/preferences/tags)
for `Project` and `Stage` once (it takes 24h to start populating, then
sticks), and you can filter Cost Explorer to "everything this project
costs me" in one click.

## Region / account overrides

Defaults are `eu-central-1` and "whatever account your AWS creds point
at." Override with env vars before running CDK:

```bash
CDK_DEFAULT_REGION=us-east-1 cdk synth
CDK_DEFAULT_ACCOUNT=123456789012 cdk synth
```

## Layout

```
infra/
├── README.md              # this file
├── cdk.json               # tells the CDK CLI how to invoke app.py
├── app.py                 # entrypoint — wires stacks together
└── stacks/
    ├── __init__.py
    └── network.py         # placeholder; real VPC in Phase 4b
```

Each stack will be small enough to read top-to-bottom in one sitting
once it's filled in. The IAM policy reused by the agent task role
already lives at [`policies/strands_glue_pipeline_access.json`](policies/strands_glue_pipeline_access.json) —
the compute stack will load it via `iam.PolicyDocument.from_json` rather
than re-author it.
