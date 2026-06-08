# Agent eval framework

A small framework that runs the deployed data-analyst agent over a set
of hand-authored goldens, scores each run with two independent
evaluators (one deterministic, one LLM-judge), and lands the result in
Phoenix as a comparable experiment.

## What's deployed and required

The eval runner is a **local-only orchestrator**: nothing about it has
to run in CI. To execute it from your laptop, the following must be in
place:

| Component | Where | Required for |
|---|---|---|
| Agent service (FastAPI) | ECS, internal ALB | All runs |
| Phoenix server (v17+) | ECS, internal ALB | All runs (datasets, experiments, traces) |
| SSM port-forward to agent ALB | `scripts/portforward_agent.sh` | All runs |
| SSM port-forward to Phoenix ALB | `scripts/portforward_phoenix.sh` | All runs |
| Service auth secret | Secrets Manager → exported as env | All runs |
| AWS credentials with Bedrock access | Local SSO session | LLM judge only |
| Bedrock model access (eu.haiku-4-5) | Account-level model enable | LLM judge only |

The runner is opinionated about Phoenix: there is **no offline / local-
only mode**. If you need to drop Phoenix dependency entirely, run the
agent through Chainlit by hand instead.

## Set-up (first time)

```bash
# 1. Two port-forwards, each in its own terminal:
./scripts/portforward_agent.sh    # listens on :8080
./scripts/portforward_phoenix.sh  # listens on :6006

# 2. Push the goldens to Phoenix as a dataset (idempotent on name):
export PHOENIX_ENDPOINT=http://localhost:6006
uv run --group dev python scripts/upload_dataset.py

# 3. Run one experiment:
export AGENT_BASE_URL=http://localhost:8080
export AGENT_SERVICE_AUTH_SECRET=$(./scripts/print_agent_auth.sh)
export AWS_REGION=eu-central-1
uv run --group dev python -m eval.run
```

The runner prints an *Experiment URL* at the end — that's where the
scored experiment lives in the Phoenix UI, with per-case
pass/fail chips and the LLM judge's explanation for each.

## Disabling parts of the stack

### Skip the LLM judge

The deterministic `text_contains` check always runs. The Bedrock-backed
correctness judge can be skipped explicitly:

```bash
# CLI flag:
uv run --group dev python -m eval.run --no-llm-judge

# Or env var (handy for CI / scripted runs):
EVAL_NO_LLM_JUDGE=1 uv run --group dev python -m eval.run
```

When skipped, the runner makes zero Bedrock calls. Use this for:
- Offline runs / no AWS creds available
- Cheap iteration during golden authoring
- Any context where you don't want a $0.01 spend per run

### Disable the whole framework

Don't invoke `eval.run`. Nothing here installs hooks or middleware in
the agent — the agent doesn't know the eval runner exists. The only
deployed component the eval framework adds to the agent stack is
**Phoenix** itself (used for traces in production, not just evals);
removing that is a separate decision in `infra/stacks/compute.py`
(see `phoenix_desired_count`).

## Cost

Per full run against the current 9-golden dataset, on a t3.medium-sized
ECS cluster with Bedrock `eu.anthropic.claude-haiku-4-5`:

| Component | Per case | Per 9-case run |
|---|---|---|
| Agent — Bedrock (Sonnet 4.6) | ~$0.02 | ~$0.18 |
| Judge — Bedrock (Haiku 4.5) | ~$0.001 | ~$0.009 |
| Athena query bytes scanned | <$0.001 | <$0.005 |
| Phoenix infra | flat | $0 (already paid for) |
| **Total** | **~$0.022** | **~$0.20** |

Skipping the LLM judge cuts the run to ~$0.19; skipping is mostly for
iteration speed and CI sanity, not cost.

## Authoring goldens

See `eval/goldens/README.md` for the per-golden shape and the policy on
sourcing. New goldens go into the repo via PR; `scripts/upload_dataset.py
--append` pushes them up as a new Phoenix dataset version. Phoenix
preserves all prior versions, so re-runs against historical datasets
are always possible.

**Lesson worth pinning:** the LLM judge reads `context` from each
golden's metadata as ground truth. A stale or wrong note there will
cause the judge to flag a *correct* agent answer as incorrect. When
authoring goldens, verify the reviewer notes against the actual loaded
data, not against documentation.

## Future hooks

- **CI integration** — wire `eval.run` into a GitHub Actions job that
  runs against PR commits via an ECS one-off task; gate the build on
  exit code. Requires more "showcase" goldens (multi-step analyses,
  Glue job authoring, chart generation) before the gate is meaningful.
- **Tiered severity** — add a `severity: blocking|advisory` field to
  goldens so CI can distinguish "this regression must block the PR"
  from "warn me about this". ~20 lines in `eval/run.py`.
- **Tool-call evaluator** — Phoenix ships `ToolSelectionEvaluator`;
  wire it the same way the correctness judge is wired in
  `_build_correctness_judge`. Useful once the toolset expands beyond
  Glue/Athena.
