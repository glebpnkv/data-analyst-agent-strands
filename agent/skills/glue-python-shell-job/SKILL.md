---
name: glue-python-shell-job
description: Build, test, and deploy a Glue Python Shell job in two phases — scratch iteration in a dev AWS account, then a wheel-based production job committed to GitHub via PR. Use this skill for small-to-medium ETL jobs whose logic is easier to express in plain Python than in Athena SQL.
---

# Glue Python Shell Job Skill

## Purpose

Produce a Glue Python Shell job for batch-style data processing where the logic is easier in plain Python than in Athena SQL, and the workload fits a single machine.

This skill runs in **two phases**:

- **Phase A — Scratch iteration and commit** (dev AWS account + GitHub). Iterate on a loose `.py` script run as a `scratch-<conversation-id>-*` Glue job until it succeeds, then lay the code out per the `project-structure` skill, push to a feature branch, and end the turn waiting for CI.
- **Phase B — Production verification and PR** (on resume, after CI is green). Verify the CI-deployed Glue job runs end-to-end with the wheel, then open the PR.

## When to use

Use this skill only if ALL of the following are true:

- one-off batch transformation over existing Glue/Athena data (or raw-S3 data per the `s3-raw-data-ingestion` skill),
- the logic is awkward in Athena SQL but straightforward in Python,
- the workload is suitable for single-node execution,
- the job reads a fixed input snapshot and writes a fresh output dataset,
- the pipeline does NOT require: incremental processing, job bookmarks, merge/upsert/update/delete semantics, triggers or schedules (beyond what the user explicitly asks for), crawlers, workflows, or Spark.

Treat the job as write-new-output only. Not a mutable pipeline component.

---

## Phase A — Scratch iteration and commit

Goal: prove the logic works in a dev Glue job, then lay it down in the repo, push, and stop.

### A.1 — Scratch iteration (dev AWS account)

1. **Discover source metadata first**
   - Athena flow: use `athena_manage_aws_athena_databases_and_tables` with `catalog_name="AwsDataCatalog"` and `work_group="primary"` on every Athena call.
   - Raw-S3 flow: follow the `s3-raw-data-ingestion` skill for discovery.

2. **Create a scratch Glue Python Shell job**
   - Job name MUST start with `scratch-<conversation-id>-` so it is identifiable and cleanable.
   - `job_definition.Command.Name = "pythonshell"`, `PythonVersion = "3.9"`.
   - Prefer `MaxCapacity`. Do not set `WorkerType` / `NumberOfWorkers`.
   - Do NOT use Spark APIs: no `SparkContext`, `GlueContext`, `DynamicFrame`, `pyspark`.
   - Do NOT require `--JOB_NAME` unless explicitly passed in `job_arguments`.
   - Do NOT set `--extra-py-files` in Phase A. The scratch script is self-contained.
   - Do NOT use `--additional-python-modules`.

3. **Write the scratch script plain and self-contained**
   - Plain Python using the standard Glue Python Shell runtime.
   - All logic in the single script file. No external packages.
   - Read from source → transform → write to a new S3 output location. Snapshot-in, snapshot-out.

4. **Stage the scratch script**
   - Use `athena_list_s3_buckets` to identify a staging location if needed.
   - Use `athena_upload_to_s3` to upload the script.

5. **Test**
   - Run one `start-job-run`.
   - Poll `get-job-run` until terminal state via `athena_manage_aws_glue_jobs` (NOT via `awsapi_call_aws`). The poll throttle only guards the MCP path.
   - If `FAILED` / `TIMEOUT`, call `glue_get_job_run_diagnostics` and include root-cause logs. Iterate.
   - Do NOT proceed to A.2 until `SUCCEEDED`.

6. **Do not set up orchestration in Phase A**
   - No triggers, no schedules, no workflows, no crawlers.

### A.2 — Commit and push (GitHub)

Once the scratch run reaches `SUCCEEDED`, lay the working logic down in the target repo and push.

**Critical division of responsibilities:**
- The agent DOES: write the job's Python package, add/update the job's entry in `glue-jobs.yaml`, commit, push. Later (Phase B): verify, open PR.
- The agent DOES NOT: create or update the production Glue job via MCP. `deploy/deploy.py` (invoked by GitHub Actions) owns production Glue job creation, wheel upload, and entrypoint upload. Calling `create-job` or `update-job` against the production job name is a bug.

Follow `_build_git_rules()` in the system prompt for branch/commit mechanics. This section covers only the Glue-specific layout.

1. **Activate the `project-structure` skill and lay out the code**
   - Read and follow `project-structure` exactly: directory layout, package naming, entrypoint conventions, and — importantly — `pyproject.toml` dependency declarations. Every non-stdlib import in your `main.py` (including `boto3`) MUST be listed in `[project].dependencies` or CI tests will fail at import time.
   - The committed entrypoint is a THIN shim: import the packaged module and call its `main()`. No business logic in the entrypoint.
   - Add or update the job's entry in the top-level `glue-jobs.yaml` manifest. The manifest entry is what tells CI's `deploy.py` to create the Glue job.

2. **Commit and push**
   - `github_list_branches` → `github_create_branch` from default → `github_push_files` in a single batch for the whole feature branch.
   - Commit message summarises the job purpose.

3. **Stop and wait for CI**
   - After pushing, END THE TURN. Tell the user the branch is pushed and ask them to resume once the GitHub Actions pipeline (`test` → `build-wheels` → `deploy`) has gone green.
   - Do not poll. Do not guess CI is green.

---

## Phase B — Production verification and PR (on resume)

Goal: confirm the CI-deployed Glue job works end-to-end with the wheel, then open the PR.

1. **Verify CI is green**
   - `github_list_workflow_runs` filtered by the feature branch → `github_get_workflow_run` for the latest. If conclusion is not `success`, call `github_list_workflow_jobs` + `github_get_job_logs` for the failed job, surface root-cause log lines, and stop. Do NOT touch the Glue job.

2. **Verify the deployed Glue job**
   - Look up the Glue job by the name from `manifest.jobs[].name` — it exists because `deploy.py` created or updated it.
   - Run one `start-job-run` against it.
   - Poll `get-job-run` via `athena_manage_aws_glue_jobs` until terminal state.
   - If `FAILED` / `TIMEOUT`, call `glue_get_job_run_diagnostics`, surface root-cause logs, and stop. Do NOT open the PR. Iterate by pushing a fix commit to the same feature branch and waiting for CI again.
   - Only if `SUCCEEDED`: proceed.
   - **Do not `create-job` or `update-job` here.** If the job doesn't exist, CI hasn't deployed yet — tell the user and stop.

3. **Register output metadata**
   - After the production run succeeds, create or update the Athena catalog table for the output dataset directly. Do not use a crawler.
   - Report the final `database.table` and S3 output location.

4. **Open the PR**
   - `github_create_pull_request` against the default branch.
   - Title: concise summary.
   - Body: what the job does, source, output `database.table`, output S3 path, Glue job name, production job run ID, and a link to the green CI run.
   - END THE TURN. Do not watch for merge.

---

## Code-interpreter use (both phases)

- Use the code interpreter only for calculations, summaries, and plotting.
- Never manually transcribe tabular output into Python.
- Use `athena_query_to_ci_csv` before plotting or statistics.
- Read from the sandbox file path returned by that tool.

## Failure handling

- On any tool failure, explain the likely AWS/GitHub configuration issue clearly.
- Do not claim completion unless:
  - Phase A: scratch run succeeded AND feature branch pushed.
  - Phase B: CI green, production run succeeded, Athena metadata registered, PR opened.

## Best practices

- Prefer Athena query execution when the transformation can be cleanly expressed in SQL.
- Use this skill only when plain Python materially simplifies the logic.
- Keep the job small, single-purpose, deterministic.
- Return the exact final Athena `database.table`, S3 output location, and PR URL.
