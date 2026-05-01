---
name: project-structure
description: Defines the directory layout, package naming, and entrypoint conventions the agent MUST follow when committing generated job code to the target GitHub repo. Activate this skill in Phase B (after scratch iteration succeeds) before calling `github_push_files`.
---

# Project Structure Skill

## Purpose

This skill tells the agent exactly how to lay out the generated Glue job code inside the target GitHub repo so that:

- the downstream CI pipeline can package the code into a `.whl`,
- the committed Glue entrypoint script can correctly import from that wheel via `--extra-py-files`,
- repeated runs produce a consistent repo history.

Activate this skill in Phase B of the `glue-python-shell-job` workflow — after scratch iteration has produced working logic and before calling `github_push_files`.

## Directory layout

```
repo-root/
├── README.md
├── glue-jobs.yaml                       # Describes every job in the repo
├── jobs/
│   ├── daily_sales/                     # Example PySpark job
│   │   ├── pyproject.toml               # This job's deps
│   │   ├── src/daily_sales/
│   │   │   ├── __init__.py
│   │   │   └── main.py                  # The real code
│   │   ├── tests/
│   │   │   ├── conftest.py              # Local Spark session fixture
│   │   │   └── test_main.py
│   │   └── entrypoint.py                # 3-line bootloader Glue invokes
│   └── customer_report/                 # Example Python shell job
│       └── ... (same shape)
├── deploy/
│   ├── deploy.py                        # boto3-based deployer invoked by CI
│   └── requirements.txt                 # deploy.py deps
└── .github/workflows/
    └── build-and-deploy.yml             # test → build-wheels → deploy
```

A reference scaffold of this layout is maintained at `agent/target_repo_template/` in this repo. The agent should treat it as the canonical example of the layout it must produce.

## `glue-jobs.yaml` manifest

```yaml
version: 1

jobs:
  - name: daily-sales
    type: pyspark
    path: jobs/daily_sales          # path to the package folder
    entrypoint: entrypoint.py       # inside that folder
    description: "Aggregate yesterday's sales by product"
    glue_version: "5.0"
    worker_type: "G.1X"
    number_of_workers: 3
    timeout_minutes: 60
    default_arguments:
      --source_bucket: "raw-sales-data"
      --target_bucket: "processed-sales-data"
    schedule:
      cron: "0 2 * * ? *"
      timezone: "UTC"

  - name: customer-report
    type: python_shell
    path: jobs/customer_report
    entrypoint: entrypoint.py
    description: "Generate a CSV customer report on demand"
    glue_version: "3.0"
    max_capacity: 0.0625       # smallest size (~$0.01/min)
    timeout_minutes: 10
    default_arguments:
      --output_path: "s3://reports/customer/"
    # No schedule = on-demand only
```

## Package and module naming

- **Job directory name** (under `jobs/`): `snake_case`, short, identifies what the job does. Example: `daily_sales`, `customer_report`. This is also the Python package name.
- **Python package** (under `jobs/<job_name>/src/`): same name as the job directory. So `jobs/daily_sales/src/daily_sales/` contains `__init__.py` and `main.py`.
- **Manifest name** (in `glue-jobs.yaml`): `kebab-case` version of the job directory. Example: `jobs/daily_sales/` → `name: daily-sales`. The manifest name is the **Glue job name** in AWS.
- **Wheel name** (CI output): `<job_name>-<version>-py3-none-any.whl`, e.g. `daily_sales-0.1.0-py3-none-any.whl`. Version comes from `pyproject.toml`. The agent does not need to compute the wheel name — `deploy.py` finds it by glob under `dist/<job_name>/`.
- **Tests**: live in `jobs/<job_name>/tests/` and ARE committed. CI runs `pytest tests/` per job.

## `pyproject.toml` dependencies

Every non-stdlib module imported anywhere under `src/<job_name>/` or `tests/` MUST be listed in `pyproject.toml`. This includes `boto3`, `botocore`, and any third-party libraries.

**Why this matters even though Glue provides boto3 at runtime**: CI runs `pip install -e "jobs/<name>[dev]"` before `pytest`, and that install only pulls what's declared in `pyproject.toml`. If `main.py` has `import boto3` but `dependencies = []`, test collection fails with `ModuleNotFoundError: No module named 'boto3'` before a single test runs — this is how the CI pipeline breaks on push.

Rules:

- **Runtime imports** (anything `main.py` or modules under `src/<job_name>/` import) go in `[project].dependencies`.
- **Test-only imports** (e.g. `pytest`, `pytest-mock`, `moto`) go in `[project.optional-dependencies].dev` alongside `pytest>=7`.
- Pin to major versions when in doubt (`boto3>=1.34`), not exact versions.
- Do NOT list stdlib modules (`argparse`, `json`, `sys`, `os`, `pathlib`, `datetime`, etc.).
- Do NOT list `awsglue` — it is a Glue-runtime-only package and cannot be pip-installed. If a job needs it, mock it in tests.

Example for a Python shell job that uses boto3:

```toml
[build-system]
requires = ["setuptools>=74", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "customer_report"
version = "0.1.0"
requires-python = ">=3.9"
dependencies = [
    "boto3>=1.34",
]

[project.optional-dependencies]
dev = [
    "pytest>=7",
]

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-dir]
"" = "src"
```

**The `[build-system]` block is mandatory.** `setuptools>=74` is required — older versions fail CI's editable install with `BackendUnavailable: Cannot import 'setuptools.backends.legacy'` under modern pip. Copy the `[build-system]` and `[tool.setuptools.*]` blocks verbatim unless you have a reason to change them.

## Entrypoint conventions

The `entrypoint.py` for each job is a **3-line bootloader** whose only purpose is to import from the wheel and invoke its `main()`. It lives at `jobs/<job_name>/entrypoint.py` and is uploaded to S3 by `deploy.py`.

For a **Python shell job**:

```python
# jobs/<job_name>/entrypoint.py
from <job_name>.main import main

if __name__ == "__main__":
    main()
```

For a **PySpark job**:

```python
# jobs/<job_name>/entrypoint.py
from <job_name>.main import main

if __name__ == "__main__":
    main()
```

Same shape. All Glue-specific plumbing (argument parsing, GlueContext setup for PySpark, etc.) lives inside `src/<job_name>/main.py`, not in the entrypoint.

**Why this split**: the entrypoint is whatever Glue's `ScriptLocation` points at — CI uploads it separately. The wheel is mounted via `--extra-py-files`, which makes `<job_name>` importable. Keeping the entrypoint trivial means a wheel upgrade is all you need to ship new logic; the entrypoint itself almost never changes.

**Argument parsing**: `main()` is responsible for reading Glue job arguments. For Python shell, use `argparse` against `sys.argv[1:]`. For PySpark, use `awsglue.utils.getResolvedOptions`. Keep argument names aligned with the `default_arguments:` block of the job's `glue-jobs.yaml` entry.

**Difference from the scratch-phase loose script**: in Phase A the scratch script is a single self-contained `.py` with business logic inline. In Phase B that logic moves into `src/<job_name>/main.py` and the entrypoint becomes the 3-line shim above. The scratch script is NOT committed.

## What not to commit

The agent must NEVER `github_push_files` with paths matching any of:

- `.venv/`, `venv/`, `env/`, `.env*` — local virtualenvs and secrets
- `dist/`, `build/`, `*.egg-info/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/` — build and cache artefacts
- `*.whl`, `*.tar.gz` — wheel files (CI builds these; never commit)
- `.DS_Store`, `Thumbs.db` — OS clutter
- Any scratch-phase job scripts — those are for Phase A iteration only
- Any cloud credentials, tokens, or account IDs
- The target repo should have a `.gitignore` covering all the above; if it does not, the agent should add one.

## Files the agent owns vs CI owns

| File | Owner | Notes |
|---|---|---|
| `jobs/<name>/src/<name>/*.py` | Agent | Business logic. |
| `jobs/<name>/entrypoint.py` | Agent | 3-line bootloader. |
| `jobs/<name>/pyproject.toml` | Agent | Job deps + build config. |
| `jobs/<name>/tests/*` | Agent | Unit tests. |
| `glue-jobs.yaml` | Agent | Add/update the entry for the job being built. |
| `deploy/deploy.py` | Human | Agent does not modify. May read for context. |
| `deploy/requirements.txt` | Human | Same. |
| `.github/workflows/*.yml` | Human | Same. |
| `README.md` | Human | Agent may append a line about a new job but does not rewrite. |

If the agent thinks a file in the "Human" column needs to change, it should flag this in the PR description, not attempt the change itself.
