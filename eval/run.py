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
    evaluators = _build_evaluators()

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
        dataset_id=dataset.id, experiment_id=ran.id,
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


def _build_evaluators() -> list:
    """Phoenix-callable evaluators. Each returns (score, label, explanation)."""

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

    return [text_contains]


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


def _all_passed(ran) -> bool:
    """True if every (run × evaluator) returned score == 1.0.

    `ran.runs` is a list of ExperimentRun, each with `.evaluation_runs`
    (Phoenix's RanExperiment shape across recent client versions).
    """
    runs = getattr(ran, "runs", None) or []
    if not runs:
        return False
    for r in runs:
        evals = getattr(r, "evaluation_runs", None) or getattr(r, "evaluations", None) or []
        if not evals:
            return False
        for e in evals:
            score = getattr(getattr(e, "result", e), "score", None)
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
        "experiment_id": getattr(ran, "id", None),
        "experiment_url": experiment_url,
        "metadata": metadata,
        "summary": _summarise_for_report(ran),
    }
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return out_path


def _summarise_for_report(ran) -> dict[str, Any]:
    runs = getattr(ran, "runs", None) or []
    total = len(runs)
    passed = 0
    for r in runs:
        evals = getattr(r, "evaluation_runs", None) or getattr(r, "evaluations", None) or []
        if evals and all(
            (float(getattr(getattr(e, "result", e), "score", 0)) >= 1.0) for e in evals
        ):
            passed += 1
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "all_passed": total > 0 and passed == total,
    }


if __name__ == "__main__":
    sys.exit(main())
