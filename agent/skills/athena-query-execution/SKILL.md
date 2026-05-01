---
name: athena-query-execution
description: Create and run Athena SQL pipelines over existing Athena/Glue tables. Use this skill when the transformation is cleanly expressible in SQL and should be executed by Athena, optionally on a schedule.
---

# Athena Query Execution Skill

## Purpose

Use this skill to build a **SQL-first Athena pipeline** over data that is already queryable in Athena. The default execution path is:

**Athena workgroup -> Athena SQL -> EventBridge Scheduler**

Use Glue crawlers only if new data must first be registered in the catalog.

## When to Use This Skill

Use this skill when all of the following are true:

- the transformation can be expressed cleanly in Athena SQL;
- the source data is already in Athena / Glue Data Catalog, or can be made visible with a crawler;
- the output should be a derived Athena table or an append-only transformed table;
- the job should be serverless SQL, not Python.

Do **not** use this skill when:
- the logic requires Python or row-wise processing;
- the task needs a Glue Python Shell job;
- the pipeline needs mutable update semantics as a normal pattern.

## Instructions for Execution

1. **Use the correct tools**
   - Use `athena_manage_aws_athena_databases_and_tables` to inspect source and target tables.
   - Use `athena_manage_aws_athena_workgroups` to create or reuse a dedicated Athena workgroup.
   - Use `athena_manage_aws_athena_query_executions` to test and run SQL.
   - Use `athena_manage_aws_athena_named_queries` to save the recurring SQL.
   - Use `awsapi_call_aws` to create the Scheduler execution role and the EventBridge Scheduler schedule.
   - Use `athena_manage_aws_glue_crawlers` only if new data is not yet visible in Athena.
   - Do **not** use Glue jobs, Glue triggers, or Glue workflows to execute Athena SQL.

2. **Inspect the source first**
   - Confirm the source table exists and is queryable.
   - Confirm the columns needed for the transform.
   - Confirm there is a safe incremental key such as a timestamp, partition column, or batch identifier.
   - If no safe incremental key exists, say so clearly and do not invent one.

3. **Set up the workgroup**
   - Create or reuse a dedicated Athena workgroup for the pipeline.
   - Prefer workgroup-level query result configuration over per-query result configuration.
   - Use the intended workgroup for all pipeline queries, not `primary` unless the user explicitly wants that.

4. **Handle source registration only if needed**
   - If the source table already sees new data, skip Glue entirely.
   - If new partitions/files are not visible yet, create or update a Glue crawler for the source S3 location.
   - Prefer incremental crawler behavior when the goal is to add newly arrived partitions only.
   - Treat the crawler as metadata registration only, not as the SQL execution engine.

5. **Validate the SQL with a plain SELECT**
   - First run a `SELECT` that shows the transformation logic on a small sample.
   - For a sine-transform example, validate expressions like `sin(x1)`, `sin(x2)`, and so on before writing output.

6. **Create the derived table**
   - Use **CTAS** for the initial materialization of the target table.
   - Prefer columnar output such as Parquet.
   - Report the final `database.table`, workgroup, and S3 output location.

7. **Create the recurring SQL**
   - Save the recurring transform as a named query.
   - Use **INSERT INTO ... SELECT ...** for append-style continuation only.
   - Filter the recurring query to only process new data based on the confirmed incremental key.

8. **Create the schedule**
   - If `SCHEDULER_ATHENA_EXEC_ROLE_ARN` is already configured in runtime context, **always reuse that role ARN** for Scheduler target `RoleArn`.
   - When `SCHEDULER_ATHENA_EXEC_ROLE_ARN` is configured, do **not** create a new Scheduler execution role.
   - Only create a new Scheduler execution role when no preconfigured role ARN exists (or when the user explicitly asks for a new role).
   - For any newly created Scheduler execution role trusted by `scheduler.amazonaws.com`, include at minimum:
     - Athena: `athena:StartQueryExecution`, `athena:GetDataCatalog`
     - Glue catalog read: `glue:GetDatabase`, `glue:GetDatabases`, `glue:GetTable`, `glue:GetTables`, `glue:GetPartition`, `glue:GetPartitions`
     - S3 source/results access required by the query runtime
   - Use `awsapi_call_aws` to create a schedule whose universal target is:
     - `arn:aws:scheduler:::aws-sdk:athena:startQueryExecution`
   - The schedule input must include:
     - `QueryString`
     - `QueryExecutionContext.Database`
     - `WorkGroup`
   - State the schedule cadence and timezone explicitly.

9. **Test before reporting success**
   - Run the CTAS or INSERT query once before claiming success.
   - Poll Athena query status until terminal state.
   - Report success only if the Athena query finishes with `SUCCEEDED`.
   - If the query or schedule creation fails, report the actual failing step clearly.

10. **Use code interpreter only for analysis**
   - Use `athena_query_to_ci_csv` before any code-interpreter analysis of Athena results.
   - Never manually transcribe table rows into Python.

## Best Practices

- Prefer Athena over Glue Python Shell when the transformation is naturally SQL.
- Use **CTAS** once, then **INSERT INTO** for recurring appends.
- Keep scheduling outside Athena SQL itself: use EventBridge Scheduler.
- Use Glue crawlers only for source registration when needed.
- Return the exact workgroup, table name, schedule name, and S3 location.
