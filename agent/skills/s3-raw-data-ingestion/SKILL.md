---
name: s3-raw-data-ingestion
description: Discover and process raw data directly from an S3 bucket/prefix without requiring an Athena/Glue Catalog entry for the source. Use this skill when the user provides a raw `s3://...` URI as the input instead of an Athena database/table.
---

# Raw S3 Data Ingestion Skill

## Purpose

Some pipelines start from raw files in S3 that are not (yet) registered in the Glue Data Catalog — dumps from upstream systems, third-party feeds, freshly landed files before a crawler has run. This skill handles the discovery-from-S3 flow as an alternative to the Athena-based discovery used elsewhere in this agent.

The downstream job-creation flow (scratch → wheel → PR) is unchanged — see the `glue-python-shell-job` skill. This skill only changes HOW the source is discovered and read.

## When to use

Use this skill if ANY of the following is true:
- the user provides an `s3://...` URI as the input source,
- `RAW_DATA_BUCKET_S3_URI` is set and the user has not provided an Athena table,
- the source data is not yet registered in Athena/Glue Catalog.

Do NOT use this skill when an Athena database/table is available — prefer the Athena flow in that case because it gives the agent schema for free.

If both an Athena source and a raw S3 URI are available, ask the user which to use. Do not silently pick one.

## Discovery steps

1. **Resolve the source URI**
   - Priority: user-provided URI > `RAW_DATA_BUCKET_S3_URI`.
   - Parse into `(bucket, prefix)`. Normalise: ensure prefix ends with `/` if it's a directory-style prefix.

2. **List the prefix**
   - Use `call_aws` with `s3api list-objects-v2 --bucket <b> --prefix <p> --max-items 200` (limit to keep output small).
   - If the listing is paginated and looks much larger, take a representative sample — do not download the full listing.
   - Do NOT use `athena_list_s3_buckets` for this step; that's for Glue asset staging, not raw-data discovery.

3. **Detect partitioning**
   - Look for Hive-style prefixes in the keys: `year=YYYY/month=MM/…` or `dt=YYYY-MM-DD/…`.
   - Record the partition keys observed and whether they appear consistent across the sampled keys.
   - If no Hive-style pattern is present, note that the dataset is flat and state this in the plan.

4. **Identify file format**
   - Prefer extensions: `.parquet`, `.csv`, `.json`, `.jsonl`, `.tsv`, `.gz`, `.snappy.parquet`, etc.
   - If extensions are absent or misleading, fetch the first ~4 KB of a sample object via `call_aws s3api get-object --range bytes=0-4096 ...` and inspect magic bytes (Parquet starts with `PAR1`, Snappy framing, etc.).
   - Assume UTF-8 for text formats unless evidence says otherwise.

5. **Sample one object per partition (or one for flat datasets)**
   - For CSV/JSONL: fetch the first ~64 KB and read headers / first few rows via code interpreter.
   - For Parquet: fetch the full file only if small (<10 MB); otherwise fetch ~128 KB and parse the footer in code interpreter.
   - Do NOT download gigabyte-scale files. If sample size is ambiguous, ask the user.

6. **Infer schema**
   - Use code interpreter (`athena_query_to_ci_csv` is NOT applicable here — the data isn't in Athena yet).
   - Produce a plain schema description: column names, inferred types, whether nullable. Show it to the user before writing the transform.

7. **Confirm with the user**
   - Report: bucket, prefix, partition keys (if any), file format, record count estimate (if cheap), inferred schema.
   - Ask the user to confirm before generating the Glue job logic.

## Processing pattern

Same philosophy as the Athena flow:
- Read from the raw S3 source.
- Transform / aggregate / feature-engineer in plain Python.
- Write a fresh output dataset to a new S3 location.
- Snapshot-in, snapshot-out. No incremental state.

When reading inside the Glue job:
- For CSV/JSON: standard library (`csv`, `json`) is fine.
- For Parquet: `pyarrow` is available in Glue Python Shell 3.9 runtime; import directly.
- For gzipped inputs: use the `gzip` module; do not add extra dependencies.

## Output registration

After the job run succeeds, register the OUTPUT dataset in Athena/Glue Catalog directly (same as the `glue-python-shell-job` skill's Phase B step). The raw S3 source does not need to be registered — leave that to a crawler or a separate task if the user asks.

## What to avoid

- Do not attempt to register the raw source as an Athena table unless the user explicitly asks. Schema inference from a sample is too brittle for that.
- Do not download the full source dataset during discovery. Sample aggressively.
- Do not use this skill when an Athena table is already available for the same data — Athena gives schema for free.
- Do not mix raw-S3 and Athena sources in the same job without explicit user direction.
