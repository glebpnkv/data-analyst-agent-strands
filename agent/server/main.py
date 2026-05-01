"""Entrypoint for the data_analyst_agent FastAPI service.

Builds the per-session Strands agent (with its MCP clients and code
interpreter sandbox) and hands the wiring off to the shared `agent_server`
scaffold. uvicorn loads the module-level `app`.
"""

import base64
import logging
import os
import shlex
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent_server import (
    ManagedAgent,
    StrandsEventReducer,
    create_app,
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

# Packages the sandbox needs upgraded so its outputs deserialize cleanly
# in our frontend. AgentCore's bundled environment can lag — for example
# plotly < 6 emits removed trace types (heatmapgl) that our plotly 6.x
# rejects. Install at session init; failure is non-fatal.
SANDBOX_PIP_UPGRADES = ["plotly"]


def _build_managed_agent(session_id: str) -> ManagedAgent:
    region = _required_env("AWS_REGION")
    model_id = _required_env("MODEL_ID")
    profile = os.environ.get("AWS_PROFILE") or None

    # Match agents/.../main.py: do NOT enter the MCP context manually.
    # Strands' add_tool path starts the client during agent construction;
    # entering it beforehand triggers a double-start error.
    mcp_client = make_mcp_client()
    try:
        agent, ci_session_name, code_interpreter_tool = make_agent(
            profile=profile,
            region=region,
            model_id=model_id,
            mcp_client=mcp_client,
            enable_code_interpreter=True,
        )
        # Make the v1 ui.* protocol available to this agent. The tools
        # pull the per-request UIEmitter from a ContextVar set by the
        # FastAPI streaming handler, so they're safe to attach once
        # per session and reuse across requests.
        if code_interpreter_tool is not None and ci_session_name is not None:
            sandbox_text_loader = _make_sandbox_text_loader(code_interpreter_tool, ci_session_name)
            sandbox_image_loader = _make_sandbox_image_loader(code_interpreter_tool, ci_session_name)
        else:
            sandbox_text_loader = None
            sandbox_image_loader = None
        for display_tool in (
            make_display_dataframe_tool(sandbox_text_loader=sandbox_text_loader),
            make_display_plotly_tool(sandbox_text_loader=sandbox_text_loader),
            make_display_image_tool(sandbox_image_loader=sandbox_image_loader),
        ):
            agent.tool_registry.process_tools([display_tool])

        if code_interpreter_tool is not None and ci_session_name is not None:
            _upgrade_sandbox_packages(code_interpreter_tool, ci_session_name)
    except Exception:
        _safe_stop(mcp_client)
        raise

    log.info("built agent for session %s", session_id)
    return ManagedAgent(agent=agent, teardown=lambda: _safe_stop(mcp_client))


def _safe_stop(mcp_client) -> None:
    try:
        mcp_client.stop(None, None, None)
    except Exception as e:
        log.warning("MCP client stop raised: %s", e)


def _get_agentcore_client(code_interpreter_tool, ci_session_name: str) -> Any:
    """Reach into AgentCoreCodeInterpreter._sessions to get the underlying
    bedrock_agentcore CodeInterpreter SDK client.

    WHY BYPASS THE WRAPPER:
        strands_tools.AgentCoreCodeInterpreter wraps the bedrock_agentcore
        CodeInterpreter and routes every response through
        `_create_tool_result`, which does
            "content": [{"text": str(result.get("content"))}]
        (see .venv/.../code_interpreter/agent_core_code_interpreter.py:489).
        AgentCore returns structured payloads (e.g. lists of typed text
        blocks); str() emits a Python repr with single quotes that fails
        json.loads on JSON files and is indistinguishable from garbage for
        base64. The underlying client has clean methods like
        `download_file()` that parse the response stream properly.

    WHY `_sessions` (private attribute):
        AgentCoreCodeInterpreter holds a name -> session-handle dict in
        `self._sessions`. There is no public accessor today. If Strands
        renames or restructures it, this function raises AttributeError on
        the first sandbox read with the message below — easy to find and
        fix in one place. The trade-off is being insulated from the str()
        bug that materially breaks display_dataframe / display_plotly /
        display_image.
    """
    sessions = getattr(code_interpreter_tool, "_sessions", None)
    if sessions is None:
        raise RuntimeError(
            "AgentCoreCodeInterpreter has no `_sessions` attribute — strands_tools "
            "API may have changed. Update _get_agentcore_client in "
            "agent/server/main.py."
        )

    handle = sessions.get(ci_session_name)
    if handle is None:
        # The wrapper creates sessions lazily on first invoke. Trigger via
        # any cheap public method, then re-look-up.
        from strands_tools.code_interpreter.models import ListFilesAction

        code_interpreter_tool.list_files(
            ListFilesAction(
                type="listFiles",
                session_name=ci_session_name,
                path="/",
            )
        )
        handle = sessions.get(ci_session_name)

    if handle is None or not hasattr(handle, "client"):
        raise RuntimeError(
            f"could not access AgentCore client for session {ci_session_name!r} — "
            "session handle missing or shape changed"
        )
    return handle.client


def _read_sandbox_file_bytes(client, sandbox_path: str) -> bytes:
    """Read a file from the AgentCore sandbox as raw bytes.

    The bedrock_agentcore SDK's `download_file()` (.venv/.../code_interpreter_client.py:600)
    returns at the FIRST content_item in the FIRST stream event and prefers
    `resource.text` over `resource.blob`. Both are wrong for our use:
        1. AgentCore can split large responses across multiple stream events
           and / or multiple content items per event — early-return drops
           everything after the first chunk. (Symptom: a 50KB PNG arrives as
           ~95 bytes; the rest is on the floor.)
        2. Binary files come through as `blob` (base64). When a leading
           text-shaped item is also present (preview, metadata) the SDK
           returns that text and never reads the blob.

    This walker invokes readFiles ourselves and concatenates every blob
    across the whole stream. If only text fields appear (genuine text
    file), they are concatenated and UTF-8-encoded.
    """
    response = client.invoke("readFiles", {"paths": [sandbox_path]})
    if "stream" not in response:
        raise FileNotFoundError(
            f"unexpected readFiles response shape for {sandbox_path!r}: {response}"
        )

    blob_chunks: list[bytes] = []
    text_chunks: list[str] = []
    saw_resource = False

    # Diagnostics: count what we actually receive so we can prove whether
    # we're walking a chunked stream or AgentCore is genuinely returning
    # this little.
    event_count = 0
    item_count = 0
    skipped_item_types: list[str] = []

    for event in response["stream"]:
        event_count += 1
        result = event.get("result") if isinstance(event, dict) else None
        if not result:
            log.debug("readFiles stream event without result: keys=%s", list(event.keys()) if isinstance(event, dict) else type(event).__name__)
            continue
        items = result.get("content", []) or []
        log.debug("readFiles event %d: %d content item(s)", event_count, len(items))
        for item in items:
            item_count += 1
            if not isinstance(item, dict):
                skipped_item_types.append(type(item).__name__)
                continue
            item_type = item.get("type", "<no type>")
            if item_type != "resource":
                skipped_item_types.append(item_type)
                log.debug("readFiles item type=%s (skipped); keys=%s", item_type, list(item.keys()))
                continue
            resource = item.get("resource") or {}
            saw_resource = True
            blob = resource.get("blob")
            text = resource.get("text")
            log.debug(
                "readFiles resource: keys=%s, blob_len=%s, text_len=%s, mimeType=%s, uri=%s",
                list(resource.keys()),
                len(blob) if isinstance(blob, str) else None,
                len(text) if isinstance(text, str) else None,
                resource.get("mimeType"),
                resource.get("uri"),
            )
            if blob:
                blob_chunks.append(base64.b64decode(blob))
                continue
            if text:
                text_chunks.append(str(text))

    log.info(
        "readFiles walk: events=%d, items=%d, blob_chunks=%d (%d bytes), text_chunks=%d (%d chars), skipped=%s",
        event_count,
        item_count,
        len(blob_chunks),
        sum(len(b) for b in blob_chunks),
        len(text_chunks),
        sum(len(t) for t in text_chunks),
        skipped_item_types or "none",
    )

    if not saw_resource:
        raise FileNotFoundError(f"file not present in readFiles response: {sandbox_path!r}")

    if blob_chunks:
        return b"".join(blob_chunks)
    return "".join(text_chunks).encode("utf-8")


def _make_sandbox_text_loader(code_interpreter_tool, ci_session_name: str):
    """Build a SandboxTextLoader that reads UTF-8 text files from the sandbox.

    Bypasses both strands_tools' str()-on-content bug AND the
    bedrock_agentcore SDK's truncating download_file (see
    _read_sandbox_file_bytes for the full story).
    """
    max_bytes = 10 * 1024 * 1024

    def load(sandbox_path: str) -> str:
        client = _get_agentcore_client(code_interpreter_tool, ci_session_name)
        raw = _read_sandbox_file_bytes(client, sandbox_path)
        log.info("sandbox text read: %r -> %d bytes", sandbox_path, len(raw))
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise RuntimeError(
                f"sandbox file is not UTF-8 text: {sandbox_path!r}"
            ) from e
        if not content:
            raise RuntimeError(f"sandbox returned empty content for {sandbox_path!r}")
        if len(content) > max_bytes:
            raise RuntimeError(
                f"sandbox file too large to inline ({len(content)} bytes); "
                "downsample or chunk before calling display_*"
            )
        return content

    return load


def _run_sandbox_command(client, command: str) -> str:
    """Execute a shell command in the sandbox and return its stdout.

    Calls invoke("executeCommand", ...) on the underlying SDK client
    directly. We pull stdout out of every text-shaped content item
    across every stream event so we don't repeat the SDK's
    "first chunk wins" bug pattern — and so we don't go through
    strands_tools.AgentCoreCodeInterpreter.execute_command which
    would str()-mangle the response (see _get_agentcore_client).
    """
    response = client.invoke("executeCommand", {"command": command})
    if "stream" not in response:
        raise RuntimeError(f"unexpected executeCommand response shape: {response}")

    text_chunks: list[str] = []
    is_error = False
    for event in response["stream"]:
        if not isinstance(event, dict):
            continue
        result = event.get("result") or {}
        if result.get("isError"):
            is_error = True
        for item in result.get("content", []) or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if text is not None:
                text_chunks.append(str(text))

    output = "".join(text_chunks)
    if is_error:
        raise RuntimeError(f"sandbox command failed ({command!r}): {output[:500]}")
    return output


def _make_sandbox_image_loader(code_interpreter_tool, ci_session_name: str):
    """Build a SandboxImageLoader closure over the AgentCore CI handle.

    Reads the binary file via `base64 -w 0 <path>` over executeCommand
    (NOT readFiles). Empirically, AgentCore's readFiles truncates blob
    responses for binary files (we saw a 53,104-byte PNG come back as
    101 bytes with no chunking and no skipped items — the tiny payload
    is the whole response). executeCommand's stdout path returns the
    full file as a clean base64 string.

    Bytes never enter the model context.
    """
    max_encoded_bytes = 10 * 1024 * 1024  # ~7.5MB raw

    def load(sandbox_path: str, mime: str) -> str:
        client = _get_agentcore_client(code_interpreter_tool, ci_session_name)
        command = f"base64 -w 0 -- {shlex.quote(sandbox_path)}"
        encoded = _run_sandbox_command(client, command).strip()
        if not encoded:
            raise RuntimeError(f"sandbox returned empty base64 for {sandbox_path!r}")
        log.info(
            "sandbox image read: %r -> %d base64 bytes (~%d raw, mime=%s)",
            sandbox_path,
            len(encoded),
            (len(encoded) * 3) // 4,
            mime,
        )
        if len(encoded) > max_encoded_bytes:
            raise RuntimeError(
                f"sandbox image too large to inline ({len(encoded)} base64 bytes); "
                "save and reference via S3 URL instead"
            )
        return f"data:{mime};base64,{encoded}"

    return load


def _upgrade_sandbox_packages(code_interpreter_tool, ci_session_name: str) -> None:
    """Upgrade pinned packages in the AgentCore Code Interpreter sandbox.

    Uses the underlying SDK client's `install_packages` so we don't go
    through strands_tools' lossy str()-on-content path. Best-effort: a
    failed install (no internet egress, etc.) only logs and keeps the
    rest of the pipeline working.
    """
    if not SANDBOX_PIP_UPGRADES:
        return
    try:
        client = _get_agentcore_client(code_interpreter_tool, ci_session_name)
        client.install_packages(SANDBOX_PIP_UPGRADES, upgrade=True)
        log.info("upgraded sandbox packages: %s", ", ".join(SANDBOX_PIP_UPGRADES))
    except Exception as e:
        log.warning("sandbox package upgrade failed: %s", e)


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} env var is required")
    return value


app = create_app(
    agent_factory=_build_managed_agent,
    reducer_factory=StrandsEventReducer,
    title="strands-glue-pipeline-agent",
    version="0.1.0",
)
