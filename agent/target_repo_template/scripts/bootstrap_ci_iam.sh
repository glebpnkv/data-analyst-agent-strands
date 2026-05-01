#!/usr/bin/env bash
#
# Create the IAM user that GitHub Actions uses to deploy Glue jobs from this repo,
# attach a least-privilege inline policy, and mint an access key pair.
#
# Idempotent: safe to re-run. Creating a second access key when two already exist
# will fail — delete the old one in the console first, or pass --rotate.
#
# Usage:
#   ./scripts/bootstrap_ci_iam.sh
#
# Required env vars (or edit the defaults below):
#   AWS_REGION              e.g. eu-central-1
#   GLUE_ASSETS_BUCKET      e.g. glue-assets-554032904022-eu-central-1-an
#   GLUE_JOB_ROLE_ARN       IAM role the deployed Glue jobs assume at runtime
#
# After running, copy the printed AccessKeyId / SecretAccessKey into the repo's
# GitHub Actions secrets (Settings -> Secrets and variables -> Actions).
#
# To migrate to OIDC later, delete this user and create an IAM role with a
# GitHub OIDC trust policy instead. See:
#   https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services

set -euo pipefail

USER_NAME="${USER_NAME:-github-actions-glue-deploy}"
POLICY_NAME="${POLICY_NAME:-glue-deploy}"
ROTATE="${ROTATE:-0}"

: "${AWS_REGION:?AWS_REGION must be set}"
: "${GLUE_ASSETS_BUCKET:?GLUE_ASSETS_BUCKET must be set}"
: "${GLUE_JOB_ROLE_ARN:?GLUE_JOB_ROLE_ARN must be set}"

for arg in "$@"; do
  case "$arg" in
    --rotate) ROTATE=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

echo "==> Ensuring IAM user: $USER_NAME"
if ! aws iam get-user --user-name "$USER_NAME" >/dev/null 2>&1; then
  aws iam create-user --user-name "$USER_NAME" >/dev/null
  echo "    created"
else
  echo "    already exists"
fi

echo "==> Writing inline policy: $POLICY_NAME"
POLICY_DOC=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3AssetRW",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": [
        "arn:aws:s3:::${GLUE_ASSETS_BUCKET}/wheels/*",
        "arn:aws:s3:::${GLUE_ASSETS_BUCKET}/scripts/*"
      ]
    },
    {
      "Sid": "S3AssetList",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::${GLUE_ASSETS_BUCKET}"
    },
    {
      "Sid": "GlueJobUpsert",
      "Effect": "Allow",
      "Action": [
        "glue:GetJob",
        "glue:CreateJob",
        "glue:UpdateJob",
        "glue:DeleteJob"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PassGlueRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "${GLUE_JOB_ROLE_ARN}",
      "Condition": {
        "StringEquals": {"iam:PassedToService": "glue.amazonaws.com"}
      }
    }
  ]
}
JSON
)

aws iam put-user-policy \
  --user-name "$USER_NAME" \
  --policy-name "$POLICY_NAME" \
  --policy-document "$POLICY_DOC"
echo "    applied"

echo "==> Managing access keys"
EXISTING=$(aws iam list-access-keys --user-name "$USER_NAME" --query 'AccessKeyMetadata[].AccessKeyId' --output text)
EXISTING_COUNT=$(echo "$EXISTING" | wc -w | tr -d ' ')

if [ "$ROTATE" = "1" ] && [ "$EXISTING_COUNT" -gt 0 ]; then
  for k in $EXISTING; do
    echo "    deleting old key $k"
    aws iam delete-access-key --user-name "$USER_NAME" --access-key-id "$k"
  done
  EXISTING_COUNT=0
fi

if [ "$EXISTING_COUNT" -ge 2 ]; then
  echo "!!! User already has 2 access keys. Delete one in the console or re-run with --rotate." >&2
  exit 1
fi

if [ "$EXISTING_COUNT" -ge 1 ] && [ "$ROTATE" != "1" ]; then
  echo "    user already has an access key; skipping creation (pass --rotate to replace)"
  echo "    existing: $EXISTING"
  exit 0
fi

KEY_JSON=$(aws iam create-access-key --user-name "$USER_NAME")
AKID=$(echo "$KEY_JSON" | awk -F'"' '/AccessKeyId/{print $4}')
SECRET=$(echo "$KEY_JSON" | awk -F'"' '/SecretAccessKey/{print $4}')

cat <<OUT

=== New access key for $USER_NAME ===
AWS_ACCESS_KEY_ID=$AKID
AWS_SECRET_ACCESS_KEY=$SECRET

Copy these into GitHub: Settings -> Secrets and variables -> Actions.
The secret is only shown here once.
OUT
