"""AgentCore Runtime entrypoint for the hackathon data-analyst-agent.

Tools wired:
  - `code_interpreter` (from strands_tools): the AgentCore Code Interpreter
    sandbox. Persistent per AgentCore session, auto-reconnects.
  - `list_s3_dataset` / `load_s3_into_sandbox` / `save_sandbox_to_s3`:
    thin closures that bridge the three hackathon S3 buckets to the
    sandbox workspace. The sandbox itself has no S3 perms — we run
    boto3 here in the runtime container (which has the role) and push
    bytes into the sandbox via the raw `CodeInterpreter.upload_file`
    API, which base64-encodes binary content automatically. That keeps
    parquet / Excel uploads working without ceremony in the LLM prompt.

Streaming: SSE events forwarded as `{delta: ...}` for text and
`{tool_use: {name, id}}` for tool invocations. Tool-use events are
deduped by `toolUseId` so the frontend sees one badge per call,
not one per streamed input token.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import uuid
import zipfile
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from botocore.exceptions import ClientError
from strands import Agent, tool
from strands.models import BedrockModel
from strands_tools.code_interpreter.agent_core_code_interpreter import (
    AgentCoreCodeInterpreter,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger(__name__)

REGION = os.environ.get("REGION", "us-east-1")
# `us.anthropic.*` is a cross-region inference profile, not a bare model
# id. Sonnet 4.5 in us-east-1 only accepts inference-profile IDs — calling
# the bare model id returns `Invocation ... with on-demand throughput isn't
# supported`. Override via env to swap models without rebuilding.
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
BUCKET_RAW = os.environ.get("BUCKET_RAW", "<unset>")
BUCKET_PROCESSED = os.environ.get("BUCKET_PROCESSED", "<unset>")
BUCKET_GOLD = os.environ.get("BUCKET_GOLD", "<unset>")

# Map the LLM-facing tier name to the actual bucket. Restrict writes to
# the buckets where writing is semantically meaningful — `raw` is for
# user uploads only, so the agent doesn't write back into it.
_BUCKETS_BY_TIER = {
    "raw": BUCKET_RAW,
    "processed": BUCKET_PROCESSED,
    "gold": BUCKET_GOLD,
}
_WRITABLE_TIERS = {"processed", "gold"}

# Lambda naming — both the runtime's IAM scope and the host-side
# deployer in the frontend use this prefix.
LAMBDA_NAME_PREFIX = "aiagent-lambda-"

# Pipeline-spec staging area inside the processed bucket. The agent
# CANNOT call lambda:CreateFunction or iam:CreateRole from the runtime —
# its execution role's workshop-boundary explicitly denies iam:* and
# only allows lambda:InvokeFunction. So `deploy_pipeline_as_lambda`
# writes a spec under this prefix and returns; the Chainlit frontend,
# running locally under WSParticipantRole (which DOES have those perms
# for aiagent-lambda-*), polls this prefix after each turn and does
# the actual CreateFunction. After deploy the spec is moved to
# PIPELINE_ACTIVE_PREFIX so `list_pipelines` can surface what's live
# without touching lambda:ListFunctions (also boundary-blocked).
PIPELINE_PENDING_PREFIX = "_pipelines/pending/"
PIPELINE_ACTIVE_PREFIX = "_pipelines/active/"

# Image-display rendezvous. The agent's `display_image` tool drops a
# PNG/SVG/etc into this prefix; the Chainlit frontend sweeps it after
# every turn and renders each as an inline image element, then cleans
# up. Same S3-as-IPC pattern as the pipeline deploys above.
DISPLAY_PENDING_PREFIX = "_display/"
_IMAGE_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "webp": "image/webp",
}

# Deployed pipelines have only the python3.12 stdlib + boto3 (the
# AWS-managed pandas layer is gated by a resource policy that blocks
# our workshop account from GetLayerVersion). Pandas work happens in
# the AgentCore Code Interpreter sandbox; pipelines do CSV-in / CSV-out
# with stdlib `csv` + boto3.

SYSTEM_PROMPT = f"""\
You are a data-analyst assistant for a hackathon demo. The user can ask
you to explore datasets and propose data pipelines.

You work with three S3 buckets in {REGION}:
  - raw       ({BUCKET_RAW}):       pristine uploads (CSV, Excel) — never modify
  - processed ({BUCKET_PROCESSED}): cleaned / normalised intermediates
  - gold      ({BUCKET_GOLD}):      final curated datasets ready for analysis

Tools available:
  - `code_interpreter`: an isolated Python sandbox (pandas / numpy /
    matplotlib / pyarrow pre-installed). Use it for any code execution.
    The sandbox has NO direct S3 or internet access — use the S3 tools
    below to bridge files in and out.
  - `list_s3_dataset(tier)`: list objects in raw/processed/gold.
  - `load_s3_into_sandbox(tier, key, local_filename=None)`: download an
    S3 object into the sandbox workspace (binary-safe — parquet, Excel,
    and CSV all work). Returns the workspace path you can then read
    with pandas. Call this BEFORE asking the sandbox to read a dataset.
  - `save_sandbox_to_s3(local_filename, tier, key)`: push a sandbox file
    back to S3. Only `processed` and `gold` are writable.
  - `display_image(workspace_path, caption="")`: show a STATIC image
    (PNG / JPG / SVG) to the user. Use after `plt.savefig(...)`.
  - `display_plotly(workspace_path, caption="")`: show an INTERACTIVE
    Plotly chart. Save the figure as JSON in the sandbox first via
    `fig.write_json('plots/chart.json')` (or `open(...).write(fig.to_json())`)
    then call this with the path. Prefer this over `display_image` for
    plots, since the user can hover / zoom / pan.

  IMPORTANT: saving a file to the sandbox alone won't make it appear
  in the chat — you MUST call `display_image` or `display_plotly` to
  surface it. Pick `display_plotly` whenever you can (better UX).
  - `deploy_pipeline_as_lambda(name, code, description="")`: QUEUE a
    Python pipeline for deployment. The code must define
    `def handler(event, context)`. The deployed Lambda has only the
    python3.12 stdlib + boto3 (no pandas). Bucket names will be
    available inside the handler as BUCKET_RAW / BUCKET_PROCESSED /
    BUCKET_GOLD env vars. Important: this tool returns `status:queued`,
    not `created`. Your execution role cannot itself call CreateFunction
    or any IAM action — those are done by the host-side deployer
    (Chainlit), which picks the spec up from S3 within seconds of your
    turn ending and posts a confirmation message in the chat. Tell the
    user "I've queued the pipeline — the frontend will deploy it now".
    Do NOT immediately try to invoke the pipeline after deploying;
    wait for the user to confirm, or to ask you to test it.
  - `invoke_pipeline(name, payload=None)`: invoke an already-deployed
    pipeline. Only call this after the user has confirmed the deploy.
  - `list_pipelines()`: list pipelines currently deployed in the
    account (reads the host-deployer's active-manifest in the processed
    bucket).

EDA workflow: list the relevant bucket, load the dataset into the
sandbox, explore (column types, summary stats, distributions, missing
values, correlations), plot when useful and save plots as PNG files.
For pipelines: prototype the transformation logic in the sandbox first
so you know it works on the data, THEN package the validated code as a
Lambda via deploy_pipeline_as_lambda.
"""

app = BedrockAgentCoreApp()

# Cache one Strands Agent per AgentCore session id so multi-turn chats
# keep their conversation history. The Agent holds state internally;
# we just key it by session.
_agents: dict[str, Agent] = {}


def _ensure_ci_client(interpreter: AgentCoreCodeInterpreter):
    """Return the raw CI client for the interpreter's default session,
    initialising the session if it hasn't been used yet.

    The Strands wrapper lazily creates an AgentCore CI session on first
    `code_interpreter` tool call from the LLM. Our S3 bridge tools need
    the underlying client (for binary-safe `upload_file`) and may be
    invoked before the LLM has touched the sandbox, so we trigger the
    same lazy-init path explicitly via the wrapper's `_ensure_session`
    helper. Single-underscore protected method, but it IS the canonical
    "make sure this session exists" entry point in this SDK.
    """
    interpreter._ensure_session(interpreter.default_session)
    return interpreter._sessions[interpreter.default_session].client


def _resolve_tier(tier: str, writable: bool = False) -> str:
    """LLM-facing tier name → bucket name. Raises if unknown or unwritable."""
    tier_clean = (tier or "").strip().lower()
    if tier_clean not in _BUCKETS_BY_TIER:
        raise ValueError(
            f"unknown tier {tier!r}; expected one of {sorted(_BUCKETS_BY_TIER)}"
        )
    if writable and tier_clean not in _WRITABLE_TIERS:
        raise ValueError(
            f"tier {tier_clean!r} is read-only; writable tiers are {sorted(_WRITABLE_TIERS)}"
        )
    return _BUCKETS_BY_TIER[tier_clean]


def _build_agent(session_id: str) -> Agent:
    """Build a Strands Agent with code-interpreter + S3 + Lambda tools."""
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    interpreter = AgentCoreCodeInterpreter(
        region=REGION,
        session_name=session_id,
    )
    # boto3 clients created once per agent so all tool calls share the
    # same connection pool. AgentCore Runtime resolves creds from the
    # task role automatically — no profile needed inside the container.
    # Only `s3` and `lambda` are used here: workshop-boundary denies
    # iam:* and all lambda actions other than InvokeFunction, so the
    # deploy step has to be done by the Chainlit host (see the
    # pipeline_pending prefix in the processed bucket).
    s3 = boto3.client("s3", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)

    @tool
    def list_s3_dataset(tier: str, prefix: str = "") -> dict[str, Any]:
        """List objects in one of the three data buckets.

        Use this to discover available datasets before loading them
        into the sandbox.

        Args:
            tier: One of "raw", "processed", "gold".
            prefix: Optional key prefix to narrow the listing.

        Returns:
            A dict with `bucket`, `tier`, and `objects` (a list of
            `{key, size, last_modified}` items).
        """
        bucket = _resolve_tier(tier)
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        return {
            "tier": tier,
            "bucket": bucket,
            "objects": [
                {
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                }
                for obj in resp.get("Contents", [])
            ],
        }

    @tool
    def load_s3_into_sandbox(
        tier: str, key: str, local_filename: str | None = None
    ) -> dict[str, Any]:
        """Copy an S3 object into the code-interpreter sandbox workspace.

        Binary-safe — parquet, Excel, and CSV all work. The file is
        placed at the workspace root unless `local_filename` includes
        subdirectories. After this returns, the sandbox can read the
        file with normal pandas calls, e.g. `pd.read_parquet(path)`.

        Args:
            tier: One of "raw", "processed", "gold".
            key: The S3 object key (e.g. "demo/sales.parquet").
            local_filename: Optional override for the sandbox filename;
                defaults to the basename of `key`.

        Returns:
            A dict with `workspace_path`, `size_bytes`, and `source_s3_uri`.
        """
        bucket = _resolve_tier(tier)
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        filename = local_filename or key.rsplit("/", 1)[-1]
        client = _ensure_ci_client(interpreter)
        # Sniff: if the bytes are valid UTF-8 we ship them as `text` —
        # the sandbox writes the file as plain text and pandas can read
        # it directly with `pd.read_csv(...)`. If decoding fails it's
        # genuinely binary (parquet, Excel, image) and we send bytes,
        # which the CI client base64-encodes into the `blob` field. The
        # CI service decodes the blob on landing, so the file in the
        # workspace is the correct binary either way.
        try:
            content: bytes | str = body.decode("utf-8")
        except UnicodeDecodeError:
            content = body
        client.upload_file(path=filename, content=content)
        return {
            "workspace_path": filename,
            "size_bytes": len(body),
            "source_s3_uri": f"s3://{bucket}/{key}",
            "uploaded_as": "text" if isinstance(content, str) else "binary",
        }

    def _stage_display(
        kind: str,
        workspace_path: str,
        caption: str,
        content_type_default: str,
    ) -> dict[str, Any]:
        """Shared body for the display_* tools.

        Both display_image and display_plotly land in the same S3
        rendezvous — they only differ in the `type` field on the
        manifest, which the Chainlit dispatcher uses to pick the right
        Chainlit element class (cl.Image vs cl.Plotly).
        """
        client = _ensure_ci_client(interpreter)
        content = client.download_file(workspace_path)
        if isinstance(content, str):
            content = content.encode("utf-8")
        filename = workspace_path.rsplit("/", 1)[-1] or f"{kind}.bin"
        if kind == "image":
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            content_type = _IMAGE_CONTENT_TYPES.get(ext, content_type_default)
        else:
            content_type = content_type_default
        display_id = uuid.uuid4().hex[:12]
        payload_key = f"{DISPLAY_PENDING_PREFIX}{display_id}/{filename}"
        meta_key = f"{DISPLAY_PENDING_PREFIX}{display_id}/meta.json"
        s3.put_object(
            Bucket=BUCKET_PROCESSED,
            Key=payload_key,
            Body=content,
            ContentType=content_type,
        )
        s3.put_object(
            Bucket=BUCKET_PROCESSED,
            Key=meta_key,
            Body=json.dumps(
                {
                    "type": kind,           # "image" | "plotly"
                    "payload_key": payload_key,
                    "filename": filename,
                    "caption": caption,
                    "content_type": content_type,
                    "queued_at": int(time.time()),
                },
                indent=2,
            ).encode("utf-8"),
            ContentType="application/json",
        )
        return {
            "display_s3_uri": f"s3://{BUCKET_PROCESSED}/{payload_key}",
            "kind": kind,
            "status": "queued",
            "note": "The frontend will render this in the chat shortly.",
        }

    @tool
    def display_image(workspace_path: str, caption: str = "") -> dict[str, Any]:
        """Show a STATIC image (PNG / JPG / SVG) in the chat.

        Call after `plt.savefig('plots/distribution.png')` or similar.
        For interactive Plotly charts, use `display_plotly` instead —
        it gives the user hover / zoom / pan.

        Args:
            workspace_path: Sandbox path to the image file.
            caption: Optional one-line caption shown below the image.

        Returns:
            Dict with `display_s3_uri` and `status: queued`.
        """
        return _stage_display(
            kind="image",
            workspace_path=workspace_path,
            caption=caption,
            content_type_default="application/octet-stream",
        )

    @tool
    def display_plotly(workspace_path: str, caption: str = "") -> dict[str, Any]:
        """Show an INTERACTIVE Plotly chart in the chat.

        First save the figure as JSON inside the sandbox, e.g.
            fig.write_json('plots/chart.json')
        or
            with open('plots/chart.json', 'w') as f:
                f.write(fig.to_json())
        then call this with the path. The frontend deserialises it back
        into a `plotly.graph_objects.Figure` and renders it via
        `cl.Plotly`, so the user gets the standard Plotly toolbar.

        Args:
            workspace_path: Sandbox path to the figure JSON file.
            caption: Optional one-line caption shown below the chart.

        Returns:
            Dict with `display_s3_uri` and `status: queued`.
        """
        return _stage_display(
            kind="plotly",
            workspace_path=workspace_path,
            caption=caption,
            content_type_default="application/json",
        )

    @tool
    def save_sandbox_to_s3(
        local_filename: str, tier: str, key: str
    ) -> dict[str, Any]:
        """Upload a sandbox-workspace file to S3.

        Only `processed` and `gold` are writable. Use this after the
        sandbox has produced a transformed dataset, plot, or report.

        Args:
            local_filename: Path to the file inside the sandbox workspace
                (e.g. "summary.parquet" or "plots/distribution.png").
            tier: One of "processed", "gold".
            key: Destination S3 object key.

        Returns:
            A dict with `s3_uri` and `size_bytes`.
        """
        bucket = _resolve_tier(tier, writable=True)
        client = _ensure_ci_client(interpreter)
        # download_file on the raw CI client returns bytes for binary.
        result = client.download_file(path=local_filename)
        content = result.get("content")
        if isinstance(content, str):
            content = content.encode("utf-8")
        s3.put_object(Bucket=bucket, Key=key, Body=content)
        return {
            "s3_uri": f"s3://{bucket}/{key}",
            "size_bytes": len(content),
        }

    # ---- Lambda-as-pipeline tools ----------------------------------------

    def _sanitise_pipeline_name(name: str) -> str:
        """LLM-supplied name → a Lambda/IAM-safe suffix.

        Both Lambda function names and IAM role names accept only
        `[a-zA-Z0-9_-]`. We keep at most 32 chars after the prefix so
        the full `aiagent-lambda-...` name stays inside the 64-char
        Lambda function-name limit and the 64-char IAM role-name limit.
        """
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_-")[:32]
        if not cleaned:
            raise ValueError(f"pipeline name {name!r} sanitises to empty")
        return cleaned

    @tool
    def deploy_pipeline_as_lambda(
        name: str, code: str, description: str = ""
    ) -> dict[str, Any]:
        """Queue a Python pipeline for deployment as an AWS Lambda function.

        The code MUST define `def handler(event, context):` — that's the
        Lambda entrypoint. The deployed Lambda has ONLY the python3.12
        stdlib + boto3 available (no pandas / numpy). Use the `csv`
        module for CSV-in / CSV-out work. Bucket names are exposed
        inside the handler as env vars: BUCKET_RAW, BUCKET_PROCESSED,
        BUCKET_GOLD.

        IMPORTANT — how deployment actually happens: this tool does NOT
        call lambda:CreateFunction itself. The agent's execution role
        on this account is capped by a permissions boundary that
        forbids lambda creation and ALL IAM actions. Instead, this
        tool writes the pipeline spec (handler code + metadata) into
        the processed bucket under `_pipelines/pending/<name>/`, and
        the Chainlit frontend (running locally under a less restricted
        role) deploys it within seconds of the agent's turn ending.

        Tell the user something like "I've queued the pipeline; the
        frontend will deploy it now" — they'll see a confirmation
        message appear in the chat once the deploy completes.

        Args:
            name: Short pipeline name. Will be sanitised and prefixed
                with `aiagent-lambda-`.
            code: Python source. Must define `def handler(event, context)`.
            description: Optional human-readable description.

        Returns:
            Dict with the sanitised function name, the spec S3 URI,
            and a `status` of "queued" — the host-side deployer
            takes over from there.
        """
        suffix = _sanitise_pipeline_name(name)
        function_name = f"{LAMBDA_NAME_PREFIX}{suffix}"

        # Write the spec atomically by uploading the bigger blob (the
        # zip) first and the small JSON manifest last — the host-side
        # deployer triggers off the manifest, so seeing it means the
        # zip is already there.
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("handler.py", code)
        zip_bytes = zip_buf.getvalue()

        zip_key = f"{PIPELINE_PENDING_PREFIX}{suffix}/handler.zip"
        manifest_key = f"{PIPELINE_PENDING_PREFIX}{suffix}/manifest.json"
        manifest = {
            "function_name": function_name,
            "suffix": suffix,
            "description": description or f"Hackathon pipeline {suffix}",
            "zip_s3_uri": f"s3://{BUCKET_PROCESSED}/{zip_key}",
            "queued_at": int(time.time()),
            # Bucket env vars the host-side deployer should bake into
            # the function's Environment.Variables.
            "env_vars": {
                "BUCKET_RAW": BUCKET_RAW,
                "BUCKET_PROCESSED": BUCKET_PROCESSED,
                "BUCKET_GOLD": BUCKET_GOLD,
            },
        }
        s3.put_object(Bucket=BUCKET_PROCESSED, Key=zip_key, Body=zip_bytes)
        s3.put_object(
            Bucket=BUCKET_PROCESSED,
            Key=manifest_key,
            Body=json.dumps(manifest, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        log.info(
            "queued pipeline %s (%d bytes zip, manifest=s3://%s/%s)",
            function_name, len(zip_bytes), BUCKET_PROCESSED, manifest_key,
        )
        return {
            "function_name": function_name,
            "manifest_s3_uri": f"s3://{BUCKET_PROCESSED}/{manifest_key}",
            "status": "queued",
            "note": (
                "The Chainlit frontend deploys queued pipelines after each "
                "turn. The user will see a confirmation message in the "
                "chat once the function is live."
            ),
        }

    @tool
    def invoke_pipeline(name: str, payload: dict | None = None) -> dict[str, Any]:
        """Invoke a deployed pipeline once (synchronous) and return its response.

        Use this to prove the pipeline runs end-to-end after deploy.
        Args:
            name: The pipeline name (sanitised, with or without the
                `aiagent-lambda-` prefix).
            payload: Optional event dict passed to the handler. Defaults
                to an empty event.

        Returns:
            Dict with `status_code`, `payload` (parsed JSON if possible
            else raw string), and `log_tail` (last 4KB of execution logs).
        """
        suffix = _sanitise_pipeline_name(name).removeprefix(LAMBDA_NAME_PREFIX)
        function_name = f"{LAMBDA_NAME_PREFIX}{suffix}"
        body = json.dumps(payload or {}).encode("utf-8")
        resp = lam.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            LogType="Tail",
            Payload=body,
        )
        raw = resp["Payload"].read().decode("utf-8", errors="replace")
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        # Tail comes back base64'd. Decode for the LLM's benefit.
        import base64
        log_tail_b64 = resp.get("LogResult", "")
        log_tail = base64.b64decode(log_tail_b64).decode("utf-8", errors="replace")
        return {
            "status_code": resp["StatusCode"],
            "function_error": resp.get("FunctionError"),
            "payload": parsed,
            "log_tail": log_tail,
        }

    @tool
    def list_pipelines() -> dict[str, Any]:
        """List pipelines deployed in this account.

        Note: the runtime can't call lambda:ListFunctions (boundary-
        blocked), so this reads the active-pipelines manifest the
        Chainlit host writes when it completes a deploy. Each entry
        is a JSON manifest under `_pipelines/active/<suffix>/manifest.json`
        in the processed bucket. Reflects what's actually live.
        """
        out = []
        resp = s3.list_objects_v2(
            Bucket=BUCKET_PROCESSED, Prefix=PIPELINE_ACTIVE_PREFIX
        )
        for obj in resp.get("Contents", []):
            if not obj["Key"].endswith("/manifest.json"):
                continue
            body = s3.get_object(Bucket=BUCKET_PROCESSED, Key=obj["Key"])["Body"].read()
            try:
                out.append(json.loads(body.decode("utf-8")))
            except json.JSONDecodeError:
                pass
        return {"pipelines": out, "count": len(out)}

    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            interpreter.code_interpreter,
            list_s3_dataset,
            load_s3_into_sandbox,
            save_sandbox_to_s3,
            display_image,
            display_plotly,
            deploy_pipeline_as_lambda,
            invoke_pipeline,
            list_pipelines,
        ],
    )


def _get_agent(session_id: str) -> Agent:
    if session_id not in _agents:
        log.info("building agent for session %s (model=%s)", session_id, MODEL_ID)
        _agents[session_id] = _build_agent(session_id)
    return _agents[session_id]


@app.entrypoint
async def invoke(payload, context):
    """Stream the agent's response as SSE events.

    Each yielded dict becomes one SSE event. Strands' `stream_async`
    yields a mix of event types; we forward only `data` text deltas
    and `current_tool_use` events (dedup'd by toolUseId so a single
    tool call doesn't fan out to dozens of "tool_use" events as the
    input streams in).
    """
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not prompt:
        yield {"error": "missing 'prompt' in payload"}
        return

    session_id = (context.session_id if context else None) or "default"
    agent = _get_agent(session_id)

    log.info("invoke session=%s prompt=%r", session_id, prompt[:120])

    announced_tool_uses: set[str] = set()
    async for event in agent.stream_async(prompt):
        if not isinstance(event, dict):
            continue
        delta = event.get("data")
        if delta:
            yield {"delta": delta}
            continue
        tool_use = event.get("current_tool_use")
        if tool_use and isinstance(tool_use, dict):
            use_id = tool_use.get("toolUseId")
            name = tool_use.get("name")
            if use_id and use_id not in announced_tool_uses and name:
                announced_tool_uses.add(use_id)
                yield {"tool_use": {"name": name, "id": use_id}}
    yield {"complete": True}


if __name__ == "__main__":
    # BedrockAgentCoreApp.run() binds 0.0.0.0:8080 with /invocations + /ping.
    app.run()
