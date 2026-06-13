# Goldens

Hand-authored eval cases. JSON files (and the occasional `.py` for cases that need code-defined assertions).

## Policy

- **No real customer data.** Every reference table must be one of the eval databases populated by `scripts/upload_eval_data.py`:
  - `sample_database.iris` (smoke / count cases)
  - `eval_taxi.taxi_trips` (aggregation / time-window cases)
  - `eval_sales.{customers, orders, products}` (join / synthetic-business cases)
- New goldens land via PR. Reviewers check that the inputs and expected outputs don't contain any real PII or proprietary data.
- One file per capability slice. Don't dump everything into one giant file.

## Shape

```json
{
  "id": "unique-kebab-case-id",
  "tags": ["athena", "smoke"],
  "input": "Natural-language question the user might ask",
  "expected_answer_contains": ["150"],
  "context": ["optional: notes for reviewers, not sent to the agent"]
}
```

Optional fields (used by M3 checks):

- `expected_tools`: list of tool calls the agent should make.
- `expected_result_set`: shape + values of the underlying query result.
- `negative_assertions`: things the agent must NOT do.
- `judge_rubric`: criteria for an LLM judge.

## Publishing to Phoenix

The goldens live in git, but the runner reads them from Phoenix. After editing or adding a golden:

```bash
export PHOENIX_ENDPOINT=http://localhost:6006   # default
uv run --group dev python scripts/upload_dataset.py --append
```

First-time upload (no existing dataset) omits `--append`. The runner (`eval/run.py`) then reads `data-analyst-goldens` from Phoenix and posts each experiment run there.

## Sourcing

- **InfiAgent-DABench-style** cases: open-source data analysis benchmark, constraint-based ground truth. Cite the source in the `context` field.
- **BIRD-SQL-style** cases: execution accuracy on result sets. Same.
- **Bespoke** cases: glue lifecycle, refusals, gotchas.
