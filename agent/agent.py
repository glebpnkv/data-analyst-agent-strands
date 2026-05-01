import os
import sys
import uuid
from pathlib import Path
from typing import Optional

import boto3
from mcp import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent, AgentSkills
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from strands_tools.code_interpreter import AgentCoreCodeInterpreter

from utils.hooks import GlueJobRunPollThrottleHook
from utils.prompts import SYSTEM_PROMPT
from utils.tools import make_athena_query_to_ci_csv, make_glue_job_run_diagnostics_tool

ALLOWED_MCP_TOOLS = [
    "manage_aws_athena_databases_and_tables",
    "manage_aws_athena_query_executions",
    "manage_aws_glue_jobs",
    "manage_aws_glue_crawlers",
    "manage_aws_glue_triggers",
    "manage_aws_glue_workflows",
    "list_s3_buckets",
    "upload_to_s3",
    "manage_aws_athena_named_queries",
    "manage_aws_athena_workgroups"
]

AWS_API_ALLOWED_MCP_TOOLS = [
    "call_aws",
]

GITHUB_ALLOWED_MCP_TOOLS = [
    "get_file_contents",
    "list_branches",
    "create_branch",
    "push_files",
    "create_pull_request",
    # CI diagnosis (invoked on resume when the user reports CI failed):
    "list_workflow_runs",
    "get_workflow_run",
    "get_workflow_run_logs",
    "list_workflow_jobs",
    "get_job_logs",
]

GITHUB_MCP_SERVER_URL = "https://api.githubcopilot.com/mcp/"


def _get_glue_poll_interval_seconds(default_seconds: float = 20.0) -> float:
    """
    Resolve minimum poll interval for repeated Glue get-job-run calls.

    Reads `GLUE_GET_JOB_RUN_MIN_INTERVAL_SECONDS` from environment and falls back
    to `default_seconds` when unset or invalid.

    :param default_seconds: Default minimum interval in seconds
    :return: Non-negative interval in seconds
    """
    raw_value = (os.getenv("GLUE_GET_JOB_RUN_MIN_INTERVAL_SECONDS") or "").strip()
    if not raw_value:
        return max(0.0, default_seconds)

    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return max(0.0, default_seconds)


def _resolve_mcp_server_command() -> tuple[str, list[str]]:
    """
    Resolve how to launch the AWS data processing MCP server.

    Prefer the installed Python module for reproducible runtime deploys.
    Fall back to `uvx` when the package is not installed.
    """
    try:
        import awslabs.aws_dataprocessing_mcp_server.server  # noqa: F401

        return (
            sys.executable,
            [
                "-m",
                "awslabs.aws_dataprocessing_mcp_server.server",
                "--allow-write",
                "--allow-sensitive-data-access",
            ],
        )
    except Exception:
        return (
            "uvx",
            [
                "awslabs.aws-dataprocessing-mcp-server@latest",
                "--allow-write",
                "--allow-sensitive-data-access",
            ],
        )

def _resolve_api_mcp_server_command() -> tuple[str, list[str]]:
    try:
        import awslabs.aws_api_mcp_server.server  # noqa: F401

        return (
            sys.executable,
            [
                "-m",
                "awslabs.aws_api_mcp_server.server",
            ],
        )
    except Exception:
        return (
            "uvx",
            [
                "awslabs.aws-api-mcp-server@latest",
            ],
        )


def _build_mcp_env(profile: Optional[str], region: Optional[str]) -> dict[str, str]:
    """
    Build subprocess environment for the AWS MCP server.

    Local dev (no ECS task metadata env): also propagates
    AWS_SDK_LOAD_CONFIG=1 + AWS_EC2_METADATA_DISABLED=true onto the
    parent's os.environ. AWS_SDK_LOAD_CONFIG=1 lets boto3 resolve SSO
    credentials from `~/.aws/config`; without it some users' agents
    couldn't find their `aws sso login` session. AWS_EC2_METADATA_DISABLED=true
    avoids a multi-second IMDS lookup delay outside EC2.

    Container (ECS task metadata env present): we DO NOT mutate
    os.environ. AWS_SDK_LOAD_CONFIG=1 in a Fargate container with no
    `~/.aws/config` makes any boto3 call that touches scoped config
    fail with `ProfileNotFound: default`. Inside the container, the
    task role + ECS container credential provider are how boto3 gets
    credentials, no profile or config file involved.

    The subprocess env (returned dict) gets the same AWS_* settings in
    both cases — the MCP server is its own Python process and benefits
    from the same SSO-friendly behavior locally; in the container
    they're harmless because the subprocess inherits the same
    credential chain as the parent.
    """
    in_ecs = bool(
        os.environ.get("ECS_CONTAINER_METADATA_URI")
        or os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    )
    if not in_ecs:
        os.environ.setdefault("AWS_SDK_LOAD_CONFIG", "1")
        os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
        os.environ.setdefault("AWS_STS_REGIONAL_ENDPOINTS", "regional")

    env = dict(os.environ)  # preserves PATH/HOME/etc.
    env.setdefault("AWS_SDK_LOAD_CONFIG", "1")
    env.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    env.setdefault("AWS_STS_REGIONAL_ENDPOINTS", "regional")
    env["FASTMCP_LOG_LEVEL"] = "INFO"
    if profile:
        env["AWS_PROFILE"] = profile
    if region:
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region
    return env


def make_mcp_client(allowed_tools: Optional[list[str]] = None) -> MCPClient:
    """
    Create an MCP client configured for Athena/Glue operations.

    :param allowed_tools: Optional override for allowed tool names
    :return: MCPClient instance
    """
    profile = os.environ.get("AWS_PROFILE")
    region = os.environ.get("AWS_REGION")
    env = _build_mcp_env(profile=profile, region=region)
    server_command, server_args = _resolve_mcp_server_command()

    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command=server_command,
                args=server_args,
                env=env,
            )
        ),
        tool_filters={"allowed": allowed_tools or ALLOWED_MCP_TOOLS},
        prefix="athena",
    )

def make_aws_api_mcp_client() -> MCPClient:
    profile = os.environ.get("AWS_PROFILE")
    region = os.environ.get("AWS_REGION")
    env = _build_mcp_env(profile=profile, region=region)

    # AWS API MCP prefers its own profile env var.
    if profile:
        env["AWS_API_MCP_PROFILE_NAME"] = profile
    if region:
        env["AWS_REGION"] = region

    server_command, server_args = _resolve_api_mcp_server_command()

    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command=server_command,
                args=server_args,
                env=env,
            )
        ),
        tool_filters={"allowed": AWS_API_ALLOWED_MCP_TOOLS},
        prefix="awsapi",
    )

def make_github_mcp_client() -> MCPClient:
    """
    Create an MCP client for GitHub's official remote MCP server.

    Uses the hosted `https://api.githubcopilot.com/mcp/` endpoint over
    streamable HTTP. Auth is a fine-grained PAT from `GITHUB_PAT`, passed
    as a bearer token. Repo pinning is enforced both by PAT scope and by
    the rules in `_build_git_rules()`.
    """
    pat = os.environ.get("GITHUB_PAT", "").strip()
    if not pat:
        raise RuntimeError("GITHUB_PAT env var is required for the GitHub MCP client")

    headers = {"Authorization": f"Bearer {pat}"}

    return MCPClient(
        lambda: streamablehttp_client(
            url=GITHUB_MCP_SERVER_URL,
            headers=headers,
        ),
        tool_filters={"allowed": GITHUB_ALLOWED_MCP_TOOLS},
        prefix="github",
    )


def _build_runtime_glue_rules() -> str:
    """
    Build runtime-focused Glue rules appended to system prompt.

    :return: Runtime-specific prompt section
    """
    role_text = os.getenv("GLUE_JOB_ROLE_ARN", "") or "(not configured; ask user for role ARN)"
    scheduler_role_text = os.getenv("SCHEDULER_ATHENA_EXEC_ROLE_ARN", "") or "(not configured; ask user for scheduler role ARN or create one)"
    # TODO: move from static default script object to conversation-scoped script paths:
    # s3://glue-assets-554032904022-eu-central-1-an/strands-glue-pipeline-agent/scripts/<conversation_id>/<script_name>.py
    script_text = os.getenv("GLUE_JOB_DEFAULT_SCRIPT_S3", "") or "(not configured; ask user for script S3 URI)"
    temp_dir_text = os.getenv("GLUE_TEMP_DIR", "") or "(not configured)"
    poll_interval_seconds = _get_glue_poll_interval_seconds(default_seconds=20.0)
    return (
        "RUNTIME GLUE RULES:\n"
        "- When GLUE_JOB_ROLE_ARN is configured, always set `job_definition.Role` to that exact ARN.\n"
        "- Do not use AWSGlueServiceRole-default unless the user explicitly asks for it.\n"
        "- When role/script/temp are not provided by user, use these defaults:\n"
        f"  - Role ARN default: {role_text}\n"
        f"  - Scheduler Athena execution role default: {scheduler_role_text}\n"
        f"  - ScriptLocation default: {script_text}\n"
        f"  - --TempDir default: {temp_dir_text}\n"
        "- For EventBridge Scheduler + Athena SQL schedules:\n"
        "- If SCHEDULER_ATHENA_EXEC_ROLE_ARN is configured, always set Scheduler Target.RoleArn to that exact ARN.\n"
        "- Do not create ad-hoc scheduler execution roles when SCHEDULER_ATHENA_EXEC_ROLE_ARN is configured.\n"
        "- If no scheduler role is configured and schedule creation is requested, create one trusted by scheduler.amazonaws.com with Athena + Glue catalog + S3 runtime permissions; include both glue:GetPartition and glue:GetPartitions.\n"
        "- For crawler-based conditional triggers:\n"
        "  - ensure crawler exists first (via `athena_manage_aws_glue_crawlers`),\n"
        "  - in predicate conditions use `CrawlerName` with `CrawlState`, not `State`.\n"
        "- Completion precondition for any created/updated job:\n"
        "  - run `athena_manage_aws_glue_jobs(operation='start-job-run', ...)`,\n"
        f"  - poll `athena_manage_aws_glue_jobs(operation='get-job-run', ...)` once every {poll_interval_seconds:g} seconds until terminal state,\n"
        "  - only mark success if state is `SUCCEEDED`;\n"
        "  - if state is `FAILED`/`TIMEOUT`, call `glue_get_job_run_diagnostics` and include root-cause log lines.\n"
        "- If `update-job` fails due MCP-managed resource constraints, explain it and propose create-new-job fallback.\n"
        "- For schedule/cron output, always state timezone explicitly as UTC.\n"
        f"- Runtime guardrail: repeated `get-job-run` calls for the same `(job_name, job_run_id)` are throttled to at least {poll_interval_seconds:g}s.\n"
    )


def _build_git_rules() -> str:
    """
    Build GitHub workflow rules appended to the system prompt.

    Pins every GitHub call to the single configured repo, defines the
    scratch -> push -> wait -> production -> PR lifecycle, and forbids
    destructive operations.
    """
    owner = os.getenv("TARGET_REPO_OWNER", "").strip()
    repo = os.getenv("TARGET_REPO_NAME", "").strip()
    default_branch = os.getenv("TARGET_REPO_DEFAULT_BRANCH", "").strip() or "main"
    repo_text = f"{owner}/{repo}" if owner and repo else "(not configured; ask user for target owner/repo)"
    return (
        "GITHUB WORKFLOW RULES:\n"
        "- NON-NEGOTIABLE: when the user asks for a Glue Python Shell job, Phase A is NOT complete without a pushed feature branch. A successful scratch run alone is NOT the deliverable — the deliverable is (scratch SUCCEEDED) + (production code laid out per `project-structure`) + (branch created) + (files pushed). If you plan, run, and stop without the git steps, you have failed the task. Do not summarise Phase A as done until `github_push_files` has returned successfully.\n"
        f"- The target repo `{repo_text}` lives on GitHub. For ALL repo operations (branches, files, commits, PRs) use the `github_*` MCP tools. NEVER use `awsapi_call_aws` — there is no AWS CodeCommit involved.\n"
        f"- Every GitHub tool call MUST target `{repo_text}` exactly. Never a different owner or repo.\n"
        f"- Default branch: `{default_branch}`. Never commit directly to it. Never force-push. Never delete branches.\n"
        "- Feature branch naming: `agent/<short-conversation-slug>`. Call `github_list_branches` first to confirm the branch does not already exist; if it does, pick a new suffix.\n"
        "- Division of responsibilities: the agent creates Glue jobs ONLY in Phase A (scratch). In Phase B, the target repo's `deploy/deploy.py` (invoked by GitHub Actions) creates and updates Glue jobs from `glue-jobs.yaml`. Do NOT call `create-job` or `update-job` in Phase B.\n"
        "- Full lifecycle (do not skip or reorder phases):\n"
        "  PHASE A — scratch + commit:\n"
        "    A.1. Iterate on the job logic using a loose-script Glue job in the dev AWS account. The scratch Glue job name MUST be prefixed `scratch-<conversation-id>-`. Iterate until the scratch run reaches `SUCCEEDED`.\n"
        "    A.2. Activate the `project-structure` skill. Lay out the production code per its rules (including `pyproject.toml` dependency declarations), add/update the job's entry in `glue-jobs.yaml`, then `github_list_branches` -> `github_create_branch` from the default branch -> `github_push_files` in a single batch.\n"
        "    A.3. END THE TURN. Tell the user the feature branch has been pushed and ask them to resume once the GitHub Actions pipeline (`test` -> `build-wheels` -> `deploy`) has gone green. Do NOT poll or wait.\n"
        "  PHASE B — verify + PR (on resume):\n"
        "    B.1. Verify CI is green: `github_list_workflow_runs` filtered by the feature branch -> `github_get_workflow_run` for the latest run. If conclusion is not `success`, call `github_list_workflow_jobs` + `github_get_job_logs` for the failed job, surface the root-cause log lines, and stop. Do NOT touch the Glue job.\n"
        "    B.2. Look up the Glue job by the name in `glue-jobs.yaml` (it exists because `deploy.py` created or updated it). Run one `start-job-run`, poll `get-job-run` via `athena_manage_aws_glue_jobs` (NOT via `awsapi_call_aws` — the poll throttle only guards the MCP path), and proceed only if `SUCCEEDED`. If the job is missing, CI has not deployed yet — tell the user and stop. If the Glue run fails, call `glue_get_job_run_diagnostics`, surface the logs, and stop; do NOT open the PR.\n"
        "    B.3. `github_create_pull_request` against the default branch with a descriptive title and body summarising what the job does, the Glue job name, the job run ID, and a link to the green CI run. End the turn. Do NOT watch for merge.\n"
        "- Forbidden: force-push, branch deletion, direct commits to the default branch, opening a PR before Phase B's verification run has passed, creating or updating Glue jobs via MCP during Phase B.\n"
    )


def _build_ci_handoff_rules(ci_session_name: str) -> str:
    """
    Build code-interpreter handoff rules appended to system prompt.

    :param ci_session_name: Code interpreter session identifier
    :return: Prompt section for Athena-to-CI handoff
    """
    return (
        "IMPORTANT DATA HANDOFF RULES:\n"
        "- Never manually transcribe tabular rows from tool text into Python lists/dicts.\n"
        "- For Athena data that will be analyzed in code interpreter, first call athena_query_to_ci_csv.\n"
        "- Always pass `database` explicitly when calling athena_query_to_ci_csv.\n"
        "- Then use the code_interpreter tool to read the returned CSV path from the sandbox.\n"
        + f"- The code interpreter session name for this run is: {ci_session_name}\n"
    )


def _create_skills_plugin(skills_dir: Path):
    """
    Create a Strands AgentSkills plugin from local skill files.

    Args:
        skills_dir: Directory containing skills (each child has `SKILL.md`).

    Returns:
        AgentSkills plugin instance, or `None` when the directory is missing.
    """
    if not skills_dir.exists():
        return None
    return AgentSkills(skills=str(skills_dir))


def make_agent(
    profile: Optional[str],
    region: str,
    model_id: str,
    mcp_client: MCPClient,
    enable_code_interpreter: bool = True,
) -> tuple[Agent, Optional[str], Optional[AgentCoreCodeInterpreter]]:
    """
    Create the Strands Glue pipeline agent.

    One function is used by both local CLI mode and AgentCore runtime mode.

    :param profile: AWS profile name
    :param region: AWS region
    :param model_id: Bedrock model ID
    :param mcp_client: MCP client wrapper for Athena/Glue tools
    :param enable_code_interpreter: Whether to attach AgentCore code interpreter + CSV handoff tool
    :return: Agent, optional CI session name, optional code interpreter tool
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    model = BedrockModel(
        boto_session=session,
        model_id=model_id,
    )

    aws_api_mcp_client = make_aws_api_mcp_client()
    github_mcp_client = make_github_mcp_client()

    tools = [mcp_client, aws_api_mcp_client, github_mcp_client]
    prompt_parts = [SYSTEM_PROMPT]
    poll_interval_seconds = _get_glue_poll_interval_seconds(default_seconds=20.0)
    hooks = [GlueJobRunPollThrottleHook(min_interval_seconds=poll_interval_seconds)]
    skills_dir = Path(__file__).resolve().parent / "skills"
    skills_plugin = _create_skills_plugin(skills_dir)
    ci_session_name: Optional[str] = None
    code_interpreter_tool: Optional[AgentCoreCodeInterpreter] = None

    # Always include Glue runtime rules so both local CLI and AgentCore runtime
    # enforce the same job/role/trigger behavior.
    prompt_parts.append(_build_runtime_glue_rules())
    prompt_parts.append(_build_git_rules())

    if enable_code_interpreter:
        # Keep one explicit CI session name to simplify tool handoff and artifact extraction.
        ci_session_name = f"glue-pipeline-{uuid.uuid4().hex[:10]}"
        code_interpreter_tool = AgentCoreCodeInterpreter(
            region=region,
            session_name=ci_session_name,
        )

        athena_query_to_ci_csv = make_athena_query_to_ci_csv(
            session=session,
            region=region,
            code_interpreter_tool=code_interpreter_tool,
            ci_session_name=ci_session_name,
        )

        tools.extend([athena_query_to_ci_csv, code_interpreter_tool.code_interpreter])
        prompt_parts.append(_build_ci_handoff_rules(ci_session_name=ci_session_name))

    # Available in both local and runtime modes for failed-run debugging.
    glue_job_run_diagnostics = make_glue_job_run_diagnostics_tool(
        session=session,
        region=region,
    )
    tools.append(glue_job_run_diagnostics)

    agent_kwargs = {
        "model": model,
        "tools": tools,
        "system_prompt": "\n\n".join(prompt_parts),
        "hooks": hooks,
    }
    if skills_plugin is not None:
        agent_kwargs["plugins"] = [skills_plugin]

    agent = Agent(**agent_kwargs)

    return agent, ci_session_name, code_interpreter_tool
