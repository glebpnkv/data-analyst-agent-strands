"""Entrypoint for the data_analyst_agent FastAPI service.

Builds the per-session Strands agent (with its MCP clients and remote
sandbox session) and hands the wiring off to the shared `agent_server`
scaffold. uvicorn loads the module-level `app`.

Sandbox modes
-------------

The agent talks to its code-execution sandbox over HTTP. Two ways to
get a sandbox URL:

* **Pool mode (production).** Set `SANDBOX_CLUSTER_NAME`,
  `SANDBOX_TASK_DEFINITION_ARN`, `SANDBOX_SUBNET_IDS`, and
  `SANDBOX_SECURITY_GROUP_ID`. The framework spins up a `SandboxPool`
  that warms `SANDBOX_POOL_SIZE` (default 2) ECS tasks; each chat
  session claims one and stops it on session end.

* **Local mode (developer laptop).** Set `SANDBOX_LOCAL_URL` (e.g.
  `http://localhost:8081`) and `SANDBOX_AUTH_TOKEN`. All chat sessions
  share a single sandbox container — fine for one-developer local
  testing, NOT for multi-user use (kernel state collides). Run the
  container with `docker run -p 8081:8081 -e SANDBOX_AUTH_TOKEN=... data-analyst-sandbox:dev`.

If neither set is configured, the service refuses to start — there's no
silent "no code interpreter" mode in production because Athena→CSV→plot
is the agent's main job.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from agent_server import (
    ManagedAgent,
    PoolConfig,
    SandboxPool,
    StrandsEventReducer,
    create_app,
    discover_from_ecs_metadata,
    make_display_dataframe_tool,
    make_display_image_tool,
    make_display_plotly_tool,
)

# Mirror main.py: load .env from the agent dir for local dev.
AGENT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(AGENT_DIR / ".env")

# When running inside an ECS task we want boto3 to authenticate via the
# ECS container credential provider (= task role), NOT via a profile
# from a config file that doesn't exist in the container. Strip any
# inherited profile envs before any boto3 import path runs. Detection:
# ECS_CONTAINER_METADATA_URI is set by Fargate / EC2 ECS only.
if os.environ.get("ECS_CONTAINER_METADATA_URI") or os.environ.get("ECS_CONTAINER_METADATA_URI_V4"):
    for _stale in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SDK_LOAD_CONFIG"):
        if _stale in os.environ:
            os.environ.pop(_stale, None)

# `agent` is the sibling module agent/agent.py.
# Importable because uvicorn is launched with --app-dir on this directory.
from agent import make_agent, make_mcp_client  # noqa: E402

log = logging.getLogger(__name__)


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} env var is required")
    return value


def _resolve_sandbox() -> tuple[SandboxPool | None, str | None, str | None]:
    """Decide between local-mode (single fixed URL) and pool-mode (ECS).

    Returns `(pool, local_url, local_token)`. Exactly one of `pool`
    or `local_url+local_token` is populated; the other side is None.

    Pool-mode config sources (per field, first match wins):
      1. Explicit `SANDBOX_*` env var.
      2. Self-discovery from the agent's own ECS task metadata + boto3
         lookups (only when running inside an ECS task).
      3. Raise.

    Only `AWS_REGION` (or `AWS_DEFAULT_REGION`) is *always* required —
    every other pool field is auto-derivable from the running task's
    own ECS context when the agent runs in an ECS task. Local
    development still needs explicit `SANDBOX_LOCAL_URL` +
    `SANDBOX_AUTH_TOKEN`.
    """
    local_url = (os.environ.get("SANDBOX_LOCAL_URL") or "").strip()
    if local_url:
        token = (os.environ.get("SANDBOX_AUTH_TOKEN") or "").strip()
        if not token:
            raise RuntimeError(
                "SANDBOX_LOCAL_URL is set but SANDBOX_AUTH_TOKEN is empty. "
                "Local dev requires both."
            )
        log.warning(
            "sandbox local-mode: pointing all sessions at %s. "
            "Kernel state is shared — do NOT use this for multi-user testing.",
            local_url,
        )
        return None, local_url, token

    # Region for the pool's boto3 clients (and for self-discovery's own
    # boto3 lookups below). Falls through AWS_REGION -> AWS_DEFAULT_REGION;
    # if neither is set, fail loud here rather than at first refill with
    # a deferred NoRegionError.
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    ).strip()
    if not region:
        raise RuntimeError(
            "AWS_REGION (or AWS_DEFAULT_REGION) must be set for the sandbox pool's "
            "ECS / EC2 boto3 clients."
        )

    # Attempt self-discovery from ECS task metadata. Returns None if we're
    # not in an ECS task (= local dev attempting pool mode); returns a
    # (possibly empty) dict otherwise. Any individual lookup that fails is
    # silently absent from the dict, so explicit env vars still work as
    # the last word for any field discovery couldn't fill.
    discovered = discover_from_ecs_metadata(region) or {}

    def _env_or_discovered(env_name: str, discovered_key: str) -> str:
        v = (os.environ.get(env_name) or "").strip()
        if v:
            return v
        v = (discovered.get(discovered_key) or "").strip() if isinstance(
            discovered.get(discovered_key), str
        ) else (discovered.get(discovered_key) or "")
        if not v:
            raise RuntimeError(
                f"{env_name} not set and could not be auto-discovered from ECS task "
                f"metadata. Set it explicitly, or run the agent inside an ECS task "
                f"so this can be derived from `ECS_CONTAINER_METADATA_URI_V4` + "
                f"ecs:DescribeTasks + ec2:DescribeNetworkInterfaces. "
                f"Alternatively for local development, set SANDBOX_LOCAL_URL + "
                f"SANDBOX_AUTH_TOKEN to use a single fixed sandbox URL."
            )
        return v

    cluster_name = _env_or_discovered("SANDBOX_CLUSTER_NAME", "cluster_name")
    task_definition_arn = _env_or_discovered(
        "SANDBOX_TASK_DEFINITION_ARN", "task_definition_arn"
    )
    security_group_id = _env_or_discovered(
        "SANDBOX_SECURITY_GROUP_ID", "security_group_id"
    )

    # Subnet ids need list handling, so don't go through _env_or_discovered.
    subnet_ids_raw = (os.environ.get("SANDBOX_SUBNET_IDS") or "").strip()
    if subnet_ids_raw:
        subnet_ids = [s.strip() for s in subnet_ids_raw.split(",") if s.strip()]
    else:
        subnet_ids = list(discovered.get("subnet_ids") or [])
    if not subnet_ids:
        raise RuntimeError(
            "SANDBOX_SUBNET_IDS not set and could not be auto-discovered from ECS "
            "task metadata. Set it explicitly, or run the agent inside an ECS task."
        )

    # container_name has a sensible default (`sandbox`, matching the
    # dev-environment task def) and only matters when the override targets
    # a container that exists in the task definition. Discovery still helps
    # in Flavour 1 setups where the agent task's container is named
    # something other than `sandbox`.
    container_name = (
        (os.environ.get("SANDBOX_CONTAINER_NAME") or "").strip()
        or discovered.get("container_name")
        or "sandbox"
    )

    config = PoolConfig(
        cluster_name=cluster_name,
        task_definition_arn=task_definition_arn,
        subnet_ids=subnet_ids,
        security_group_id=security_group_id,
        region=region,
        pool_size=int(os.environ.get("SANDBOX_POOL_SIZE") or "2"),
        sandbox_port=int(os.environ.get("SANDBOX_PORT") or "8081"),
        container_name=container_name,
    )
    return SandboxPool(config), None, None


_pool, _local_url, _local_token = _resolve_sandbox()


async def _build_managed_agent(session_id: str) -> ManagedAgent:
    """Async factory called by SessionRegistry on first message of a session.

    Order:
      1. Claim a sandbox task (or pick up the local URL).
      2. Build the MCP + agent stack.
      3. Wire host-side display loaders to the sandbox client.
      4. Return a ManagedAgent whose teardown both stops MCP and releases
         the sandbox task back to the pool (= StopTask).
    """
    region = _required_env("AWS_REGION")
    model_id = _required_env("MODEL_ID")
    profile = os.environ.get("AWS_PROFILE") or None

    # Claim a sandbox first — if this fails we don't want to leave a half-built
    # MCP subprocess hanging around.
    if _pool is not None:
        claimed = await _pool.claim()
        sandbox_http_url = claimed.http_url
        sandbox_auth_token = claimed.auth_token
    else:
        claimed = None
        sandbox_http_url = _local_url
        sandbox_auth_token = _local_token

    # MCP client uses subprocess+stdio; building it can fail (binary not
    # installed, etc), so it goes inside the try/except where we know how
    # to release the sandbox if anything below explodes.
    mcp_client = make_mcp_client()
    try:
        agent, ci_session_name, code_interpreter_tool = make_agent(
            profile=profile,
            region=region,
            model_id=model_id,
            mcp_client=mcp_client,
            sandbox_http_url=sandbox_http_url,
            sandbox_auth_token=sandbox_auth_token,
        )

        # Host-side display tool loaders. They run in this FastAPI worker
        # process (NOT in the LLM context), reading the sandbox files via
        # HTTP and emitting `ui.*` events. Bytes never round-trip through
        # the model.
        if code_interpreter_tool is not None:
            sandbox_text_loader = code_interpreter_tool.read_text_file
            sandbox_image_loader = code_interpreter_tool.read_binary_file_as_data_url
        else:
            sandbox_text_loader = None
            sandbox_image_loader = None

        for display_tool in (
            make_display_dataframe_tool(sandbox_text_loader=sandbox_text_loader),
            make_display_plotly_tool(sandbox_text_loader=sandbox_text_loader),
            make_display_image_tool(sandbox_image_loader=sandbox_image_loader),
        ):
            agent.tool_registry.process_tools([display_tool])
    except Exception:
        _safe_stop(mcp_client)
        if claimed is not None and _pool is not None:
            try:
                await _pool.release(claimed)
            except Exception as e:  # noqa: BLE001
                log.warning("failed to release sandbox after agent build error: %s", e)
        raise

    log.info("built agent for session %s (sandbox=%s)", session_id, sandbox_http_url)

    async def _teardown() -> None:
        _safe_stop(mcp_client)
        if claimed is not None and _pool is not None:
            try:
                await _pool.release(claimed)
            except Exception as e:  # noqa: BLE001
                log.warning("sandbox release on teardown raised: %s", e)

    return ManagedAgent(agent=agent, teardown=_teardown)


def _safe_stop(mcp_client) -> None:
    try:
        mcp_client.stop(None, None, None)
    except Exception as e:
        log.warning("MCP client stop raised: %s", e)


# `lifespan_resources` lets the framework start/stop the SandboxPool
# alongside FastAPI's own lifespan. Pool warms before the first request
# lands and stops every claimed/idle task on shutdown.
_lifespan_resources = [_pool] if _pool is not None else []

app = create_app(
    agent_factory=_build_managed_agent,
    reducer_factory=StrandsEventReducer,
    title="data-analyst-agent-strands",
    version="0.1.0",
    lifespan_resources=_lifespan_resources,
)
