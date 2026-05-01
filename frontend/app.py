"""Chainlit chat frontend that streams from the agent_server v1 SSE protocol.

Each Chainlit chat session maps to one agent session_id, so multi-turn
conversations land on the same agent instance and preserve context. The
handler reads SSE events from POST /v1/chat and dispatches them onto
Chainlit primitives:

  text.delta     -> stream_token into the assistant message
  thinking.delta -> stream into a Step(type="thinking") (lazily created)
  tool.start     -> open a Step(type="tool", name=<tool name>)
  tool.end       -> set the step's output and close it
  ui.dataframe   -> attach a cl.Dataframe element to the assistant message
  ui.plotly      -> attach a cl.Plotly element to the assistant message
  ui.image       -> attach a cl.Image element to the assistant message
  done           -> end of turn
  error          -> ErrorMessage

Persistence and authentication are out of scope for v0 — added in Phase 3.
"""

import base64
import json
import logging
import os
import uuid
from pathlib import Path

import chainlit as cl
import httpx
import pandas as pd
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from dotenv import load_dotenv
from plotly import graph_objects as go

load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger(__name__)

AGENT_BASE_URL = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
AGENT_SERVICE_AUTH_SECRET = os.environ.get("AGENT_SERVICE_AUTH_SECRET") or None
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("AGENT_REQUEST_TIMEOUT_SECONDS", "600"))
DATABASE_URL = os.environ.get("DATABASE_URL") or None
DEPLOYED_BEHIND_ALB = os.environ.get("DEPLOYED_BEHIND_ALB") == "1"


# --- Persistence -----------------------------------------------------------
#
# Chainlit's data layer hook: when DATABASE_URL is set, persist threads /
# steps / elements to Postgres via the built-in SQLAlchemyDataLayer.
# Without it, conversations vanish on refresh — fine for the bare local
# skeleton, not fine for stakeholder demos. The local stack script sets
# DATABASE_URL automatically; deployed environments get it from the CDK
# stack's RDS Postgres.

if DATABASE_URL:

    @cl.data_layer
    def get_data_layer():
        # storage_provider is None for local dev: Chainlit logs a warning
        # at boot saying inline elements (cl.Image bytes etc) won't be
        # persisted, so charts inside resumed threads won't replay. Thread
        # text + tool steps DO persist. Phase 4 introduces an S3-backed
        # storage_provider for full fidelity.
        return SQLAlchemyDataLayer(conninfo=DATABASE_URL)


# --- Authentication --------------------------------------------------------
#
# Two modes, switched by the DEPLOYED_BEHIND_ALB env var (set by the CDK
# compute stack on the deployed task).
#
# Local dev (DEPLOYED_BEHIND_ALB unset): hardcoded admin/admin via
# password_auth_callback. Lets `./scripts/run_local_stack.sh` work
# without any auth infrastructure.
#
# Deployed (DEPLOYED_BEHIND_ALB=1): the ALB has already authenticated
# the user via Cognito by the time the request reaches us, and forwards
# the user's identity in a JWT in the `x-amzn-oidc-data` header. We
# decode the JWT (no signature verification — see comment below) and
# return a cl.User keyed on the user's email.
#
# Why we don't verify the JWT signature here: the ALB sets this header
# AFTER it has already validated the user with Cognito, and the SG only
# lets ALB traffic reach the frontend tasks (frontend_alb_sg ->
# frontend_task_sg). So the JWT we receive has been validated upstream.
# A defence-in-depth setup WOULD verify by fetching the public key
# from `https://public-keys.auth.elb.<region>.amazonaws.com/<key-id>`
# (the JWT header carries `kid`); add that if the security model
# requires zero trust between ALB and tasks.

if DEPLOYED_BEHIND_ALB:

    @cl.header_auth_callback
    def header_auth(headers: dict) -> cl.User | None:
        # Header name is case-insensitive but Chainlit normalises to
        # lower; accept either form to be robust to that contract
        # changing.
        oidc_data = headers.get("x-amzn-oidc-data") or headers.get("X-Amzn-Oidc-Data")
        if not oidc_data:
            log.warning("DEPLOYED_BEHIND_ALB=1 but no x-amzn-oidc-data header")
            return None
        try:
            _hdr_b64, payload_b64, _sig_b64 = oidc_data.split(".")
            # JWT base64url is unpadded — re-add padding to make
            # urlsafe_b64decode happy.
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception as e:
            log.warning("failed to decode x-amzn-oidc-data: %s", e)
            return None

        email = payload.get("email")
        sub = payload.get("sub")
        if not email and not sub:
            log.warning("x-amzn-oidc-data payload had neither email nor sub: %s", payload)
            return None

        return cl.User(
            identifier=email or sub,
            metadata={
                "email": email,
                "cognito_sub": sub,
                "role": "deployed",
            },
        )

else:

    @cl.password_auth_callback
    def password_auth(username: str, password: str) -> cl.User | None:
        if (username, password) == ("admin", "admin"):
            return cl.User(identifier="admin", metadata={"role": "local-dev"})
        return None


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("agent_session_id", uuid.uuid4().hex)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    session_id = cl.user_session.get("agent_session_id") or uuid.uuid4().hex
    cl.user_session.set("agent_session_id", session_id)

    body = {"session_id": session_id, "prompt": message.content}
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if AGENT_SERVICE_AUTH_SECRET:
        headers["X-Service-Auth"] = AGENT_SERVICE_AUTH_SECRET

    assistant_msg = cl.Message(content="")
    tool_steps: dict[str, cl.Step] = {}
    thinking_step: cl.Step | None = None

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{AGENT_BASE_URL}/v1/chat",
                json=body,
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")
                    await cl.ErrorMessage(
                        content=f"Agent service returned {response.status_code}: {detail[:500]}"
                    ).send()
                    return

                async for event_type, payload in _iter_sse(response):
                    thinking_step = await _dispatch(
                        event_type=event_type,
                        payload=payload,
                        assistant_msg=assistant_msg,
                        tool_steps=tool_steps,
                        thinking_step=thinking_step,
                    )

    except httpx.RequestError as e:
        log.exception("agent service request failed")
        await cl.ErrorMessage(content=f"Could not reach agent service: {e}").send()
        return

    await assistant_msg.send()


async def _dispatch(
    *,
    event_type: str,
    payload: dict,
    assistant_msg: cl.Message,
    tool_steps: dict[str, cl.Step],
    thinking_step: cl.Step | None,
) -> cl.Step | None:
    if event_type == "text.delta":
        await assistant_msg.stream_token(payload.get("content", ""))
        return thinking_step

    if event_type == "thinking.delta":
        if thinking_step is None:
            thinking_step = cl.Step(name="thinking", type="undefined")
            await thinking_step.send()
        thinking_step.output = (thinking_step.output or "") + payload.get("content", "")
        await thinking_step.update()
        return thinking_step

    if event_type == "tool.start":
        step = cl.Step(name=payload.get("name", "tool"), type="tool")
        step.input = json.dumps(payload.get("input", {}), indent=2, default=str)
        await step.send()
        tool_steps[payload["id"]] = step
        return thinking_step

    if event_type == "tool.end":
        step = tool_steps.pop(payload.get("id", ""), None)
        if step is not None:
            summary = payload.get("summary") or ""
            status = payload.get("status", "ok")
            step.output = summary
            if status not in {"ok", "success"}:
                step.is_error = True
            await step.update()
        return thinking_step

    if event_type == "ui.dataframe":
        await _attach_dataframe(assistant_msg, payload)
        return thinking_step

    if event_type == "ui.plotly":
        await _attach_plotly(assistant_msg, payload)
        return thinking_step

    if event_type == "ui.image":
        await _attach_image(assistant_msg, payload)
        return thinking_step

    if event_type == "done":
        return thinking_step

    if event_type == "error":
        await cl.ErrorMessage(
            content=payload.get("message", "agent emitted an error event")
        ).send()
        return thinking_step

    log.debug("ignoring unknown SSE event type: %s", event_type)
    return thinking_step


async def _attach_dataframe(assistant_msg: cl.Message, payload: dict) -> None:
    schema = payload.get("schema") or []
    columns = [col.get("name") for col in schema if col.get("name")]
    rows = payload.get("rows") or []
    try:
        df = pd.DataFrame(rows, columns=columns) if columns else pd.DataFrame(rows)
    except Exception as e:
        log.warning("ui.dataframe payload could not be coerced to DataFrame: %s", e)
        await cl.ErrorMessage(content=f"Could not render table: {e}").send()
        return
    title = payload.get("title") or "table"
    suffix = " (truncated)" if payload.get("truncated") else ""
    assistant_msg.elements.append(cl.Dataframe(name=f"{title}{suffix}", data=df, display="inline"))
    await assistant_msg.update()


async def _attach_plotly(assistant_msg: cl.Message, payload: dict) -> None:
    figure_dict = payload.get("figure") or {}
    try:
        # skip_invalid=True drops properties our plotly version doesn't
        # recognize (e.g. removed trace types like heatmapgl). The agent
        # also upgrades plotly in its sandbox at session init; this is
        # the defense-in-depth path for stale figure templates.
        figure = go.Figure(figure_dict, skip_invalid=True)
    except Exception as e:
        log.warning("ui.plotly payload is not a valid figure dict: %s", e)
        await cl.ErrorMessage(content=f"Could not render chart: {e}").send()
        return
    title = payload.get("title") or "chart"
    assistant_msg.elements.append(cl.Plotly(name=title, figure=figure, display="inline"))
    await assistant_msg.update()


async def _attach_image(assistant_msg: cl.Message, payload: dict) -> None:
    """Render a ui.image event.

    Note: unlike dataframe/plotly we don't append to assistant_msg.elements
    and update(). cl.Image elements added mid-stream don't render reliably
    that way (the message has already been sent via stream_token, and the
    update path serializes elements differently for binary content). We
    send a fresh message carrying just the image — it appears as its own
    bubble in the conversation, immediately after whatever the assistant
    was streaming.
    """
    url = payload.get("url")
    if not url:
        log.warning("ui.image payload missing url")
        return
    title = payload.get("title") or "image"
    mime = payload.get("mime") or "image/png"
    url_kind = "data-url" if url.startswith("data:") else url.split("://", 1)[0]
    log.info("ui.image arrived (kind=%s, title=%r, url_len=%d)", url_kind, title, len(url))

    image_element: cl.Image | None = None

    # data URLs: decode here, serve via cl.Image(content=).
    if url.startswith("data:"):
        try:
            header, b64 = url.split(",", 1)
            content = base64.b64decode(b64)
            if ";" in header:
                mime_from_header = header[len("data:"):].split(";", 1)[0]
                if mime_from_header:
                    mime = mime_from_header
        except Exception as e:
            log.warning("ui.image: failed to decode data URL: %s", e)
            await cl.ErrorMessage(content=f"Could not decode image: {e}").send()
            return
        image_element = cl.Image(
            name=title,
            content=content,
            mime=mime,
            display="inline",
        )
    else:
        # Real URL Chainlit can fetch directly.
        image_element = cl.Image(
            name=title,
            url=url,
            display="inline",
        )

    # The image needs a non-empty content string so Chainlit renders the
    # bubble at all; the title doubles as caption.
    await cl.Message(content=title, elements=[image_element]).send()


async def _iter_sse(response: httpx.Response):
    """Parse SSE stream into (event_type, json_payload) tuples.

    Handles the minimal subset of the SSE spec we need: `event:` and `data:`
    lines, terminated by a blank line. Tolerates multiple `data:` lines
    by joining with newlines per the spec.
    """
    event_type: str | None = None
    data_lines: list[str] = []

    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if event_type and data_lines:
                payload_text = "\n".join(data_lines)
                try:
                    payload = json.loads(payload_text)
                except json.JSONDecodeError:
                    log.warning("dropping malformed SSE payload: %r", payload_text)
                    payload = None
                if payload is not None:
                    yield event_type, payload
            event_type = None
            data_lines = []
            continue

        if line.startswith(":"):
            # SSE comment / keepalive
            continue
        if line.startswith("event:"):
            event_type = line[6:].lstrip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
