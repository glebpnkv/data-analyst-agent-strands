"""
Deploy Glue jobs declared in glue-jobs.yaml.

Reads the manifest, uploads each job's wheel and entrypoint to S3, then
creates or updates the corresponding Glue job. Idempotent.

Expected environment:
    GLUE_ASSETS_BUCKET   Bucket where wheels and entrypoints go.
    GLUE_JOB_ROLE_ARN    IAM role the Glue job assumes at runtime.
    AWS_REGION           Also accepted as --region.

Expected filesystem layout (produced by the build-wheels CI job):
    dist/<job_name>/*.whl
    dist/<job_name>/entrypoint.py

Run:
    python deploy/deploy.py --region eu-central-1
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import boto3
import yaml
from botocore.exceptions import ClientError


def main() -> int:
    args = _parse_args()
    asset_bucket = _require_env("GLUE_ASSETS_BUCKET")
    role_arn = _require_env("GLUE_JOB_ROLE_ARN")

    manifest = _load_manifest(args.manifest)
    dist_root = Path(args.dist).resolve()
    if not dist_root.is_dir():
        raise SystemExit(f"dist directory not found: {dist_root}")

    s3 = boto3.client("s3", region_name=args.region)
    glue = boto3.client("glue", region_name=args.region)
    scheduler = boto3.client("scheduler", region_name=args.region)

    for job in manifest["jobs"]:
        name = job["name"]
        job_dir_name = Path(job["path"]).name
        job_dist = dist_root / job_dir_name

        wheel = _find_one_wheel(job_dist, name)
        entrypoint = job_dist / job.get("entrypoint", "entrypoint.py")
        if not entrypoint.exists():
            raise SystemExit(f"[{name}] entrypoint missing: {entrypoint}")

        wheel_key = f"wheels/{name}/{wheel.name}"
        entry_key = f"scripts/{name}/entrypoint.py"
        print(f"[{name}] uploading wheel -> s3://{asset_bucket}/{wheel_key}")
        s3.upload_file(str(wheel), asset_bucket, wheel_key)
        print(f"[{name}] uploading entrypoint -> s3://{asset_bucket}/{entry_key}")
        s3.upload_file(str(entrypoint), asset_bucket, entry_key)

        wheel_uri = f"s3://{asset_bucket}/{wheel_key}"
        entry_uri = f"s3://{asset_bucket}/{entry_key}"

        job_body = _build_job_body(job, role_arn, entry_uri, wheel_uri)
        _upsert_glue_job(glue, name, job_body)
        print(f"[{name}] deployed")

        schedule = job.get("schedule")
        if schedule:
            _upsert_schedule(scheduler, glue_client=glue, job=job, schedule=schedule, region=args.region)

    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"), required=False)
    parser.add_argument("--manifest", default="glue-jobs.yaml")
    parser.add_argument("--dist", default="dist")
    args = parser.parse_args()
    if not args.region:
        raise SystemExit("--region or AWS_REGION env var is required")
    return args


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} env var is required")
    return value


def _load_manifest(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    if not manifest or "jobs" not in manifest:
        raise SystemExit(f"{path} is missing a top-level 'jobs' list")
    return manifest


def _find_one_wheel(job_dist: Path, name: str) -> Path:
    wheels = sorted(job_dist.glob("*.whl"))
    if not wheels:
        raise SystemExit(f"[{name}] no wheel found under {job_dist}")
    if len(wheels) > 1:
        raise SystemExit(f"[{name}] expected one wheel under {job_dist}, found {len(wheels)}: {[w.name for w in wheels]}")
    return wheels[0]


def _build_job_body(job: dict[str, Any], role_arn: str, entry_uri: str, wheel_uri: str) -> dict[str, Any]:
    job_type = job["type"]
    if job_type not in {"python_shell", "pyspark"}:
        raise SystemExit(f"[{job['name']}] unsupported job type: {job_type}")

    default_args = {"--extra-py-files": wheel_uri}
    default_args.update(job.get("default_arguments") or {})

    body: dict[str, Any] = {
        "Role": role_arn,
        "DefaultArguments": default_args,
        "Timeout": int(job.get("timeout_minutes", 60)),
    }

    if job_type == "python_shell":
        body["Command"] = {
            "Name": "pythonshell",
            "ScriptLocation": entry_uri,
            "PythonVersion": str(job.get("python_version", "3.9")),
        }
        body["GlueVersion"] = str(job.get("glue_version", "3.0"))
        body["MaxCapacity"] = float(job.get("max_capacity", 0.0625))
    else:  # pyspark
        body["Command"] = {
            "Name": "glueetl",
            "ScriptLocation": entry_uri,
            "PythonVersion": str(job.get("python_version", "3")),
        }
        body["GlueVersion"] = str(job.get("glue_version", "5.0"))
        body["WorkerType"] = str(job.get("worker_type", "G.1X"))
        body["NumberOfWorkers"] = int(job.get("number_of_workers", 2))

    return body


def _upsert_glue_job(glue, name: str, body: dict[str, Any]) -> None:
    update_body = {k: v for k, v in body.items() if k != "Name"}
    try:
        glue.update_job(JobName=name, JobUpdate=update_body)
    except ClientError as e:
        if e.response["Error"].get("Code") == "EntityNotFoundException":
            glue.create_job(Name=name, **update_body)
        else:
            raise


def _upsert_schedule(scheduler, *, glue_client, job: dict[str, Any], schedule: dict[str, Any], region: str) -> None:
    """
    Create or update an EventBridge Scheduler entry that starts the Glue job.

    Scheduler target needs its own role; pull it from SCHEDULER_ATHENA_EXEC_ROLE_ARN
    if set (reusing the existing agent env convention), otherwise fail loudly.
    """
    schedule_role = os.environ.get("SCHEDULER_GLUE_EXEC_ROLE_ARN") or os.environ.get("SCHEDULER_ATHENA_EXEC_ROLE_ARN")
    if not schedule_role:
        raise SystemExit(
            f"[{job['name']}] schedule declared but SCHEDULER_GLUE_EXEC_ROLE_ARN env var is not set"
        )

    name = f"glue-{job['name']}"
    cron = schedule["cron"]
    tz = schedule.get("timezone", "UTC")

    params = {
        "Name": name,
        "ScheduleExpression": f"cron({cron})",
        "ScheduleExpressionTimezone": tz,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {
            "Arn": "arn:aws:scheduler:::aws-sdk:glue:startJobRun",
            "RoleArn": schedule_role,
            "Input": f'{{"JobName": "{job["name"]}"}}',
        },
    }

    try:
        scheduler.update_schedule(**params)
        print(f"[{job['name']}] schedule updated")
    except ClientError as e:
        if e.response["Error"].get("Code") == "ResourceNotFoundException":
            scheduler.create_schedule(**params)
            print(f"[{job['name']}] schedule created")
        else:
            raise


if __name__ == "__main__":
    sys.exit(main())
