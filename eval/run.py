"""Entry point: `uv run --group dev python -m eval.run`.

Runs the deployed agent over the Phoenix-side `data-analyst-goldens`
dataset as a Phoenix *experiment*. Each example is one /v1/chat call.
Each declared check becomes a Phoenix evaluator → annotation. The
experiment is stored on the Phoenix server; the UI shows pass/fail per
example and lets you diff against previous experiments (PR vs main,
prompt v1 vs v0, etc.).

A local JSON report is still written under `eval/reports/` for offline
debugging — same shape as before, with the Phoenix experiment URL
embedded.

Prerequisites (operator setup):
  1. Port-forward to the Phoenix ALB on localhost:6006.
  2. Port-forward to the agent ALB on localhost:8080.
  3. Upload goldens to Phoenix once via scripts/upload_dataset.py.

Env vars:
  PHOENIX_ENDPOINT              default http://localhost:6006
  PHOENIX_API_KEY               optional (only if Phoenix auth enabled)
  AGENT_BASE_URL                default http://localhost:8080
  AGENT_SERVICE_AUTH_SECRET     from Secrets Manager
                                  (DataAnalystAgent/Dev/ServiceAuthSecret)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval.checks import check_text_contains
from eval.runners import AgentRunResult, DeployedAgentClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("eval.run")

EVAL_ROOT = Path(__file__).resolve().parent
REPORTS_DIR = EVAL_ROOT / "reports"

DEFAULT_DATASET_NAME = "data-analyst-goldens"
DEFAULT_PHOENIX_ENDPOINT = "http://localhost:6006"


@dataclass
class _AgentTaskOutput:
    """Payload returned by the task callable to Phoenix per example.

    Phoenix stores this as the experiment run's `output` and shows it in
    the UI. We keep tokens/elapsed/trace_id here so the experiment row
    is self-describing and so evaluators can read trace_id later for
    span-level annotations if we want.
    """

    answer: str
    trace_id: str | None
    span_id: str | None
    session_id: str
    tool_call_count: int
    tool_error_count: int
    elapsed_seconds: float
    usage: dict[str, Any] | None
    errors: list[str]
    run_error: str | None


def main() -> int:
    args = _parse_args()

    agent_client = _agent_client_or_exit()
    phoenix_client = _phoenix_client()

    dataset = _load_dataset_or_exit(phoenix_client, args.dataset)
    log.info(
        "Phoenix dataset %r resolved (id=%s, %d example(s))",
        args.dataset,
        dataset.id,
        len(getattr(dataset, "examples", []) or []),
    )

    task = _build_task(agent_client)
    disable_judge = args.no_llm_judge or _env_truthy("EVAL_NO_LLM_JUDGE")
    evaluators = _build_evaluators(disable_judge=disable_judge)

    experiment_name = args.experiment_name or _default_experiment_name()
    experiment_metadata = {
        "git_branch": _git_branch(),
        "git_sha": _git_sha(),
        "agent_base_url": agent_client.base_url,
    }

    log.info("Running experiment %r…", experiment_name)
    ran = phoenix_client.experiments.run_experiment(
        dataset=dataset,
        task=task,
        evaluators=evaluators,
        experiment_name=experiment_name,
        experiment_description="data-analyst-agent eval run",
        experiment_metadata=experiment_metadata,
        print_summary=True,
    )

    experiment_url = phoenix_client.experiments.get_experiment_url(
        dataset_id=ran["dataset_id"],
        experiment_id=ran["experiment_id"],
    )
    log.info("Experiment URL: %s", experiment_url)

    report_path = _write_local_report(ran, experiment_url, experiment_name, experiment_metadata)
    log.info("Local debug report written to %s", report_path)

    return 0 if _all_passed(ran) else 1


# --- argparse ---------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run agent evals as a Phoenix experiment.")
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET_NAME,
        help=f"Phoenix dataset name. Default: {DEFAULT_DATASET_NAME}",
    )
    parser.add_argument(
        "--experiment-name",
        default=None,
        help="Experiment name shown in Phoenix. Defaults to a git-sha-based name.",
    )
    parser.add_argument(
        "--no-llm-judge",
        action="store_true",
        help=(
            "Skip the Bedrock-backed LLM judge. Only the deterministic "
            "text_contains check runs. Use this when running offline, "
            "when you want zero Bedrock spend, or when AWS creds aren't "
            "available. Equivalent: EVAL_NO_LLM_JUDGE=1."
        ),
    )
    return parser.parse_args()


# --- Phoenix / agent clients ------------------------------------------------


def _agent_client_or_exit() -> DeployedAgentClient:
    try:
        return DeployedAgentClient.from_env()
    except RuntimeError as e:
        log.error("Agent client setup failed: %s", e)
        sys.exit(2)


def _phoenix_client():
    from phoenix.client import Client

    endpoint = os.environ.get("PHOENIX_ENDPOINT", DEFAULT_PHOENIX_ENDPOINT)
    api_key = os.environ.get("PHOENIX_API_KEY") or None
    log.info("Phoenix endpoint: %s%s", endpoint, " (with api key)" if api_key else "")
    return Client(base_url=endpoint, api_key=api_key)


def _load_dataset_or_exit(client, name: str):
    try:
        return client.datasets.get_dataset(dataset=name)
    except Exception as e:  # noqa: BLE001
        log.error(
            "Could not load Phoenix dataset %r: %s. "
            "Did you run scripts/upload_dataset.py?",
            name,
            e,
        )
        sys.exit(2)


# --- task + evaluator construction ------------------------------------------


def _build_task(agent_client: DeployedAgentClient):
    """Phoenix-callable task: one call per dataset example.

    Phoenix passes the DatasetExample mapping; we extract the prompt,
    POST to /v1/chat, then close the session so the sandbox releases
    before the next example.
    """

    def task(example: dict[str, Any]) -> dict[str, Any]:
        prompt = example["input"]["prompt"]
        run: AgentRunResult | None = None
        run_error: str | None = None
        try:
            run = agent_client.chat(prompt)
        except Exception as e:  # noqa: BLE001
            log.exception("Agent call failed for example %s", example.get("id"))
            run_error = f"{type(e).__name__}: {e}"
            run = AgentRunResult(session_id="", answer="")
        finally:
            sid = run.session_id if run else ""
            if sid:
                agent_client.close_session(sid)

        output = _AgentTaskOutput(
            answer=run.answer,
            trace_id=run.trace_id,
            span_id=run.span_id,
            session_id=run.session_id,
            tool_call_count=run.tool_call_count,
            tool_error_count=run.tool_error_count,
            elapsed_seconds=round(run.elapsed_seconds, 2),
            usage=run.usage,
            errors=list(run.errors),
            run_error=run_error,
        )
        return asdict(output)

    return task


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _build_evaluators(*, disable_judge: bool = False) -> list:
    """Phoenix-callable evaluators.

    Two-tier setup, each producing one annotation per task run:

    1. `text_contains` — deterministic cheap gate. Hand-rolled (Phoenix's
       MatchesRegex would do the same job, but we already have our own
       and it costs zero per run).
    2. `correctness_judge` — LLM-as-judge via Phoenix's
       create_classifier. Bedrock Haiku 4.5 reads the question, agent
       answer, expected substrings, and reviewer notes from the golden,
       and classifies correct/incorrect. This is the one that catches
       refusal/gotcha goldens where text_contains is too permissive.

    The judge is skipped (with a warning) if AWS creds aren't available
    or Bedrock returns an error, so the deterministic gate still runs
    in offline / CI-without-Bedrock contexts.
    """

    def text_contains(output: dict[str, Any], expected: dict[str, Any], **_: Any):
        if output.get("run_error"):
            return (0.0, "fail", f"agent run errored: {output['run_error']}")
        expected_substrings = list(expected.get("expected_substrings") or [])
        verdict = check_text_contains(output.get("answer", ""), expected_substrings)
        label = "pass" if verdict.passed else "fail"
        explanation = (
            f"matched={verdict.matched}; missing={verdict.missing}"
            if expected_substrings
            else "no expected_substrings declared — vacuous pass"
        )
        return (verdict.score, label, explanation)

    evaluators: list = [text_contains]
    if disable_judge:
        log.info("LLM judge disabled (--no-llm-judge / EVAL_NO_LLM_JUDGE).")
        return evaluators
    judge = _build_correctness_judge()
    if judge is not None:
        evaluators.append(judge)
    return evaluators


def _build_correctness_judge():
    """Construct a Phoenix ClassificationEvaluator backed by Bedrock Haiku.

    Returns None if Phoenix/litellm/Bedrock plumbing can't be set up,
    so the deterministic check still runs without LLM-judge coverage.
    Override the model via EVAL_JUDGE_MODEL.
    """
    model_id = os.environ.get(
        "EVAL_JUDGE_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    try:
        from phoenix.evals import LLM, create_classifier
    except Exception as e:  # noqa: BLE001
        log.warning("LLM judge disabled: phoenix.evals import failed (%s)", e)
        return None

    try:
        llm = LLM(provider="bedrock", model=model_id, client="litellm")
    except Exception as e:  # noqa: BLE001
        log.warning("LLM judge disabled: could not build Bedrock LLM (%s)", e)
        return None

    template = (
        "You are evaluating a data-analyst agent's answer against an eval golden.\n"
        "\n"
        "<question>\n{{input}}\n</question>\n"
        "\n"
        "<agent_answer>\n{{output}}\n</agent_answer>\n"
        "\n"
        "<expected>\n"
        "The answer should contain these substrings (case-insensitive): {{expected}}\n"
        "</expected>\n"
        "\n"
        "<reviewer_notes>\n{{metadata}}\n</reviewer_notes>\n"
        "\n"
        "Judging guidance:\n"
        "- For factual questions: 'correct' means the agent's answer is factually right and addresses the question; the expected substrings + reviewer notes describe the ground truth.\n"
        "- For refusal cases: 'correct' means the agent honestly declined / explained the data limitation rather than fabricating a number.\n"
        "- For wrong-premise cases (e.g. data outside the dataset's range): 'correct' means the agent acknowledged that no data exists rather than computing over the wrong slice.\n"
        "- If the agent errored before producing an answer, output is 'incorrect'.\n"
        "\n"
        "Is the agent's answer correct or incorrect?\n"
    )
    log.info("LLM judge enabled: Bedrock model=%s", model_id)
    return create_classifier(
        name="correctness",
        llm=llm,
        prompt_template=template,
        choices={"correct": 1.0, "incorrect": 0.0},
    )


# --- report + helpers -------------------------------------------------------


def _default_experiment_name() -> str:
    sha = _git_sha() or "nosha"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{sha[:8]}"


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=EVAL_ROOT.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:  # noqa: BLE001
        return ""


def _git_branch() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=EVAL_ROOT.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:  # noqa: BLE001
        return ""


def _g(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` off `obj` whether it's a dict, TypedDict, or dataclass.

    Phoenix's client mixes TypedDicts (RanExperiment, ExperimentRun,
    ExperimentEvaluation) with dataclasses (ExperimentEvaluationRun) —
    so a single experiment's nested data needs both `[k]` and `.k`
    depending on which layer you're at. This unifies them.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _all_passed(ran) -> bool:
    """True if every task succeeded AND every evaluator scored 1.0."""
    task_runs = _g(ran, "task_runs") or []
    if not task_runs:
        return False
    if any(_g(r, "error") for r in task_runs):
        return False
    eval_runs = _g(ran, "evaluation_runs") or []
    if not eval_runs:
        return False
    for er in eval_runs:
        if _g(er, "error"):
            return False
        score = _g(_g(er, "result"), "score")
        if score is None or float(score) < 1.0:
            return False
    return True


def _write_local_report(
    ran,
    experiment_url: str,
    experiment_name: str,
    metadata: dict[str, Any],
) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = REPORTS_DIR / f"{ts}-{experiment_name}.json"
    report = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "experiment_name": experiment_name,
        "experiment_id": ran.get("experiment_id"),
        "experiment_url": experiment_url,
        "metadata": metadata,
        "summary": _summarise_for_report(ran),
    }
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return out_path


def _summarise_for_report(ran) -> dict[str, Any]:
    """Per-task pass/fail counts grouped via experiment_run_id."""
    task_runs = _g(ran, "task_runs") or []
    eval_runs = _g(ran, "evaluation_runs") or []
    total = len(task_runs)
    errored_run_ids = {_g(r, "id") for r in task_runs if _g(r, "error")}

    evals_by_run: dict[str, list[Any]] = {}
    for er in eval_runs:
        evals_by_run.setdefault(_g(er, "experiment_run_id", ""), []).append(er)

    passed = 0
    for r in task_runs:
        rid = _g(r, "id")
        if rid in errored_run_ids:
            continue
        ers = evals_by_run.get(rid) or []
        if not ers or any(_g(er, "error") for er in ers):
            continue
        scores = [_g(_g(er, "result"), "score") for er in ers]
        if all(s is not None and float(s) >= 1.0 for s in scores):
            passed += 1

    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "errored_tasks": len(errored_run_ids),
        "all_passed": total > 0 and passed == total,
    }


if __name__ == "__main__":
    sys.exit(main())
