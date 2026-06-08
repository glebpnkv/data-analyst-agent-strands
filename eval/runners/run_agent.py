"""POST a prompt to the deployed agent's /v1/chat, parse the SSE stream.

Why direct HTTP and not the Strands SDK: we want to evaluate the
*deployed* agent — the same code path as production traffic — so the
trace lands in Phoenix and we exercise the real session lifecycle,
sandbox claim, MCP servers, etc. Strands-in-process would skip all of
that.

How to reach the agent: the agent ALB is internal-only. From a
developer laptop the recommended path is an SSM port-forward to the
agent ALB. See `scripts/portforward_agent.sh` (helper).

Auth: every request carries `X-Service-Auth: <secret>` where the
secret is the same Secrets Manager value the deployed Chainlit task
uses. Fetch via the helper, or set AGENT_SERVICE_AUTH_SECRET in env.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 180.0
"""Cold sandbox claim + multi-tool turns can take a while; be generous."""


@dataclass
class ToolCall:
    """One tool invocation captured from the SSE stream."""

    name: str
    input: dict
    status: str | None = None  # success / error / canceled, from tool_end
    summary: str | None = None  # truncated tool output, from tool_end


@dataclass
class AgentRunResult:
    """Everything one /v1/chat call produced, normalised."""

    session_id: str
    answer: str  # accumulated text_delta content
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict | None = None  # token counts from `done` event
    trace_id: str | None = None  # OTel trace id (32 hex) from `done`
    span_id: str | None = None  # OTel root span id (16 hex) from `done`
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    raw_events: list[dict] = field(default_factory=list)  # for debugging

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def tool_error_count(self) -> int:
        return sum(1 for t in self.tool_calls if t.status and t.status != "success")


class DeployedAgentClient:
    """Thin client over POST /v1/chat with SSE response parsing."""

    def __init__(
        self,
        base_url: str,
        service_auth_secret: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_auth_secret = service_auth_secret
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "DeployedAgentClient":
        """Construct from AGENT_BASE_URL + AGENT_SERVICE_AUTH_SECRET env vars."""
        base = os.environ.get("AGENT_BASE_URL", "").strip()
        secret = os.environ.get("AGENT_SERVICE_AUTH_SECRET", "").strip()
        if not base:
            raise RuntimeError("AGENT_BASE_URL must be set (e.g. http://localhost:8080)")
        if not secret:
            raise RuntimeError(
                "AGENT_SERVICE_AUTH_SECRET must be set "
                "(fetch from Secrets Manager DataAnalystAgent/Dev/ServiceAuthSecret)"
            )
        return cls(base_url=base, service_auth_secret=secret)

    def close_session(self, session_id: str) -> None:
        """End the named session so the agent releases its sandbox + MCP subprocesses.

        Idempotent on the agent side — a 204 is returned whether or not
        the session was still alive. We do this after every eval golden
        so the cluster's sandbox capacity is freed for the next case
        rather than parked until the session TTL fires.
        """
        if not session_id:
            return
        url = f"{self.base_url}/v1/sessions/{session_id}"
        headers = {"X-Service-Auth": self.service_auth_secret}
        try:
            with httpx.Client(timeout=30.0) as client:
                client.delete(url, headers=headers)
        except Exception as e:  # noqa: BLE001
            # Cleanup failure shouldn't fail the eval run; the session's
            # idle TTL will reap it eventually.
            log.warning("close_session(%s) failed: %s", session_id, e)

    def chat(self, prompt: str, *, session_id: str | None = None) -> AgentRunResult:
        """Send one prompt, drain the SSE stream, return the normalised result."""
        sid = session_id or uuid.uuid4().hex
        url = f"{self.base_url}/v1/chat"
        headers = {
            "X-Service-Auth": self.service_auth_secret,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        payload = {"session_id": sid, "prompt": prompt}

        result = AgentRunResult(session_id=sid, answer="")
        # tool_use_id -> partial ToolCall, completed on tool_end
        in_flight: dict[str, ToolCall] = {}

        start = time.monotonic()
        with httpx.Client(timeout=self.timeout_seconds) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                for event in _iter_sse_events(response.iter_lines()):
                    result.raw_events.append(event)
                    self._apply_event(event, result, in_flight)
        result.elapsed_seconds = time.monotonic() - start
        # Anything still in_flight at end-of-stream — count as
        # incomplete tool calls (status left as None).
        result.tool_calls.extend(in_flight.values())
        return result

    @staticmethod
    def _apply_event(
        event: dict,
        result: AgentRunResult,
        in_flight: dict[str, ToolCall],
    ) -> None:
        # Event-name keys match the v1 wire protocol exactly — see
        # agent_server/events.py. Dotted names, not underscored. The
        # tool ID field is "id" on both tool.start and tool.end.
        kind = event.get("event")
        data = event.get("data") or {}
        if kind == "text.delta":
            result.answer += data.get("content", "")
        elif kind == "tool.start":
            tool_use_id = data.get("id") or uuid.uuid4().hex
            in_flight[tool_use_id] = ToolCall(
                name=data.get("name", ""),
                input=data.get("input") or {},
            )
        elif kind == "tool.end":
            tool_use_id = data.get("id", "")
            call = in_flight.pop(tool_use_id, None)
            if call is None:
                # tool.end without a matching tool.start (shouldn't
                # happen) — record it anyway so it counts in
                # tool_call_count and the discrepancy is visible.
                call = ToolCall(name="<unknown>", input={})
            call.status = data.get("status")
            call.summary = data.get("summary")
            result.tool_calls.append(call)
        elif kind == "done":
            result.usage = data.get("usage")
            result.trace_id = data.get("trace_id")
            result.span_id = data.get("span_id")
        elif kind == "error":
            result.errors.append(data.get("message", "<no message>"))


def _iter_sse_events(line_iter: Iterable[bytes | str]) -> Iterable[dict]:
    """Standard SSE parser: lines until blank, then yield the event."""
    event_type: str | None = None
    data_lines: list[str] = []
    for raw_line in line_iter:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if line == "":
            if event_type is not None or data_lines:
                yield _build_event(event_type, data_lines)
                event_type = None
                data_lines = []
            continue
        if line.startswith(":"):
            # SSE comment line; ignore.
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
        # Other fields (id:, retry:) ignored.
    # Tail event with no trailing blank line.
    if event_type is not None or data_lines:
        yield _build_event(event_type, data_lines)


def _build_event(event_type: str | None, data_lines: list[str]) -> dict:
    payload = "\n".join(data_lines)
    try:
        parsed = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        parsed = {"_raw": payload}
    return {"event": event_type or "message", "data": parsed}
