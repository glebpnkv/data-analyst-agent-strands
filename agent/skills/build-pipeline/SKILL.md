---
name: build-pipeline
description: Build a data pipeline as an AWS Lambda function, end-to-end without asking the user for confirmation between steps. Prototype the transform in the sandbox, deploy as Lambda via deploy_pipeline_as_lambda, then on the next turn invoke it and show a sample of the real output. Use this skill whenever the user asks for a "pipeline", "transformation", "ETL", "data prep", or any wording that implies productionising a transform — including any bronze / silver / gold request.
---

# Build-Pipeline Skill

## Purpose

Take a transformation idea and run it all the way to a working Lambda
pipeline plus a sample of its output, **without stopping mid-flow to
ask the user for confirmation**. This is the headline demo flow for
the hackathon — speed and autonomy matter.

The skill enforces three things:

1. **Bronze / silver / gold tier discipline** — every pipeline writes
   into a known prefix so the same dataset can grow more layers later.
2. **Sandbox-first prototyping** — never deploy code you haven't
   already executed against the real bytes.
3. **Auto-continuation after deploy** — the Chainlit host deploys the
   Lambda asynchronously and re-prompts you to invoke + sample. Don't
   wait for the human to type "now test it".

## When to use this skill

Activate the moment the user says anything that implies "make me a
data pipeline / transformation / ETL", whether or not they use
medallion vocabulary. Don't activate for plain EDA, plotting, or
ad-hoc queries — those don't need a Lambda.

## The bronze / silver / gold convention

We have three S3 buckets (`raw`, `processed`, `gold`). The medallion
tiers map onto **prefixes inside those buckets**, not separate buckets:

| Tier   | Bucket / prefix                    | Purpose                                                                 |
|--------|------------------------------------|-------------------------------------------------------------------------|
| Raw    | `raw://`                           | User uploads. Read-only — never write back.                             |
| Bronze | `processed://bronze/<dataset>/`    | Typed, parsed, deduped. One bronze key ↔ one raw input.                |
| Silver | `processed://silver/<dataset>/`    | Cleaned + business logic: joins, filters, derived columns, aggregates. |
| Gold   | `gold://<dataset>/`                | Analysis-ready / feature-ready. The output the user actually consumes. |

Pipeline naming follows the destination tier:

- `bronze-<dataset>` — reads from `raw`,        writes to `processed/bronze/<dataset>/`.
- `silver-<dataset>` — reads from `processed/bronze/...`, writes to `processed/silver/<dataset>/`.
- `gold-<dataset>`   — reads from `processed/silver/...` (or bronze, for simple flows), writes to `gold/<dataset>/`.

Output filenames inside each prefix should be stable (`part-000.csv`)
so re-running the pipeline overwrites the previous output instead of
piling up versioned junk. Use `.csv` for tabular output unless the
user asks for something else; the deployed Lambda has no pandas /
pyarrow, only stdlib + boto3.

If the user just says "make me a pipeline for X" without specifying a
tier, default to a single `gold-<dataset>` pipeline that goes raw →
gold in one hop. Add bronze/silver layers only if the transformation
genuinely splits cleanly across tiers, or if the user asks for them.

## Workflow

### Step 1 — Discover the source

Always start by reading what's actually there. Don't assume schema.

- `list_s3_dataset(tier="raw")` (or whichever upstream tier).
- `load_s3_into_sandbox(...)` for the candidate file.
- In the sandbox: read with pandas, inspect dtypes, head, null counts,
  cardinality of categorical columns. Cap previews at 10–20 rows.

### Step 2 — Prototype the transform in the sandbox

Build the **exact handler logic** in the sandbox first, against the
real data. This is the "scratch job" equivalent from the Glue skill —
proves the transform works before anything ships.

Critical rule: **the prototype must use stdlib + boto3 only** (no
pandas, no numpy, no pyarrow). The Lambda runtime won't have those
packages, and you're about to copy this code straight into the Lambda
handler. Using pandas in the prototype just to throw it away is a
trap — write CSV-in / CSV-out with the `csv` module from the start.

Recipe:

```python
import csv, io
# Pretend the sandbox just downloaded the source file via
# load_s3_into_sandbox(tier="raw", key="uploads/abc123/sales.csv")
with open("sales.csv", newline="") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

# ... your transform here ...
out = transform(rows)

# Write a sample so you can eyeball it before queuing the deploy
with open("sample_output.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=out[0].keys())
    writer.writeheader()
    writer.writerows(out[:20])
```

If the prototype throws, fix it in the sandbox and re-run. Do not
queue a deploy until the sandbox transform produces sensible output.

### Step 3 — Package as a Lambda handler

Wrap the validated transform in a `handler(event, context)` function.
The handler reads input from S3, transforms, writes output to S3.
Bucket names come from env vars baked in at deploy time.

```python
import csv, io, os, boto3

s3 = boto3.client("s3")

BUCKET_RAW       = os.environ["BUCKET_RAW"]
BUCKET_PROCESSED = os.environ["BUCKET_PROCESSED"]
BUCKET_GOLD      = os.environ["BUCKET_GOLD"]

# Default input/output keys baked in. The event dict can override
# either of them so the same Lambda is reusable for related files.
DEFAULT_INPUT_KEY  = "uploads/abc123/sales.csv"
DEFAULT_OUTPUT_KEY = "sales/part-000.csv"

def transform(rows):
    # ... copy the sandbox-validated logic here verbatim ...
    return rows

def handler(event, context):
    in_bucket  = event.get("input_bucket",  BUCKET_RAW)
    in_key     = event.get("input_key",     DEFAULT_INPUT_KEY)
    out_bucket = event.get("output_bucket", BUCKET_GOLD)
    out_key    = event.get("output_key",    DEFAULT_OUTPUT_KEY)

    body = s3.get_object(Bucket=in_bucket, Key=in_key)["Body"].read().decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(body)))

    out = transform(rows)

    out_buf = io.StringIO()
    writer = csv.DictWriter(out_buf, fieldnames=out[0].keys())
    writer.writeheader()
    writer.writerows(out)
    s3.put_object(Bucket=out_bucket, Key=out_key, Body=out_buf.getvalue().encode("utf-8"))

    return {
        "rows_in":  len(rows),
        "rows_out": len(out),
        "output_s3_uri": f"s3://{out_bucket}/{out_key}",
    }
```

Hard rules:

- The handler MUST be self-contained. No imports beyond stdlib + boto3.
- The handler MUST resolve buckets from env vars (`BUCKET_RAW`,
  `BUCKET_PROCESSED`, `BUCKET_GOLD`) — `deploy_pipeline_as_lambda`
  bakes these in.
- The handler MUST accept event-overridable input/output keys, even if
  it has sensible defaults.
- The handler SHOULD return a small JSON-serialisable summary
  (row counts, output URI). That's what `invoke_pipeline` echoes back.

### Step 4 — Queue the deploy and END the turn

Call `deploy_pipeline_as_lambda(name=<tier>-<dataset>, code=<the
handler above as a string>, description=...)`. It returns
`status: queued` — that's expected. The agent runtime CANNOT call
`lambda:CreateFunction` (workshop boundary), so the Chainlit host
does the actual deploy after your turn ends.

In your final message of this turn:

- Say what was queued and what tier it lands in.
- Mention you'll invoke it and show a sample once it's live.
- Then **end the turn**. Don't try to `invoke_pipeline` in the same
  turn — the Lambda doesn't exist yet, and the call will fail with
  `ResourceNotFoundException`.

### Step 5 — Auto-continuation: invoke + sample

The Chainlit host deploys the Lambda within a few seconds, then
auto-prompts you with a message that starts with
`[system] Pipeline <name> deployed`. When you see that:

1. Call `invoke_pipeline(name=<name>)` (no payload — the handler's
   defaults cover the demo path).
2. If the response shows a non-200 `status_code`, surface the
   `function_error` and `log_tail` so the user can see the failure.
   Then either fix and re-deploy or stop and ask. **Don't silently
   pretend it succeeded.**
3. On success, `load_s3_into_sandbox(tier=<output_tier>, key=<output_key>)`
   to pull the actual output file the Lambda just wrote.
4. Read it in the sandbox with pandas, then either:
   - print `df.head(10).to_markdown(index=False)` and include the
     markdown table in your reply (best for narrow tabular output), OR
   - build a `plotly.graph_objects.Table` of the head and surface it
     via `display_plotly` (best when the table is wide / styled), OR
   - if a chart is more illuminating than a table, build the chart
     and surface it via `display_plotly`.
   Whichever you pick, the user MUST see real bytes from the deployed
   pipeline, not just a "✓ ran successfully" message.
5. End the turn with a one-paragraph summary: tier, output URI, row
   counts, anything notable.

Do not ask the user "should I invoke it now?" — the auto-prompt IS
their go-ahead.

## Anti-patterns

- ❌ Deploying without a sandbox prototype run.
- ❌ Using pandas / numpy / pyarrow in the handler.
- ❌ Hard-coding bucket names instead of reading env vars.
- ❌ Trying to `invoke_pipeline` in the same turn as `deploy_pipeline_as_lambda`.
- ❌ After auto-continuation, just saying "the pipeline ran ✓" without
  loading the output file and showing a real sample.
- ❌ Writing pipeline output back into the `raw` tier (raw is
  read-only by convention).
- ❌ Stalling between steps to ask "do you want me to continue?".

## When the user asks for multiple tiers

If the user explicitly asks for a multi-stage pipeline (e.g. "build
me a bronze, silver, and gold pipeline for sales"), ship them in
order, **one Lambda per tier**, each as its own pass through Steps
2–5. Each tier's auto-continuation message can chain into the next
tier's deploy in the same conversation — just keep the prototype-then-
deploy discipline for every layer.
