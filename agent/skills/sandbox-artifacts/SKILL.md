---
name: sandbox-artifacts
description: How to produce analysis artifacts (charts, tables, images) inside the AgentCore code interpreter sandbox and hand them off to the display_plotly / display_image tools without paying per-token cost for the bytes. Activate this skill any time you intend to show the user a chart, image, or table — it is the **only** safe way.
---

# Sandbox Artifacts Skill

## Purpose

The agent has two tools that surface inline UI in the chat:

- `display_plotly` — renders an interactive Plotly chart (or a styled
  `plotly.graph_objects.Table` when you want a real tabular widget).
- `display_image` — renders an image (matplotlib, plotly static, PNG/JPEG/SVG).

Both accept a **sandbox file path**. The tool reads and parses the
file in the agent service process — bytes never enter the LLM context.

If you instead pass the data *inline* (a giant CSV string, a Plotly
figure JSON, or a base64-encoded PNG) you force the model to emit every
byte as output tokens. This is slow, expensive, and a frequent cause of
conversation hangs. **Always go through a sandbox file.**

There is no `display_dataframe` tool on this branch. To show tabular
output:

- For small / narrow tables: print `df.head(10).to_markdown(index=False)`
  in the sandbox and embed the markdown in your reply.
- For larger / styled tables: build a `plotly.graph_objects.Table`,
  write it to JSON, and surface via `display_plotly` (recipe below).

## When to use this skill

Activate this skill the moment you decide to show the user a chart,
image, or table. It applies in addition to whatever workflow skill is
already active (e.g. `build-pipeline`).

## Required directory layout

Write artifacts under `tmp/analysis_outputs/`, separated by kind. The
sandbox is ephemeral, so cleanup is unnecessary.

```
tmp/analysis_outputs/
├── plotly/        # Plotly figure JSON files (charts and Table widgets)
└── images/        # PNG / JPEG / SVG image files
```

### CRITICAL path rules — read these before writing any file

The single most common failure here is the LLM picking the wrong
path convention, watching the write silently fail, and then handing
the `display_*` tool a path that doesn't exist (404). To avoid that:

1. **NEVER use `~` in any path.** Python's `open()` does NOT expand
   `~` to a home directory. Writing `fig.write_json("~/foo.json")`
   tries to create a literal `~` subdirectory — which doesn't exist,
   so the call raises `FileNotFoundError` and you don't notice. Same
   for `pd.read_csv("~/...")`, `plt.savefig("~/...")`, etc. **Always
   use a relative path starting with `tmp/analysis_outputs/...`.**

2. **NEVER use absolute paths** like `/workspace/...` or `/tmp/...`.
   The recipes below use workspace-relative paths and the sandbox is
   already running with `/workspace` as its current working
   directory.

3. **ALWAYS `os.makedirs(..., exist_ok=True)` first.** Plotly's
   `write_json`, matplotlib's `savefig`, and `pandas.to_csv` all
   refuse to create parent directories — they raise
   `FileNotFoundError` if the directory tree isn't there. The skill's
   one-time-per-session mkdir block (just below) covers all three
   subdirectories at once.

4. **Use the EXACT same path string in `write_*` and `display_*`.**
   Don't normalize, don't change separators, don't add or remove
   leading dots. Copy-paste the literal string.

Create the parent directories once per session if they don't exist:

```python
import os
os.makedirs("tmp/analysis_outputs/plotly", exist_ok=True)
os.makedirs("tmp/analysis_outputs/images", exist_ok=True)
```

## File naming

Use a short, descriptive name. If you may produce more than one artifact
of the same kind in a single turn, append a small disambiguator (a
counter or short uuid) — never a timestamp, the user doesn't see it:

```
tmp/analysis_outputs/plotly/orders_by_day.json
tmp/analysis_outputs/plotly/orders_by_day_log_scale.json
tmp/analysis_outputs/plotly/orders_table.json
tmp/analysis_outputs/images/orders_heatmap.png
```

## Recipes

### Tables (markdown — preferred for narrow output)

For small tables (≤10 rows, ≤6-ish columns), markdown is the cheapest
path: it embeds straight into your assistant reply with no extra tool
call.

```python
import pandas as pd
df = pd.DataFrame(...)  # or read an existing sandbox CSV
print(df.head(10).to_markdown(index=False))
```

Copy the printed markdown into your reply. Done.

### Tables (Plotly — for wide / styled output)

When markdown looks ugly (wide columns, lots of rows, or you want
alignment / colour), build a `plotly.graph_objects.Table` and surface
it as a regular Plotly chart:

```python
import plotly.graph_objects as go
fig = go.Figure(
    data=[go.Table(
        header=dict(values=list(df.columns)),
        cells=dict(values=[df[c] for c in df.columns]),
    )]
)
fig.write_json("tmp/analysis_outputs/plotly/results_table.json")
```

```
display_plotly("tmp/analysis_outputs/plotly/results_table.json", caption="Daily orders by region")
```

If your dataframe has more than a few hundred rows, sample or
aggregate first — a giant Table widget is awkward to read. Save the
full CSV to S3 via `save_sandbox_to_s3` if the user might want it.

### Plotly charts

Build the figure in the sandbox, persist with `write_json`, then hand
off the path:

```python
import plotly.express as px
fig = px.bar(df, x="day", y="orders")
fig.write_json("tmp/analysis_outputs/plotly/orders_by_day.json")
```

```
display_plotly("tmp/analysis_outputs/plotly/orders_by_day.json", caption="Daily orders")
```

The figure JSON file may be large (tens of KB to a few MB) — that's
fine, the file lives in the sandbox; only the path enters tool args.

### Images (matplotlib)

```python
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(8, 5))
ax.imshow(...)
fig.savefig("tmp/analysis_outputs/images/heatmap.png", dpi=150, bbox_inches="tight")
plt.close(fig)
```

```
display_image("tmp/analysis_outputs/images/heatmap.png", caption="Order heatmap")
```

The MIME type is sniffed from the file extension (`.png`, `.jpg`,
`.jpeg`, `.gif`, `.svg`, `.webp`). Use a sensible extension and you
don't need to think about it.

### Images (plotly static export)

If you specifically need a static (non-interactive) Plotly chart:

```python
fig.write_image("tmp/analysis_outputs/images/chart.png", scale=2)
```

Otherwise prefer `display_plotly` — interactive is almost always better.

## Hard rules

1. **Never** pass the data inline to `display_*`. Always write to a
   sandbox file first and pass the path.
2. **Never** base64-encode an image inside the sandbox to feed the
   result back to a tool argument. The `display_image` tool reads the
   file itself.
3. **Never** chain through prose: don't dump a 1,000-row CSV into the
   chat as text and then describe it. Aggregate or sample, render via
   markdown table or `display_plotly` (Table or chart), then summarise.
4. **Cap inline rendering by sampling, not by truncating prose.** If
   the result has 50,000 rows, save the full CSV (e.g. via
   `save_sandbox_to_s3`) but pass an aggregated/sampled view to
   markdown / `display_plotly`. The user can still ask follow-up
   questions over the full file.
5. **One artifact per file.** Don't pack multiple charts into a single
   PNG just to save a `display_*` call.

## Why this matters

Passing N bytes inline through a tool argument costs roughly N
output tokens at the model and N input tokens on the next turn (when
the tool result echoes back into context). For a 1MB Plotly figure
JSON that's ~250k tokens each way — minutes of latency, dollars of
inference cost, and frequent context-window overflows. Sandbox files
cost ~50 tokens of path string regardless of size.
