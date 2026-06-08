"""Agent eval foundation.

Sub-modules:
    checks/    deterministic checks (text_contains, schema_grounding,
               result_set). Pure functions; no network. Composed by
               run.py per-golden according to what each golden declares.
    runners/   client adapters that invoke an agent under test and
               normalise the response into a runner-agnostic shape.
               Today: run_agent.py (POST to deployed /v1/chat).
    goldens/   the eval dataset. JSON or Python files, one per slice.
               Loaded by run.py at start-up; PR'd like tests.
    metrics.py LLM-as-judge wrappers (BedrockJudge). Scaffolding only
               in M2; populated with actual metrics in M3.
    reports/   gitignored. JSON reports written per run, one file
               per `uv run python -m eval.run` invocation.
    run.py     orchestrator. Loads goldens, runs each through the
               configured runner, applies checks, writes a report.
"""
