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
    _shim_node_id_onto_examples(dataset)
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


def _shim_node_id_onto_examples(dataset) -> None:
    """Work around Phoenix client/server version skew.

    Phoenix client 2.x's experiment runner reads `example["node_id"]`
    when posting each task run; the field was added server-side in
    Phoenix ~14. Our deployed Phoenix is 11.4 and returns examples with
    `id` only — but that `id` IS the GraphQL global ID (the value the
    newer server populates `node_id` with). So copy id → node_id on
    each example in place. Drop this shim when the Phoenix server is
    bumped past v14.
    """
    examples = getattr(dataset, "examples", None) or []
    patched = 0
    for ex in examples:
        if isinstance(ex, dict) and "node_id" not in ex and "id" in ex:
            ex["node_id"] = ex["id"]
            patched += 1
    if patched:
        log.debug("shimmed node_id onto %d dataset example(s)", patched)


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
    """True if every task succeeded AND every evaluator scored 1.0.

    Phoenix's RanExperiment is a TypedDict (dict subclass) with
    `task_runs` (list of ExperimentRun TypedDicts) and `evaluation_runs`
    (list of ExperimentEvaluationRun TypedDicts, one per (run, evaluator)
    pair). A task with `error` set never produces evaluations, so we
    have to check both lists.
    """
    task_runs = ran.get("task_runs") or []
    if not task_runs:
        return False
    if any(r.get("error") for r in task_runs):
        return False
    eval_runs = ran.get("evaluation_runs") or []
    if not eval_runs:
        return False
    for er in eval_runs:
        if er.get("error"):
            return False
        result = er.get("result") or {}
        score = result.get("score") if isinstance(result, dict) else None
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
    """Per-task pass/fail counts from the flat evaluation_runs list.

    `evaluation_runs` is keyed off `experiment_run_id`; we group by it
    and a task passes only if every evaluator on it scored 1.0 with no
    error, and the task itself didn't error out.
    """
    task_runs = ran.get("task_runs") or []
    eval_runs = ran.get("evaluation_runs") or []
    total = len(task_runs)
    errored_run_ids = {r["id"] for r in task_runs if r.get("error")}

    evals_by_run: dict[str, list[dict[str, Any]]] = {}
    for er in eval_runs:
        evals_by_run.setdefault(er.get("experiment_run_id", ""), []).append(er)

    passed = 0
    for r in task_runs:
        if r["id"] in errored_run_ids:
            continue
        ers = evals_by_run.get(r["id"]) or []
        if not ers:
            continue
        if any(er.get("error") for er in ers):
            continue
        scores = [
            (er.get("result") or {}).get("score") if isinstance(er.get("result"), dict) else None
            for er in ers
        ]
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
