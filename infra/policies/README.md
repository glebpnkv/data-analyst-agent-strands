# IAM policies

JSON policy documents consumed by the CDK stacks in `infra/stacks/`.

## `data_analyst_access.json`

Granted to the `data_analyst_agent` ECS task role. Lifted verbatim from the (now-deleted) `scripts/deploy_strands_agentcore.sh` so the IAM work isn't lost in the AgentCore-to-ECS migration.

Permissions: Glue job/trigger/workflow/crawler management, Glue Data Catalog read, S3 script + artifact access, CloudWatch Logs read for diagnostics, scoped `iam:PassRole` to `glue.amazonaws.com`.

### Outstanding scope-down work

- **`iam:PassRole` is currently `Resource: "*"`.** When the CDK stack provisions the agent task role, scope this down to the specific Glue execution role ARN (was `GLUE_JOB_ROLE_ARN` in the old deploy script).
- **`S3ScriptAndArtifactAccess` is currently `Resource: "*"`.** Scope to the specific Glue scripts/temp/results buckets used by the pipeline.

### Athena extension (TODO)

The current policy is Glue-first. To support Athena query execution from the deployed agent, extend with:

- `athena:StartQueryExecution`, `athena:GetQueryExecution`, `athena:GetQueryResults`, `athena:StopQueryExecution`
- `glue:GetDatabase`/`GetTable`/`GetPartition` (already covered above)
- S3 access scoped to the **Athena results bucket(s)** and the **target data buckets/workgroups**

This note replaces the inline comment at lines 121–124 of the deleted deploy script.
