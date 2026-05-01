"""Custom Strands hooks used by the Glue pipeline agent."""

import logging
import re
import time
from threading import Lock
from typing import Any, Optional

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent

LOGGER = logging.getLogger(__name__)

# Matches `aws glue get-job-run --job-name <x> --run-id <y>` in any order/whitespace.
# We key the throttle on (job_name, run_id) so different jobs don't interfere with each other.
_CLI_JOB_NAME_RE = re.compile(r"--job-name[=\s]+([^\s'\"]+)")
_CLI_RUN_ID_RE = re.compile(r"--run-id[=\s]+([^\s'\"]+)")
_CLI_QUERY_EXEC_ID_RE = re.compile(r"--query-execution-id[=\s]+([^\s'\"]+)")


class GlueJobRunPollThrottleHook(HookProvider):
    """
    Throttle repeated poll-style tool calls, regardless of which tool ferries them.

    Covered poll patterns (each keyed so different resources don't interfere):
    - Glue `get-job-run` via `athena_manage_aws_glue_jobs` (MCP) or `awsapi_call_aws` (CLI).
    - Athena `get-query-execution` via `athena_manage_aws_athena_query_executions` (MCP)
      or `awsapi_call_aws` (CLI).

    Models sometimes claim in text "waiting 20s" and then call again 3s later; the throttle
    enforces the gap at the hook layer so the hallucinated wait becomes real.
    """

    def __init__(self, min_interval_seconds: float = 20.0):
        """
        Initialize the poll throttle hook.

        Args:
            min_interval_seconds: Minimum gap between identical get-job-run calls.
        """
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._last_call_ts_by_key: dict[tuple[str, str, str], float] = {}
        self._lock = Lock()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """
        Register hook callbacks with the Strands hook registry.
        """
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """
        Enforce a minimum interval for repeated `get-job-run` tool calls.
        """
        key = self._poll_key(event.tool_use)
        if key is None:
            return

        now = time.monotonic()
        with self._lock:
            last_ts = self._last_call_ts_by_key.get(key)
            remaining = 0.0 if last_ts is None else self.min_interval_seconds - (now - last_ts)

            if remaining > 0:
                LOGGER.info(
                    "Throttling repeated %s call for %s/%s by %.2fs",
                    key[0],
                    key[1],
                    key[2],
                    remaining,
                )
                time.sleep(remaining)
                now = time.monotonic()

            self._last_call_ts_by_key[key] = now

    def _poll_key(self, tool_use: dict[str, Any]) -> Optional[tuple[str, str, str]]:
        """
        Return a `(poll_kind, id1, id2)` key for a throttled poll call, or None.

        `poll_kind` distinguishes the operation (e.g. `glue-get-job-run`,
        `athena-get-query-execution`) so two unrelated polls can't share a slot.
        Covers both the MCP path and the CLI fallback — two calls with the same
        key are the same poll no matter which tool ferried them.
        """
        name = tool_use.get("name")
        tool_input = tool_use.get("input")
        if not isinstance(tool_input, dict):
            return None

        if name == "athena_manage_aws_glue_jobs":
            if tool_input.get("operation") != "get-job-run":
                return None
            job_name = tool_input.get("job_name")
            run_id = tool_input.get("job_run_id")
            if isinstance(job_name, str) and job_name and isinstance(run_id, str) and run_id:
                return ("glue-get-job-run", job_name, run_id)
            return None

        if name == "athena_manage_aws_athena_query_executions":
            if tool_input.get("operation") != "get-query-execution":
                return None
            qeid = tool_input.get("query_execution_id")
            if isinstance(qeid, str) and qeid:
                return ("athena-get-query-execution", qeid, "")
            return None

        if name == "awsapi_call_aws":
            cli = tool_input.get("cli_command")
            # cli_command may be a string or list[str]; normalise to string.
            if isinstance(cli, list):
                cli = " ".join(str(part) for part in cli)
            if not isinstance(cli, str):
                return None

            if "glue get-job-run" in cli:
                job_match = _CLI_JOB_NAME_RE.search(cli)
                run_match = _CLI_RUN_ID_RE.search(cli)
                if job_match and run_match:
                    return ("glue-get-job-run", job_match.group(1), run_match.group(1))
                return ("glue-get-job-run-cli", cli, "")

            if "athena get-query-execution" in cli:
                qe_match = _CLI_QUERY_EXEC_ID_RE.search(cli)
                if qe_match:
                    return ("athena-get-query-execution", qe_match.group(1), "")
                return ("athena-get-query-execution-cli", cli, "")

            return None

        return None
