#!/usr/bin/env bash
set -euo pipefail

# This script prepares Glue job prerequisites only.
# It does NOT deploy the AgentCore runtime.
#
# What it does:
# 1) Creates/updates a Glue execution IAM role with proper trust policy.
# 2) Attaches AWS managed Glue service role policy.
# 3) Adds scoped S3 access for script/temp prefixes and job data bucket.
# 4) Adds Glue Data Catalog permissions so outputs can be registered in Athena.
# 5) Creates/updates an EventBridge Scheduler execution role for Athena SQL schedules.
# 6) Uploads a default Python Shell script to S3.
# 7) Prints export lines for:
#    - GLUE_JOB_ROLE_ARN
#    - SCHEDULER_ATHENA_EXEC_ROLE_ARN
#    - GLUE_JOB_DEFAULT_SCRIPT_S3
#    - GLUE_TEMP_DIR

export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="${AWS_REGION:-eu-central-1}"

: "${AWS_REGION:?set AWS_REGION}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="${ROOT_DIR}/agent"

GLUE_ASSETS_BUCKET="${GLUE_ASSETS_BUCKET:-}"
GLUE_JOB_ROLE_NAME="${GLUE_JOB_ROLE_NAME:-StrandsGluePythonJobRole}"
GLUE_JOB_DEFAULT_SCRIPT_KEY="${GLUE_JOB_DEFAULT_SCRIPT_KEY:-scripts/default_python_shell_job.py}"
GLUE_TEMP_PREFIX="${GLUE_TEMP_PREFIX:-temp/}"
# Bucket used by Glue jobs for input/output data paths.
# Defaults to your current demo bucket.
GLUE_DATA_BUCKET="${GLUE_DATA_BUCKET:-langchain-strands}"
SCHEDULER_ATHENA_EXEC_ROLE_NAME="${SCHEDULER_ATHENA_EXEC_ROLE_NAME:-StrandsSchedulerAthenaExecutionRole}"
ENABLE_SCHEDULER_ATHENA_PREREQS="${ENABLE_SCHEDULER_ATHENA_PREREQS:-1}"
# Optional workgroup scoping for athena:StartQueryExecution.
# Leave empty to allow all workgroups.
ATHENA_SCHEDULER_WORKGROUP_NAME="${ATHENA_SCHEDULER_WORKGROUP_NAME:-}"
# Scheduler-executed Athena queries read source data here.
ATHENA_QUERY_SOURCE_BUCKET="${ATHENA_QUERY_SOURCE_BUCKET:-${GLUE_DATA_BUCKET}}"
# Scheduler-executed Athena queries write result files here.
ATHENA_QUERY_RESULTS_BUCKET="${ATHENA_QUERY_RESULTS_BUCKET:-${GLUE_ASSETS_BUCKET}}"
# Optional prefix to scope Athena result writes in ATHENA_QUERY_RESULTS_BUCKET.
ATHENA_QUERY_RESULTS_PREFIX="${ATHENA_QUERY_RESULTS_PREFIX:-athena-results/}"
CREATE_BUCKET_IF_MISSING="${CREATE_BUCKET_IF_MISSING:-0}"

if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: AWS CLI is required." >&2
  exit 1
fi

if [[ -z "${GLUE_ASSETS_BUCKET}" ]]; then
  echo "ERROR: set GLUE_ASSETS_BUCKET to an existing bucket name." >&2
  echo "Example: GLUE_ASSETS_BUCKET=my-data-platform-assets ./agent/setup_glue_job_prereqs.sh" >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --no-cli-pager)"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${GLUE_JOB_ROLE_NAME}"
SCHEDULER_ATHENA_EXEC_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${SCHEDULER_ATHENA_EXEC_ROLE_NAME}"
DEFAULT_SCRIPT_LOCAL="${AGENT_DIR}/default_python_shell_job.py"
GLUE_JOB_DEFAULT_SCRIPT_S3="s3://${GLUE_ASSETS_BUCKET}/${GLUE_JOB_DEFAULT_SCRIPT_KEY}"
GLUE_TEMP_DIR="s3://${GLUE_ASSETS_BUCKET}/${GLUE_TEMP_PREFIX%/}/"

if [[ -n "${ATHENA_SCHEDULER_WORKGROUP_NAME}" ]]; then
  ATHENA_SCHEDULER_WORKGROUP_RESOURCE="arn:aws:athena:${AWS_REGION}:${ACCOUNT_ID}:workgroup/${ATHENA_SCHEDULER_WORKGROUP_NAME}"
else
  ATHENA_SCHEDULER_WORKGROUP_RESOURCE="*"
fi

verify_role_trust_for_glue() {
  local role_name="$1"
  local trust_services
  trust_services="$(
    aws iam get-role \
      --role-name "${role_name}" \
      --query "Role.AssumeRolePolicyDocument.Statement[].Principal.Service" \
      --output text \
      --no-cli-pager 2>/dev/null || true
  )"

  if ! grep -q "glue.amazonaws.com" <<< "${trust_services}"; then
    echo "ERROR: Role ${role_name} trust policy does not include glue.amazonaws.com." >&2
    echo "Update trust policy and re-run this script." >&2
    exit 1
  fi
  echo "[OK] Verified role trust includes glue.amazonaws.com: ${role_name}"
}

verify_role_trust_for_scheduler() {
  local role_name="$1"
  local trust_services
  trust_services="$(
    aws iam get-role \
      --role-name "${role_name}" \
      --query "Role.AssumeRolePolicyDocument.Statement[].Principal.Service" \
      --output text \
      --no-cli-pager 2>/dev/null || true
  )"

  if ! grep -q "scheduler.amazonaws.com" <<< "${trust_services}"; then
    echo "ERROR: Role ${role_name} trust policy does not include scheduler.amazonaws.com." >&2
    echo "Update trust policy and re-run this script." >&2
    exit 1
  fi
  echo "[OK] Verified role trust includes scheduler.amazonaws.com: ${role_name}"
}

ensure_bucket() {
  if aws s3api head-bucket --bucket "${GLUE_ASSETS_BUCKET}" --no-cli-pager >/dev/null 2>&1; then
    return 0
  fi

  if [[ "${CREATE_BUCKET_IF_MISSING}" != "1" ]]; then
    echo "ERROR: Bucket ${GLUE_ASSETS_BUCKET} does not exist or is not accessible." >&2
    echo "Set CREATE_BUCKET_IF_MISSING=1 if you want this script to create it." >&2
    exit 1
  fi

  if [[ "${AWS_REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${GLUE_ASSETS_BUCKET}" --no-cli-pager >/dev/null
  else
    aws s3api create-bucket \
      --bucket "${GLUE_ASSETS_BUCKET}" \
      --create-bucket-configuration LocationConstraint="${AWS_REGION}" \
      --no-cli-pager >/dev/null
  fi
  echo "[OK] Created bucket: ${GLUE_ASSETS_BUCKET}"
}

ensure_glue_role() {
  local trust_file
  trust_file="$(mktemp)"

  cat > "${trust_file}" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "glue.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
JSON

  if aws iam get-role --role-name "${GLUE_JOB_ROLE_NAME}" --no-cli-pager >/dev/null 2>&1; then
    aws iam update-assume-role-policy \
      --role-name "${GLUE_JOB_ROLE_NAME}" \
      --policy-document "file://${trust_file}" \
      --no-cli-pager >/dev/null
    echo "[OK] Updated trust policy for role: ${GLUE_JOB_ROLE_NAME}"
  else
    aws iam create-role \
      --role-name "${GLUE_JOB_ROLE_NAME}" \
      --assume-role-policy-document "file://${trust_file}" \
      --description "Execution role for Glue Python shell jobs managed by data_analyst_agent" \
      --no-cli-pager >/dev/null
    echo "[OK] Created role: ${GLUE_JOB_ROLE_NAME}"
  fi

  rm -f "${trust_file}"

  aws iam attach-role-policy \
    --role-name "${GLUE_JOB_ROLE_NAME}" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole" \
    --no-cli-pager >/dev/null || true
}

ensure_scheduler_athena_exec_role() {
  local trust_file
  trust_file="$(mktemp)"

  cat > "${trust_file}" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "scheduler.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
JSON

  if aws iam get-role --role-name "${SCHEDULER_ATHENA_EXEC_ROLE_NAME}" --no-cli-pager >/dev/null 2>&1; then
    aws iam update-assume-role-policy \
      --role-name "${SCHEDULER_ATHENA_EXEC_ROLE_NAME}" \
      --policy-document "file://${trust_file}" \
      --no-cli-pager >/dev/null
    echo "[OK] Updated trust policy for role: ${SCHEDULER_ATHENA_EXEC_ROLE_NAME}"
  else
    aws iam create-role \
      --role-name "${SCHEDULER_ATHENA_EXEC_ROLE_NAME}" \
      --assume-role-policy-document "file://${trust_file}" \
      --description "Execution role for EventBridge Scheduler to run Athena SQL for data_analyst_agent" \
      --no-cli-pager >/dev/null
    echo "[OK] Created role: ${SCHEDULER_ATHENA_EXEC_ROLE_NAME}"
  fi

  rm -f "${trust_file}"
}

attach_s3_policy() {
  local s3_policy_file
  s3_policy_file="$(mktemp)"

  cat > "${s3_policy_file}" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListAssetsBucket",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::${GLUE_ASSETS_BUCKET}"
    },
    {
      "Sid": "ReadWriteAssetsBucket",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListBucketMultipartUploads",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::${GLUE_ASSETS_BUCKET}/*"
    },
    {
      "Sid": "ListDataBucketForGlueJobs",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::${GLUE_DATA_BUCKET}"
    },
    {
      "Sid": "ReadWriteDataBucketObjectsForGlueJobs",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListBucketMultipartUploads",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::${GLUE_DATA_BUCKET}/*"
    }
  ]
}
JSON

  aws iam put-role-policy \
    --role-name "${GLUE_JOB_ROLE_NAME}" \
    --policy-name "StrandsGlueJobS3Access" \
    --policy-document "file://${s3_policy_file}" \
    --no-cli-pager >/dev/null

  rm -f "${s3_policy_file}"
  echo "[OK] Ensured inline S3 policy on role: ${GLUE_JOB_ROLE_NAME}"
}

attach_glue_catalog_policy() {
  local catalog_policy_file
  catalog_policy_file="$(mktemp)"

  cat > "${catalog_policy_file}" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GlueCatalogReadWriteForAthenaRegistration",
      "Effect": "Allow",
      "Action": [
        "glue:GetDatabase",
        "glue:GetDatabases",
        "glue:CreateDatabase",
        "glue:UpdateDatabase",
        "glue:GetTable",
        "glue:GetTables",
        "glue:CreateTable",
        "glue:UpdateTable",
        "glue:DeleteTable",
        "glue:GetPartition",
        "glue:GetPartitions",
        "glue:CreatePartition",
        "glue:BatchCreatePartition",
        "glue:UpdatePartition",
        "glue:DeletePartition",
        "glue:BatchDeletePartition"
      ],
      "Resource": "*"
    },
    {
      "Sid": "AthenaQueryExecutionForGlueJobs",
      "Effect": "Allow",
      "Action": [
        "athena:StartQueryExecution",
        "athena:GetQueryExecution",
        "athena:GetQueryResults",
        "athena:StopQueryExecution",
        "athena:GetWorkGroup"
      ],
      "Resource": "*"
    }
  ]
}
JSON

  aws iam put-role-policy \
    --role-name "${GLUE_JOB_ROLE_NAME}" \
    --policy-name "StrandsGlueCatalogAccess" \
    --policy-document "file://${catalog_policy_file}" \
    --no-cli-pager >/dev/null

  rm -f "${catalog_policy_file}"
  echo "[OK] Ensured Glue Data Catalog policy on role: ${GLUE_JOB_ROLE_NAME}"
}

attach_scheduler_athena_policy() {
  local scheduler_policy_file
  scheduler_policy_file="$(mktemp)"

  cat > "${scheduler_policy_file}" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AthenaStartQueryExecutionForScheduler",
      "Effect": "Allow",
      "Action": [
        "athena:StartQueryExecution"
      ],
      "Resource": "${ATHENA_SCHEDULER_WORKGROUP_RESOURCE}"
    },
    {
      "Sid": "AthenaGetDataCatalogForExternalCatalogs",
      "Effect": "Allow",
      "Action": [
        "athena:GetDataCatalog"
      ],
      "Resource": "*"
    },
    {
      "Sid": "GlueCatalogReadWriteForAthenaSql",
      "Effect": "Allow",
      "Action": [
        "glue:GetDatabase",
        "glue:GetDatabases",
        "glue:CreateDatabase",
        "glue:UpdateDatabase",
        "glue:GetTable",
        "glue:GetTables",
        "glue:CreateTable",
        "glue:UpdateTable",
        "glue:DeleteTable",
        "glue:GetPartition",
        "glue:GetPartitions",
        "glue:CreatePartition",
        "glue:BatchCreatePartition",
        "glue:UpdatePartition",
        "glue:DeletePartition",
        "glue:BatchDeletePartition"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ListSourceAndResultsBucketsForAthenaSql",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::${ATHENA_QUERY_SOURCE_BUCKET}",
        "arn:aws:s3:::${ATHENA_QUERY_RESULTS_BUCKET}"
      ]
    },
    {
      "Sid": "ReadSourceDataForAthenaSql",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::${ATHENA_QUERY_SOURCE_BUCKET}/*"
    },
    {
      "Sid": "ReadWriteResultsForAthenaSql",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListBucketMultipartUploads",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::${ATHENA_QUERY_RESULTS_BUCKET}/${ATHENA_QUERY_RESULTS_PREFIX%/}/*"
    }
  ]
}
JSON

  aws iam put-role-policy \
    --role-name "${SCHEDULER_ATHENA_EXEC_ROLE_NAME}" \
    --policy-name "StrandsSchedulerAthenaExecutionAccess" \
    --policy-document "file://${scheduler_policy_file}" \
    --no-cli-pager >/dev/null

  rm -f "${scheduler_policy_file}"
  echo "[OK] Ensured Scheduler Athena execution policy on role: ${SCHEDULER_ATHENA_EXEC_ROLE_NAME}"
}

upload_default_script() {
  if [[ ! -f "${DEFAULT_SCRIPT_LOCAL}" ]]; then
    echo "ERROR: Missing local default script: ${DEFAULT_SCRIPT_LOCAL}" >&2
    exit 1
  fi

  aws s3 cp "${DEFAULT_SCRIPT_LOCAL}" "${GLUE_JOB_DEFAULT_SCRIPT_S3}" --no-cli-pager >/dev/null
  echo "[OK] Uploaded default script: ${GLUE_JOB_DEFAULT_SCRIPT_S3}"
}

ensure_bucket
ensure_glue_role
verify_role_trust_for_glue "${GLUE_JOB_ROLE_NAME}"

if [[ "${ENABLE_SCHEDULER_ATHENA_PREREQS}" == "1" ]]; then
  ensure_scheduler_athena_exec_role
  verify_role_trust_for_scheduler "${SCHEDULER_ATHENA_EXEC_ROLE_NAME}"
fi

# Optional warning for the legacy default Glue role frequently used by mistake.
if aws iam get-role --role-name "AWSGlueServiceRole-default" --no-cli-pager >/dev/null 2>&1; then
  if ! aws iam get-role \
    --role-name "AWSGlueServiceRole-default" \
    --query "Role.AssumeRolePolicyDocument.Statement[].Principal.Service" \
    --output text \
    --no-cli-pager 2>/dev/null | grep -q "glue.amazonaws.com"; then
    echo "[WARN] AWSGlueServiceRole-default exists but does not trust glue.amazonaws.com."
    echo "[WARN] Do not use AWSGlueServiceRole-default for job_definition.Role."
  fi
fi

attach_s3_policy
attach_glue_catalog_policy
if [[ "${ENABLE_SCHEDULER_ATHENA_PREREQS}" == "1" ]]; then
  attach_scheduler_athena_policy
fi
upload_default_script

echo
echo "Prerequisites ready. Use these values for future deployment:"
echo "export GLUE_JOB_ROLE_ARN='${ROLE_ARN}'"
if [[ "${ENABLE_SCHEDULER_ATHENA_PREREQS}" == "1" ]]; then
  echo "export SCHEDULER_ATHENA_EXEC_ROLE_ARN='${SCHEDULER_ATHENA_EXEC_ROLE_ARN}'"
fi
echo "export GLUE_JOB_DEFAULT_SCRIPT_S3='${GLUE_JOB_DEFAULT_SCRIPT_S3}'"
echo "export GLUE_TEMP_DIR='${GLUE_TEMP_DIR}'"
echo
echo "Notes:"
echo "- Set job_definition.Role to GLUE_JOB_ROLE_ARN (avoid AWSGlueServiceRole-default)."
if [[ "${ENABLE_SCHEDULER_ATHENA_PREREQS}" == "1" ]]; then
  echo "- Use SCHEDULER_ATHENA_EXEC_ROLE_ARN when creating EventBridge Scheduler targets for athena:startQueryExecution."
  if [[ -n "${ATHENA_SCHEDULER_WORKGROUP_NAME}" ]]; then
    echo "- Scheduler role scopes athena:StartQueryExecution to workgroup ${ATHENA_SCHEDULER_WORKGROUP_NAME}."
  else
    echo "- Scheduler role allows athena:StartQueryExecution on all workgroups (set ATHENA_SCHEDULER_WORKGROUP_NAME to scope it)."
  fi
fi
echo "- For Python shell jobs use Command.Name=pythonshell."
echo "- For crawler-based conditional triggers, crawler must exist and trigger condition should use CrawlState."
echo "- Role includes Glue Data Catalog permissions so outputs can be registered as Athena tables."
echo "- GLUE_JOB_DEFAULT_SCRIPT_S3 must be a full object URI (s3://bucket/path/file.py)."
echo "- GLUE_TEMP_DIR must be an S3 prefix URI (s3://bucket/path/), usually ending with '/'."
echo "- GLUE_DATA_BUCKET is where job inputs/outputs live (default: langchain-strands)."
