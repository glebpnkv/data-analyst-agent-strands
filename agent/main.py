import argparse
import json
import logging
import os
import shlex
import sys
from datetime import datetime
from os import linesep
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv
from strands.telemetry import StrandsTelemetry
from strands_tools.code_interpreter.models import ExecuteCommandAction

from agent import make_agent, make_mcp_client
from utils.utils import _json_default, extract_artifacts_from_sandbox, extract_text

AGENT_DIR = Path(__file__).resolve().parent
load_dotenv(AGENT_DIR / ".env")


def parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments for the Strands Glue pipeline agent.

    :return: argparse.Namespace object containing parsed arguments
    """
    p = argparse.ArgumentParser(
        description="Strands Glue Pipeline Agent (Bedrock + Athena MCP + AgentCore Code Interpreter)"
    )
    p.add_argument(
        "--profile",
        default=os.environ.get("AWS_PROFILE"),
        help="AWS profile name used with aws sso login",
    )
    p.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION"),
        help="AWS region (e.g., eu-west-1, us-east-1)",
    )
    p.add_argument(
        "--model-id",
        default=os.environ.get("MODEL_ID"),
        help="AWS Bedrock model ID (or inference profile ARN / model id you use in Bedrock)",
    )
    p.add_argument(
        "--table",
        default=os.environ.get("ATHENA_TABLE", ""),
        help="Optional preferred table context for prompt scaffolding",
    )
    p.add_argument(
        "--prompt",
        default=None,
        help="Optional one-shot user request. If omitted, runs interactive chat mode.",
    )
    p.add_argument(
        "--list-tools",
        action="store_true",
        help="List MCP tools and exit (useful for debugging MCP/AWS setup)",
    )
    p.add_argument("--run-dir", default="runs", help="Base directory for logs/traces/artifacts")
    p.add_argument("--enable-otlp", action="store_true", help="Also export traces to OTLP endpoint")
    p.add_argument("--otel-endpoint", default=None, help="OTLP endpoint, e.g. http://localhost:4318")

    return p.parse_args()


def setup_observability(
    run_dir: str,
    enable_otlp: bool = False,
    otel_endpoint: str | None = None,
) -> Tuple[Path, Path]:
    """
    Setup observability infrastructure for the agent run.

    :param run_dir: Base directory for logs/traces/artifacts
    :param enable_otlp: Whether to enable OTLP export
    :param otel_endpoint: OTLP endpoint URL (optional)
    :return: Tuple of observability directory and artifacts directory
    """
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_root_dir = Path(run_dir) / "strands-glue-pipeline-agent" / ts
    obs_dir = run_root_dir / "observability"
    artifacts_dir = run_root_dir / "artifacts"
    obs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # 1) Python logging for Strands SDK internals (debug logs).
    log_file = obs_dir / "strands_debug.log"
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Avoid duplicate handlers if function is called multiple times in dev.
    if not any(
        isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == str(log_file)
        for h in root_logger.handlers
    ):
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        root_logger.addHandler(fh)

    # Strands logger debug level (documented quickstart pattern).
    logging.getLogger("strands").setLevel(logging.DEBUG)

    # 2) Strands OpenTelemetry traces -> local JSONL (documented pattern).
    trace_jsonl = obs_dir / "traces.jsonl"
    trace_fp = open(trace_jsonl, "wt", encoding="utf-8")

    telemetry = StrandsTelemetry()
    telemetry.setup_console_exporter(
        out=trace_fp,
        formatter=lambda span: span.to_json() + linesep,
    )

    # Optional OTLP export (Jaeger/Langfuse/etc.).
    if enable_otlp:
        if otel_endpoint:
            telemetry.setup_otlp_exporter(endpoint=otel_endpoint)
        else:
            telemetry.setup_otlp_exporter()
        telemetry.setup_meter(
            enable_console_exporter=False,
            enable_otlp_exporter=True,
        )
    else:
        # Keep local metrics provider available even without OTLP.
        telemetry.setup_meter(enable_console_exporter=False, enable_otlp_exporter=False)

    return obs_dir, artifacts_dir


def ensure_sandbox_workspace(code_interpreter_tool, ci_session_name: str, sandbox_work_dir: str) -> bool:
    """
    Create a dedicated workspace directory in the sandbox for agent outputs.

    :param code_interpreter_tool: AgentCoreCodeInterpreter instance
    :param ci_session_name: Code interpreter session name
    :param sandbox_work_dir: Dedicated output path inside sandbox
    :return: True if setup succeeded, False otherwise
    """
    result = code_interpreter_tool.execute_command(
        ExecuteCommandAction(
            type="executeCommand",
            session_name=ci_session_name,
            command=f"mkdir -p {shlex.quote(sandbox_work_dir)}",
        )
    )
    return result.get("status") == "success"


def list_tools_mode() -> int:
    """
    List available tools in MCP mode without constructing an agent.

    :return: Exit code (0 for success)
    """
    mcp_client = make_mcp_client()
    try:
        with mcp_client:
            tools = mcp_client.list_tools_sync()
            print("Available MCP tools:", flush=True)
            for t in tools:
                name = getattr(t, "tool_name", None) or (t.get("tool_name") if isinstance(t, dict) else str(t))
                print(f" - {name}", flush=True)
        return 0
    finally:
        try:
            mcp_client.stop(None, None, None)
        except Exception as e:
            print(f"Warning during MCP cleanup in list_tools_mode: {e}", file=sys.stderr, flush=True)


def run_agent_mode(args: argparse.Namespace) -> int:
    """
    Run the Strands Glue pipeline agent with the specified configuration.

    :param args: Command-line arguments
    :return: Exit code (0 for success)
    """
    if not args.model_id:
        print("--model-id is required unless using --list-tools", file=sys.stderr, flush=True)
        return 2
    if not args.region:
        print("--region is required unless set in environment", file=sys.stderr, flush=True)
        return 2

    run_dir, artifacts_dir = setup_observability(
        run_dir=args.run_dir,
        enable_otlp=args.enable_otlp,
        otel_endpoint=args.otel_endpoint,
    )

    mcp_client = make_mcp_client()
    try:
        agent, ci_session_name, code_interpreter_tool = make_agent(
            profile=args.profile,
            region=args.region,
            model_id=args.model_id,
            mcp_client=mcp_client,
        )
        if not ci_session_name or code_interpreter_tool is None:
            print(
                "ERROR: code interpreter setup failed in local agent mode.",
                file=sys.stderr,
                flush=True,
            )
            return 1

        preferred_table_text = args.table if args.table else "(none configured)"
        sandbox_work_dir = f"workspace/{ci_session_name}"

        if not ensure_sandbox_workspace(code_interpreter_tool, ci_session_name, sandbox_work_dir):
            print(
                f"Warning: failed to create sandbox working directory '{sandbox_work_dir}'. "
                "Continuing, but artifact extraction may be less reliable.",
                file=sys.stderr,
                flush=True,
            )

        def build_task(user_prompt: str) -> str:
            """
            Build a task wrapper with shared execution context for each agent turn.

            :param user_prompt: Raw user request
            :return: Prompt passed to the agent
            """
            return f"""
User task:
{user_prompt}

Execution context:
- AWS region: {args.region}
- Preferred table context: {preferred_table_text}
- Dedicated sandbox output directory: {sandbox_work_dir}
""".strip()

        def run_turn(user_prompt: str, turn_idx: int | None = None) -> str:
            """
            Execute one prompt turn and persist turn outputs.

            :param user_prompt: Raw user prompt
            :param turn_idx: Optional turn number for interactive mode files
            :return: Final text produced by the agent for this turn
            """
            task = build_task(user_prompt)

            try:
                result = agent(task)
            except Exception as e:
                final_text = (
                    "Plan:\n"
                    "- Report the tool failure.\n"
                    "- Explain the likely AWS/MCP configuration issue.\n"
                    "- Continue with best-effort guidance.\n\n"
                    f"I hit a tool failure: {e}\n"
                    "Likely AWS configuration issue: verify AWS_PROFILE/AWS_REGION, run `aws sso login`, "
                    "and confirm the Athena MCP server is available."
                )
                print(final_text, flush=True)
                (run_dir / "agent_error.txt").write_text(str(e), encoding="utf-8")
                return final_text

            final_text = extract_text(result)

            turn_prefix = f"turn_{turn_idx:04d}_" if turn_idx is not None else ""
            (run_dir / f"{turn_prefix}final_answer.txt").write_text(final_text, encoding="utf-8")

            # Structured metrics summary (tokens, latency, tool usage, traces summary).
            try:
                metrics_summary = result.metrics.get_summary()
                (run_dir / f"{turn_prefix}metrics_summary.json").write_text(
                    json.dumps(metrics_summary, indent=2, default=_json_default),
                    encoding="utf-8",
                )
            except Exception as e:
                (run_dir / f"{turn_prefix}metrics_summary_error.txt").write_text(str(e), encoding="utf-8")

            # Optional: raw result dump for debugging.
            try:
                (run_dir / f"{turn_prefix}agent_result_raw.json").write_text(
                    json.dumps(result, indent=2, default=_json_default),
                    encoding="utf-8",
                )
            except Exception:
                (run_dir / f"{turn_prefix}agent_result_raw.txt").write_text(str(result), encoding="utf-8")

            return final_text

        if args.prompt:
            final_text = run_turn(args.prompt)
            print(final_text, flush=True)
        else:
            # Interactive mode keeps one agent instance/session for multi-turn conversation.
            print(
                "Interactive mode started. Enter prompts and press Ctrl+C to stop. "
                "Type 'exit' or 'quit' to end normally.",
                flush=True,
            )
            transcript_path = run_dir / "chat_transcript.txt"
            turn_idx = 1
            while True:
                user_prompt = input("\nYou> ").strip()
                if not user_prompt:
                    continue
                if user_prompt.lower() in {"exit", "quit"}:
                    print("Exiting interactive mode.", flush=True)
                    break

                final_text = run_turn(user_prompt, turn_idx=turn_idx)
                print(f"\nAgent> {final_text}", flush=True)
                with open(transcript_path, "a", encoding="utf-8") as fp:
                    fp.write(f"\n[turn {turn_idx}] USER\n{user_prompt}\n")
                    fp.write(f"[turn {turn_idx}] AGENT\n{final_text}\n")
                turn_idx += 1

        # Extract all artifacts from code interpreter sandbox.
        try:
            extracted_paths = extract_artifacts_from_sandbox(
                code_interpreter_tool=code_interpreter_tool,
                ci_session_name=ci_session_name,
                artifacts_dir=artifacts_dir,
                sandbox_root_path=sandbox_work_dir,
                max_files=200,
            )
            print(
                f"[artifacts] extracted {len(extracted_paths)} file(s) from sandbox path '{sandbox_work_dir}'",
                flush=True,
            )
            print(f"\n[run artifacts] {run_dir}", flush=True)
        except Exception as e:
            print(f"Warning during artifact extraction: {e}", file=sys.stderr, flush=True)
        return 0

    finally:
        try:
            mcp_client.stop(None, None, None)
        except Exception as e:
            print(f"Warning during MCP cleanup in run_agent_mode: {e}", file=sys.stderr, flush=True)

        trace_fp = getattr(args, "_trace_fp", None)
        if trace_fp:
            try:
                trace_fp.close()
            except Exception:
                pass


def main() -> int:
    try:
        args = parse_args()
        if args.list_tools:
            return list_tools_mode()
        return run_agent_mode(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr, flush=True)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
