"""Tool factories used by the strands Glue pipeline agent.

This module exposes small factory helpers that build runtime tool callables.
The returned callables are decorated as Strands tools in production and can be
left undecorated in tests.
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd


def _identity_decorator(func: Callable[..., str]) -> Callable[..., str]:
    """
    Return the function unchanged.

    Args:
        func: Callable to return as-is when no tool decorator is available.

    Returns:
        The same callable passed in.
    """
    return func


def _resolve_tool_decorator(
    tool_decorator: Callable[[Callable[..., str]], Callable[..., str]] | None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """
    Resolve the decorator used to register tool functions.

    Args:
        tool_decorator: Optional explicit decorator override (typically in tests).

    Returns:
        Callable decorator selected by precedence:
        1. `tool_decorator` when provided.
        2. `strands.tool` when importable in the current environment.
        3. `_identity_decorator` fallback when Strands is unavailable.
    """
    if tool_decorator is not None:
        return tool_decorator
    try:
        from strands import tool as strands_tool

        return strands_tool
    except Exception:
        return _identity_decorator


def _resolve_ci_factories(
    file_content_factory: Callable[..., Any] | None,
    write_files_action_factory: Callable[..., Any] | None,
    list_files_action_factory: Callable[..., Any] | None,
) -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
    """
    Resolve Code Interpreter model constructors used by Athena CSV handoff.

    Args:
        file_content_factory: Optional `FileContent` constructor override.
        write_files_action_factory: Optional `WriteFilesAction` constructor override.
        list_files_action_factory: Optional `ListFilesAction` constructor override.

    Returns:
        Tuple of constructors `(FileContent, WriteFilesAction, ListFilesAction)`.
        Explicit overrides are used when all are provided; otherwise defaults are
        imported from `strands_tools.code_interpreter.models`.
    """
    if file_content_factory and write_files_action_factory and list_files_action_factory:
        return file_content_factory, write_files_action_factory, list_files_action_factory

    from strands_tools.code_interpreter.models import FileContent, ListFilesAction, WriteFilesAction

    return (
        file_content_factory or FileContent,
        write_files_action_factory or WriteFilesAction,
        list_files_action_factory or ListFilesAction,
    )


def make_athena_query_to_ci_csv(
    session: Any,
    region: str,
    code_interpreter_tool: Any,
    ci_session_name: str,
    workgroup: str = "primary",
    tool_decorator: Callable[[Callable[..., str]], Callable[..., str]] | None = None,
    file_content_factory: Callable[..., Any] | None = None,
    write_files_action_factory: Callable[..., Any] | None = None,
    list_files_action_factory: Callable[..., Any] | None = None,
) -> Callable[..., str]:
    """
    Build an Athena-to-CSV tool for the code-interpreter sandbox.

    The returned tool runs read-only Athena SQL (`SELECT`/`WITH`), materializes
    all rows to a DataFrame, and writes a CSV file into the active Code
    Interpreter session.

    Args:
        session: Boto3 session used to build Athena clients.
        region: AWS region for Athena API calls.
        code_interpreter_tool: AgentCore code interpreter tool instance.
        ci_session_name: Code Interpreter session name included in responses.
        workgroup: Athena workgroup for query execution.
        tool_decorator: Optional Strands tool decorator override.
        file_content_factory: Optional `FileContent` constructor override.
        write_files_action_factory: Optional WriteFilesAction model constructor override.
        list_files_action_factory: Optional ListFilesAction model constructor override.

    Returns:
        Tool function `athena_query_to_ci_csv(...)` returning JSON text with
        either success metadata (`ok=true`) or structured failure diagnostics
        (`ok=false`).
    """
    tool_wrap = _resolve_tool_decorator(tool_decorator)
    file_content, write_files_action, list_files_action = _resolve_ci_factories(
        file_content_factory=file_content_factory,
        write_files_action_factory=write_files_action_factory,
        list_files_action_factory=list_files_action_factory,
    )

    @tool_wrap
    def athena_query_to_ci_csv(
        sql: str,
        database: str,
        sandbox_path: str = "data/athena_query.csv",
        poll_seconds: float = 0.5,
        timeout_seconds: int = 60,
    ) -> str:
        """
        Execute a read-only Athena query and save its result set as sandbox CSV.

        Args:
            sql: SQL text starting with `SELECT` or `WITH`.
            database: Athena database to query.
            sandbox_path: Destination path inside the Code Interpreter sandbox.
            poll_seconds: Poll interval while waiting for Athena terminal state.
            timeout_seconds: Max wait time before returning a timeout error.

        Returns:
            JSON text.

            Success payload (`ok=true`) includes:
            - `query_execution_id`, `database`, `workgroup`
            - `sql_executed`, `sandbox_path`, `ci_session_name`
            - `rows`, `columns`, and `preview_rows`
            - raw `write_result` and `list_result` from the code interpreter API

            Error payload (`ok=false`) includes:
            - human-readable `error`
            - optional `reason`
            - query context such as SQL, database, workgroup, and execution id
        """
        raw_sql = (sql or "").strip().rstrip(";")
        if not raw_sql:
            return json.dumps({"ok": False, "error": "Empty SQL"})

        if not re.match(r"(?is)^\s*(select|with)\b", raw_sql):
            return json.dumps({"ok": False, "error": "Only read-only SELECT/WITH queries are allowed."})

        query_database = (database or "").strip()
        if not query_database:
            return json.dumps(
                {
                    "ok": False,
                    "error": "database is required (pass database=...).",
                }
            )

        wrapped_sql = f"SELECT * FROM ({raw_sql}) AS t"
        athena = session.client("athena", region_name=region)

        try:
            start = athena.start_query_execution(
                QueryString=wrapped_sql,
                QueryExecutionContext={"Database": query_database, "Catalog": "AwsDataCatalog"},
                WorkGroup=workgroup,
            )
            qid = start["QueryExecutionId"]

            deadline = time.time() + timeout_seconds
            state = "QUEUED"
            reason = None
            timed_out = True

            while time.time() < deadline:
                q = athena.get_query_execution(QueryExecutionId=qid)
                status = q["QueryExecution"]["Status"]
                state = status["State"]
                reason = status.get("StateChangeReason")
                if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    timed_out = False
                    break
                time.sleep(max(poll_seconds, 0.0))

            if timed_out and state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "Athena query timeout",
                        "reason": reason,
                        "query_execution_id": qid,
                        "sql_attempted": wrapped_sql,
                        "database": query_database,
                        "workgroup": workgroup,
                    }
                )

            if state != "SUCCEEDED":
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"Athena query {state}",
                        "reason": reason,
                        "query_execution_id": qid,
                        "sql_attempted": wrapped_sql,
                        "database": query_database,
                        "workgroup": workgroup,
                    }
                )

            rows: list[list[Any]] = []
            next_token = None
            col_names: list[str] | None = None
            first_page = True

            while True:
                kwargs = {"QueryExecutionId": qid, "MaxResults": 1000}
                if next_token:
                    kwargs["NextToken"] = next_token
                resp = athena.get_query_results(**kwargs)

                result_set = resp["ResultSet"]
                metadata = result_set["ResultSetMetadata"]["ColumnInfo"]
                if col_names is None:
                    col_names = [c["Name"] for c in metadata]

                page_rows = result_set.get("Rows", [])
                start_idx = 0
                if first_page and page_rows:
                    header_cells = page_rows[0].get("Data", [])
                    header_values = [cell.get("VarCharValue") for cell in header_cells]
                    if header_values == col_names:
                        start_idx = 1

                for row in page_rows[start_idx:]:
                    data = row.get("Data", [])
                    vals = [cell.get("VarCharValue") if i < len(data) else None for i, cell in enumerate(data)]
                    if len(vals) < len(col_names):
                        vals += [None] * (len(col_names) - len(vals))
                    rows.append(vals[: len(col_names)])

                first_page = False
                next_token = resp.get("NextToken")
                if not next_token:
                    break

            df = pd.DataFrame(rows, columns=col_names or [])
            for column in df.columns:
                try:
                    df[column] = pd.to_numeric(df[column])
                except (TypeError, ValueError):
                    pass

            csv_text = df.to_csv(index=False)

            try:
                write_result = code_interpreter_tool.write_files(
                    write_files_action(
                        type="writeFiles",
                        content=[file_content(path=sandbox_path, text=csv_text)],
                    )
                )
                list_result = code_interpreter_tool.list_files(
                    list_files_action(
                        type="listFiles",
                        path=str(Path(sandbox_path).parent),
                    )
                )
            except Exception as exc:
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"Code interpreter write failed: {exc}",
                        "query_execution_id": qid,
                        "sql_attempted": wrapped_sql,
                        "database": query_database,
                        "workgroup": workgroup,
                        "sandbox_path": sandbox_path,
                        "ci_session_name": ci_session_name,
                    }
                )

            return json.dumps(
                {
                    "ok": True,
                    "query_execution_id": qid,
                    "database": query_database,
                    "workgroup": workgroup,
                    "sql_executed": wrapped_sql,
                    "sandbox_path": sandbox_path,
                    "rows": int(len(df)),
                    "columns": list(df.columns),
                    "preview_rows": df.head(5).to_dict(orient="records"),
                    "ci_session_name": ci_session_name,
                    "write_result": write_result,
                    "list_result": list_result,
                },
                default=str,
            )

        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "sql_attempted": wrapped_sql,
                    "database": query_database,
                    "workgroup": workgroup,
                }
            )

    return athena_query_to_ci_csv


def make_glue_job_run_diagnostics_tool(
    session: Any,
    region: str,
    tool_decorator: Callable[[Callable[..., str]], Callable[..., str]] | None = None,
) -> Callable[..., str]:
    """
    Build a Glue run diagnostics tool that enriches run metadata with log lines.

    This complements MCP `get-job-run` by searching CloudWatch log groups for
    run-specific error/output messages around the run's execution window.

    Args:
        session: Boto3 session used to create Glue and CloudWatch Logs clients.
        region: AWS region for Glue/CloudWatch API calls.
        tool_decorator: Optional Strands tool decorator override.

    Returns:
        Tool function `glue_get_job_run_diagnostics(...)` returning JSON text
        with run metadata, group-level search diagnostics, and merged log events.
    """
    tool_wrap = _resolve_tool_decorator(tool_decorator)

    @tool_wrap
    def glue_get_job_run_diagnostics(
        job_name: str,
        job_run_id: str,
        max_events: int = 200,
    ) -> str:
        """
        Fetch Glue run metadata and best-effort CloudWatch logs for one run.

        Args:
            job_name: Glue job name whose run should be inspected.
            job_run_id: Glue run identifier returned by `start-job-run`.
            max_events: Max log events to return (clamped to the range 10-1000).

        Returns:
            JSON text.

            Success payload (`ok=true`) includes:
            - run summary: `state`, `error_message`, `arguments`
            - timing fields: `started_on`, `completed_on`,
              `diagnostic_window_start`, `diagnostic_window_end`
            - `group_diagnostics`: per-log-group lookup outcomes
            - `log_events`: merged events with timestamp/message/group/stream

            Failure payload (`ok=false`) includes:
            - `error` message
            - `job_name` and `job_run_id` for traceability
        """
        if not job_name or not job_run_id:
            return json.dumps({"ok": False, "error": "job_name and job_run_id are required."})

        glue = session.client("glue", region_name=region)
        logs = session.client("logs", region_name=region)

        try:
            run_resp = glue.get_job_run(
                JobName=job_name,
                RunId=job_run_id,
                PredecessorsIncluded=False,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"get_job_run failed: {exc}",
                    "job_name": job_name,
                    "job_run_id": job_run_id,
                }
            )

        job_run = run_resp.get("JobRun", {})
        state = job_run.get("JobRunState")
        error_message = job_run.get("ErrorMessage")
        started_on = job_run.get("StartedOn")
        completed_on = job_run.get("CompletedOn")
        log_group_name = job_run.get("LogGroupName")

        # Use a bounded time window around the run to avoid scanning excessive logs.
        now_utc = datetime.now(timezone.utc)
        if isinstance(started_on, datetime):
            start_dt = started_on - timedelta(minutes=5)
        else:
            start_dt = now_utc - timedelta(hours=6)
        if isinstance(completed_on, datetime):
            end_dt = completed_on + timedelta(minutes=5)
        else:
            end_dt = now_utc + timedelta(minutes=5)

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        candidate_groups = []
        if isinstance(log_group_name, str) and log_group_name:
            candidate_groups.append(log_group_name)
        candidate_groups.extend(
            [
                "/aws-glue/jobs/error",
                "/aws-glue/jobs/output",
                "/aws-glue/jobs/logs-v2",
                "/aws-glue/python-jobs/error",
                "/aws-glue/python-jobs/output",
            ]
        )

        # Deduplicate groups while preserving order.
        seen_groups: set[str] = set()
        ordered_groups = []
        for group in candidate_groups:
            if group not in seen_groups:
                ordered_groups.append(group)
                seen_groups.add(group)

        max_events = max(10, min(max_events, 1000))
        collected_events: list[dict[str, Any]] = []
        group_diagnostics: list[dict[str, Any]] = []

        for group in ordered_groups:
            group_info: dict[str, Any] = {"log_group": group}
            try:
                # Fast path: search by run ID in event text.
                filter_resp = logs.filter_log_events(
                    logGroupName=group,
                    startTime=start_ms,
                    endTime=end_ms,
                    filterPattern=f'"{job_run_id}"',
                    limit=max_events,
                )
                events = filter_resp.get("events", [])
                if events:
                    group_info["match_type"] = "filter_by_run_id"
                    group_info["matched_events"] = len(events)
                    for event in events:
                        collected_events.append(
                            {
                                "timestamp": event.get("timestamp"),
                                "message": event.get("message", ""),
                                "log_group": group,
                                "log_stream": event.get("logStreamName"),
                            }
                        )
                else:
                    # Fallback path: try likely stream prefixes for this run.
                    prefixes = [
                        job_run_id,
                        job_name,
                        f"{job_name}/{job_run_id}",
                        f"{job_name}-{job_run_id}",
                    ]
                    stream_names: list[str] = []
                    for prefix in prefixes:
                        ds = logs.describe_log_streams(
                            logGroupName=group,
                            logStreamNamePrefix=prefix,
                            descending=True,
                            limit=25,
                        )
                        for s in ds.get("logStreams", []):
                            name = s.get("logStreamName")
                            if isinstance(name, str) and name not in stream_names:
                                stream_names.append(name)

                    stream_names = stream_names[:10]
                    stream_event_count = 0
                    for stream in stream_names:
                        get_resp = logs.get_log_events(
                            logGroupName=group,
                            logStreamName=stream,
                            startTime=start_ms,
                            endTime=end_ms,
                            limit=100,
                            startFromHead=False,
                        )
                        for event in get_resp.get("events", []):
                            collected_events.append(
                                {
                                    "timestamp": event.get("timestamp"),
                                    "message": event.get("message", ""),
                                    "log_group": group,
                                    "log_stream": stream,
                                }
                            )
                            stream_event_count += 1
                    group_info["match_type"] = "stream_prefix_fallback"
                    group_info["matched_events"] = stream_event_count
                    group_info["stream_count"] = len(stream_names)
            except logs.exceptions.ResourceNotFoundException:
                group_info["error"] = "log group not found"
            except Exception as exc:
                group_info["error"] = str(exc)

            group_diagnostics.append(group_info)

        # Deduplicate and sort events by timestamp.
        dedup_map: dict[tuple[Any, str, str, str], dict[str, Any]] = {}
        for event in collected_events:
            key = (
                event.get("timestamp"),
                event.get("message", ""),
                event.get("log_group", ""),
                event.get("log_stream", ""),
            )
            dedup_map[key] = event

        ordered_events = sorted(
            dedup_map.values(),
            key=lambda e: (e.get("timestamp") or 0),
        )[-max_events:]

        return json.dumps(
            {
                "ok": True,
                "job_name": job_name,
                "job_run_id": job_run_id,
                "state": state,
                "error_message": error_message,
                "started_on": started_on,
                "completed_on": completed_on,
                "arguments": job_run.get("Arguments", {}),
                "log_group_name": log_group_name,
                "diagnostic_window_start": start_dt.isoformat(),
                "diagnostic_window_end": end_dt.isoformat(),
                "group_diagnostics": group_diagnostics,
                "log_events": ordered_events,
            },
            default=str,
        )

    return glue_get_job_run_diagnostics
