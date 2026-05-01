# Glue Jobs Repository

Home for AWS Glue jobs produced by the `data_analyst_agent`. Each job lives in its own package under `jobs/`, is declared in `glue-jobs.yaml`, and is packaged + deployed by CI.

## Layout

```
repo-root/
├── glue-jobs.yaml                 # Manifest: one entry per deployed Glue job
├── jobs/
│   └── <job_name>/
│       ├── pyproject.toml         # Per-job deps and build config
│       ├── src/<job_name>/
│       │   ├── __init__.py
│       │   └── main.py            # Business logic — exposes main()
│       ├── tests/
│       │   ├── conftest.py
│       │   └── test_main.py
│       └── entrypoint.py          # 3-line shim Glue invokes
├── deploy/
│   ├── deploy.py                  # boto3-based deployer, invoked by CI
│   └── requirements.txt
└── .github/workflows/
    └── build-and-deploy.yml       # test → build-wheels → deploy
```

## Flow

1. Agent writes a job under `jobs/<name>/` and adds an entry to `glue-jobs.yaml`.
2. Agent pushes to a feature branch.
3. GitHub Actions runs `test` → `build-wheels` → `deploy`.
4. `deploy.py` uploads each job's wheel and entrypoint to S3, then creates or updates the corresponding Glue job to reference them.
5. Agent runs a `start-job-run` on the deployed job to verify.
6. Agent opens a PR. Humans review and merge.

## CI secrets required

Configured in **Settings → Secrets and variables → Actions** in this repo:

| Name | Purpose |
|---|---|
| `AWS_ACCESS_KEY_ID` | AWS creds for CI deploy. |
| `AWS_SECRET_ACCESS_KEY` | Same. |
| `AWS_REGION` | Region for Glue + S3. |
| `GLUE_ASSETS_BUCKET` | S3 bucket where `deploy.py` puts wheels and entrypoints. |
| `GLUE_JOB_ROLE_ARN` | IAM role the Glue jobs will assume. |

For production, switch static keys to GitHub OIDC federation (see [`aws-actions/configure-aws-credentials`](https://github.com/aws-actions/configure-aws-credentials)).

## S3 bucket layout

Everything the pipeline reads or writes lives in a single bucket (`GLUE_ASSETS_BUCKET`). `deploy.py` uploads to `wheels/` and `scripts/`; the agent uses `scratch/` and `temp/` directly; Athena writes query results to `athena-results/`.

```
<GLUE_ASSETS_BUCKET>/
├── athena-results/                      # Athena query output
├── wheels/<job-name>/*.whl              # written by deploy.py
├── scripts/<job-name>/entrypoint.py     # written by deploy.py, referenced by Glue
├── scratch/<conversation-id>/*.py       # Phase A scratch scripts (agent-managed)
└── temp/                                # Glue TempDir
```

## TODO (handled in separate branches)

- **EventBridge Scheduler**: `deploy.py` has scaffolding for creating schedules from `glue-jobs.yaml` `schedule:` blocks, but it has not been exercised yet. Leave `schedule:` out of manifest entries until we wire this up properly on a dedicated branch. Blocked on: a dedicated scheduler execution role (`SCHEDULER_GLUE_EXEC_ROLE_ARN`).
- **OIDC for AWS creds**: swap `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` for `aws-actions/configure-aws-credentials` with a role ARN. Requires creating an IAM role in AWS with a GitHub-OIDC trust relationship.
- **Per-environment deploys**: split `deploy` into `deploy-dev`/`deploy-prod` with branch gating once there's more than one account.

## Local development

Per job:

```bash
cd jobs/<job_name>
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest tests/
```

## Adding a new job by hand

The agent does this for you, but if you need to add one manually:

1. `cp -R jobs/example_python_shell jobs/<new_name>`.
2. Rename the inner `src/example_python_shell` to `src/<new_name>`.
3. Update `pyproject.toml` (`name`, package directories).
4. Put your logic in `src/<new_name>/main.py` — export a `main()` function.
5. Add an entry to `glue-jobs.yaml`.
6. Push. CI will build + deploy.
