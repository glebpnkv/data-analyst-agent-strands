"""Entry point: `uv run python -m eval.run [--smoke] [--tag TAG]`.

Loads every golden under `eval/goldens/`, runs each through the
deployed agent (POST /v1/chat over SSE), applies the deterministic
checks each golden declares, writes a JSON report per run.

Today (M2): text_contains check only. M3 adds tool-call correctness,
result-set comparison, schema grounding, and LLM-as-judge metrics.

Env vars (the only setup required from the operator):
  AGENT_BASE_URL                  http://localhost:8080
                                    (point at the SSM port-forward to
                                    the internal agent ALB)
  AGENT_SERVICE_AUTH_SECRET       the service auth secret used by
                                    deployed Chainlit -> Agent traffic
                                    (Secrets Manager:
                                    DataAnalystAgent/Dev/ServiceAuthSecret)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from eval.checks import check_text_contains
from eval.runners import AgentRunResult, DeployedAgentClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("eval.run")

EVAL_ROOT = Path(__file__).resolve().parent
GOLDENS_DIR = EVAL_ROOT / "goldens"
REPORTS_DIR = EVAL_ROOT / "reports"


def main() -> int:
    args = _parse_args()

    goldens = _load_goldens(GOLDENS_DIR)
    selected = _select_goldens(goldens, smoke=args.smoke, tag=args.tag, ids=args.id)
    if not selected:
        log.error("No goldens matched the selection. Aborting.")
        return 2
    log.info("Loaded %d golden(s); running %d after filtering", len(goldens), len(selected))

    try:
        client = DeployedAgentClient.from_env()
    except RuntimeError as e:
        log.error("Agent client setup failed: %s", e)
        return 2

    report_entries: list[dict[str, Any]] = []
    for i, golden in enumerate(selected, start=1):
        log.info("[%d/%d] %s", i, len(selected), golden["id"])
        entry = _run_one(client, golden)
        report_entries.append(entry)
        _print_one_line_summary(entry)

    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "goldens_total": len(selected),
        "agent_base_url": client.base_url,
        "entries": report_entries,
        "summary": _summarise(report_entries),
    }
    out_path = _write_report(report)
    log.info("Report written to %s", out_path)
    _print_summary(report)

    # Exit code: 0 if all checks passed, 1 otherwise. Used by future CI.
    return 0 if report["summary"]["all_passed"] else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run agent evals.")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run only goldens tagged 'smoke'. Default: run all.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Only run goldens with this tag. Repeatable. ANDed with --smoke.",
    )
    parser.add_argument(
        "--id",
        action="append",
        default=[],
        help="Only run goldens with this id. Repeatable. Overrides --smoke/--tag.",
    )
    return parser.parse_args()


def _load_goldens(root: Path) -> list[dict[str, Any]]:
    """Read every *.json under root/ and return a flat list of golden dicts."""
    goldens: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        content = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(content, list):
            goldens.extend(content)
        elif isinstance(content, dict):
            goldens.append(content)
        else:
            log.warning("Skipping %s: unexpected top-level type %s", path, type(content))
    return goldens


def _select_goldens(
    goldens: list[dict[str, Any]],
    *,
    smoke: bool,
    tag: list[str],
    ids: list[str],
) -> list[dict[str, Any]]:
    if ids:
        wanted = set(ids)
        return [g for g in goldens if g.get("id") in wanted]
    result = goldens
    if smoke:
        result = [g for g in result if "smoke" in (g.get("tags") or [])]
    for t in tag:
        result = [g for g in result if t in (g.get("tags") or [])]
    return result


def _run_one(client: DeployedAgentClient, golden: dict[str, Any]) -> dict[str, Any]:
    prompt = golden["input"]
    try:
        run = client.chat(prompt)
        run_error: str | None = None
    except Exception as e:  # noqa: BLE001 — top-level harness
        log.exception("Agent call failed for %s", golden["id"])
        run = AgentRunResult(session_id="", answer="")
        run_error = f"{type(e).__name__}: {e}"

    checks = _apply_checks(golden, run)
    passed = all(c["passed"] for c in checks)

    return {
        "id": golden["id"],
        "tags": golden.get("tags", []),
        "input": prompt,
        "run_error": run_error,
        "run": {
            "session_id": run.session_id,
            "answer": run.answer,
            "tool_call_count": run.tool_call_count,
            "tool_error_count": run.tool_error_count,
            "tool_calls": [asdict(t) for t in run.tool_calls],
            "usage": run.usage,
            "elapsed_seconds": round(run.elapsed_seconds, 2),
            "errors": run.errors,
        },
        "checks": checks,
        "passed": passed and run_error is None,
    }


def _apply_checks(
    golden: dict[str, Any],
    run: AgentRunResult,
) -> list[dict[str, Any]]:
    """Run every check this golden declares; return per-check verdicts."""
    results: list[dict[str, Any]] = []

    expected_contains = golden.get("expected_answer_contains")
    if expected_contains:
        verdict = check_text_contains(run.answer, expected_contains)
        results.append({
            "name": "text_contains",
            "score": verdict.score,
            "passed": verdict.passed,
            "detail": {
                "expected_substrings": expected_contains,
                "matched": verdict.matched,
                "missing": verdict.missing,
            },
        })

    return results


def _summarise(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    entries_list = list(entries)
    total = len(entries_list)
    passed = sum(1 for e in entries_list if e["passed"])
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "all_passed": total > 0 and passed == total,
    }


def _print_one_line_summary(entry: dict[str, Any]) -> None:
    status = "PASS" if entry["passed"] else "FAIL"
    elapsed = entry["run"]["elapsed_seconds"]
    tools = entry["run"]["tool_call_count"]
    errors = entry["run"]["tool_error_count"]
    extra = f" [run_error={entry['run_error']}]" if entry["run_error"] else ""
    log.info(
        "  -> %s  %s  tools=%d  tool_errors=%d  elapsed=%.1fs%s",
        status, entry["id"], tools, errors, elapsed, extra,
    )


def _print_summary(report: dict[str, Any]) -> None:
    s = report["summary"]
    log.info("=== Summary === total=%d passed=%d failed=%d", s["total"], s["passed"], s["failed"])


def _write_report(report: dict[str, Any]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = REPORTS_DIR / f"{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    sys.exit(main())
