# Eval stack — decisions

One-page summary of the stack chosen for agent evaluation and production tracing on the `data-analyst-agent-strands` project. Full rationale, framework comparison, methodology, dev/CI walkthrough, and workplan are in [`agent-evals-research.md`](./agent-evals-research.md).

## Chosen stack

| Layer | Choice | Rationale (short) |
|---|---|---|
| Production tracing | **Arize Phoenix**, self-hosted on ECS Ec2 cluster | OpenInference-native; already in local docker-compose; uses existing RDS Postgres + ECS cluster; no outbound SaaS. |
| Offline / CI eval metrics | **DeepEval** (Python library) | Pytest-native; deepest OSS agent metric catalogue (ToolCorrectness, TaskCompletion, GEval, PlanAdherence); Bedrock judge via LiteLLM. |
| Judge model — default | Claude 3.5 Haiku, EU cross-region profile (`eu.anthropic.claude-3-5-haiku-20241022-v1:0`) | Fast, cheap; correlates well with humans on the metrics in scope. |
| Judge model — heavy | Claude Sonnet 4.5, EU cross-region profile (`eu.anthropic.claude-sonnet-4-5-20250929-v1:0`) | Used only for TaskCompletion + PlanAdherence where Haiku is empirically weaker. |
| Dataset format | JSON / Python files under `eval/goldens/`, git-versioned, synced to Phoenix dataset `golden-v1` (versioned in Phoenix automatically) | Goldens travel with code; Phoenix versioning gives a queryable history. |
| Regression gate | Per-metric `pr_score - main_baseline` delta thresholds in CI; PR-body acknowledgement override; critical-tagged cases bypass average | See `agent-evals-research.md` §5.5 for the threshold table. |
| Online evaluation | EventBridge-scheduled Lambda sampling 10% of recent Phoenix traces every 15 min | Closes the offline/online loop. |

## Rejected options (one-line)

| Option | Why rejected |
|---|---|
| AWS Bedrock Model Evaluation Jobs | Single-turn, prompt-only — not agent-aware. |
| SageMaker Clarify / FMEval | Same shape as Bedrock Model Eval; requires SageMaker surface for no agent-specific gain. |
| Langfuse self-host (v3) | Requires 5 backing services including ClickHouse on Fargate — operationally heavy for our scale. |
| LangSmith | SaaS US-only or EKS-Enterprise self-host; conflicts with restricted-egress posture. |
| Braintrust | Hybrid data-plane not available in eu-central-1. |
| W&B Weave | Best comparison UX, but self-host requires a paid W&B license. |
| Promptfoo as alternative to DeepEval | Credible; DeepEval wins on deeper agent-metric catalogue and first-party Strands integration. |
| Ragas as primary library | Strong for RAG; not the right primary tool for a non-RAG SQL-emitting agent today. Reserve for the M6 RAG stretch. |

## Constraints baked into the choice

- **Region:** eu-central-1 (Frankfurt). Bedrock cross-region inference profiles available; AgentCore Evaluations is *not yet confirmed* in this region.
- **Egress posture:** target stays portable to environments with restricted outbound SaaS. The chosen stack runs entirely in-VPC and judges via Bedrock — no external dependencies.
- **Operational footprint:** single engineer must be able to own and operate it. Two services (one new — Phoenix; one extended — pyproject deps for DeepEval) is the budget.
- **Cost target:** ~$110/mo all-in for the dev stack (~$70–90 Phoenix infra + ~$20 judge tokens). Production cost driven by Bedrock token cost on the agent itself, not by the eval infra.

## Decisions deferred (intentionally)

- **AgentCore Evaluations region/SCP availability** — gates M6 option B. Not investigated yet; revisit if/when a "best AWS-native" demo becomes a priority.
- **Judge bake-off: Haiku 3.5 vs Nova Pro** — Haiku is the safe default. Worth a small bake-off in M3 once judges are wired; Nova Pro is ~2× cheaper and worth the comparison if results correlate.
- **Hermetic mocking strategy: `FakeMCPClient` vs `vcrpy` cassettes** — recommend `FakeMCPClient` for the tool-call shape; decision deferred to M3.
- **Trace retention beyond 14 days** — default 14d. Tune in M1 if needed.
