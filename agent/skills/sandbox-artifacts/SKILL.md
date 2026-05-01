---
name: sandbox-artifacts
description: How to produce analysis artifacts (charts, tables, images) inside the AgentCore code interpreter sandbox and hand them off to the display_dataframe / display_plotly / display_image tools without paying per-token cost for the bytes. Activate this skill any time you intend to show the user a table, chart, or image — it is the **only** safe way.
---

# Sandbox Artifacts Skill

## Purpose

The agent has three tools that surface inline UI in the chat:

- `display_dataframe` — renders a table
- `display_plotly` — renders an interactive Plotly chart
- `display_image` — renders an image (matplotlib, plotly static, PNG/JPEG/SVG)

All three accept a **sandbox file path**. The tool reads and parses the
file in the agent service process — bytes never enter the LLM context.

If you instead pass the data *inline* (a giant CSV string, a Plotly
figure JSON, or a base64-encoded PNG) you force the model to emit every
byte as output tokens. This is slow, expensive, and a frequent cause of
conversation hangs. **Always go through a sandbox file.**

## When to use this skill

Activate this skill the moment you decide to show the user a chart,
table, or image. It applies in addition to whatever workflow skill is
already active (Athena query execution, Glue diagnostics, etc.).

## Required directory layout

Write artifacts under `tmp/analysis_outputs/`, separated by kind. The
sandbox is ephemeral, so cleanup is unnecessary.

```
tmp/analysis_outputs/
├── dataframes/    # CSV (preferred) or JSON-records files
├── plotly/        # Plotly figure JSON files
└── images/        # PNG / JPEG / SVG image files
```

Create the parent directory once per session if it doesn't exist:

```python
import os
os.makedirs("tmp/analysis_outputs/dataframes", exist_ok=True)
os.makedirs("tmp/analysis_outputs/plotly", exist_ok=True)
os.makedirs("tmp/analysis_outputs/images", exist_ok=True)
```

## File naming

Use a short, descriptive name. If you may produce more than one artifact
of the same kind in a single turn, append a small disambiguator (a
counter or short uuid) — never a timestamp, the user doesn't see it:

```
tmp/analysis_outputs/dataframes/orders_by_day.csv
tmp/analysis_outputs/plotly/orders_by_day.json
tmp/analysis_outputs/plotly/orders_by_day_log_scale.json
tmp/analysis_outputs/images/orders_heatmap.png
```

## Recipes

### DataFrames

Prefer **CSV** for tabular output — it's the smallest text format and
the same format `athena_query_to_ci_csv` already produces, so you can
reuse a CSV that's already in the sandbox without rewriting it.

```python
import pandas as pd
df = pd.DataFrame(...)  # or read an existing sandbox CSV
df.to_csv("tmp/analysis_outputs/dataframes/results.csv", index=False)
```

Then call:
```
display_dataframe("tmp/analysis_outputs/dataframes/results.csv", title="Daily orders by region")
```

JSON records (`[{"col": 1}, ...]`) also work — use `.json` extension.
The first row of a CSV is treated as the header.

If your dataframe has more than ~1000 rows, the tool truncates and
flags it via a `truncated` indicator in the rendered table. For larger
results, sample or aggregate first.

### Plotly charts

Build the figure in the sandbox, persist with `write_json`, then hand
off the path:

```python
import plotly.express as px
fig = px.bar(df, x="day", y="orders")
fig.write_json("tmp/analysis_outputs/plotly/orders_by_day.json")
```

```
display_plotly("tmp/analysis_outputs/plotly/orders_by_day.json", title="Daily orders")
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
display_image("tmp/analysis_outputs/images/heatmap.png", title="Order heatmap", mime="image/png")
```

Mime can be `image/png`, `image/jpeg`, or `image/svg+xml`. The
default is `image/png`.

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
   chat as text and then describe it. Save it, render with
   `display_dataframe`, and then optionally summarize in prose.
4. **Cap inline rendering by sampling, not by truncating prose.** If
   the result has 50,000 rows, write the full CSV but pass an
   aggregated/sampled CSV to `display_dataframe`. The user can still
   ask follow-up questions over the full file.
5. **One artifact per file.** Don't pack multiple charts into a single
   PNG just to save a `display_*` call.

## Why this matters

Passing N bytes inline through a tool argument costs roughly N
output tokens at the model and N input tokens on the next turn (when
the tool result echoes back into context). For a 1MB Plotly figure
JSON that's ~250k tokens each way — minutes of latency, dollars of
inference cost, and frequent context-window overflows. Sandbox files
cost ~50 tokens of path string regardless of size.
