"""Streaming-event reducer interface and concrete Strands implementation.

`EventReducer` is the contract the FastAPI app expects: take whatever the
underlying agent SDK yields and produce zero-or-more v1 SSE events.

Strands agents share the same SDK, so they can use `StrandsEventReducer`
unchanged. Other SDKs (LangChain Deep Agents, plain LangGraph, etc.) write
their own reducer that yields the same v1 events, and the rest of the
scaffold doesn't change.
"""

from collections.abc import Callable, Iterable
from typing import Any, Protocol

from . import events as ev


class EventReducer(Protocol):
    """Translate one SDK-native event into zero-or-more SSE events."""

    def reduce(self, sdk_event: Any) -> Iterable[dict[str, str]]: ...


ReducerFactory = Callable[[], EventReducer]


class StrandsEventReducer:
    """Reduce Strands typed events (dicts from `TypedEvent.as_dict()`).

    State is needed to emit `tool.start` exactly once per `toolUseId`,
    which Strands streams incrementally as `ToolUseStreamEvent` chunks.
    """

    def __init__(self) -> None:
        self._tools_started: set[str] = set()

    def reduce(self, sdk_event: Any) -> Iterable[dict[str, str]]:
        if not isinstance(sdk_event, dict):
            return

        # Final agent result — emit `done`. Capture the OTel trace/span
        # IDs now: Strands is still inside its agent-root span at this
        # point, so `get_current_span()` returns the span Phoenix will
        # surface as the trace root. Callers (eval runners, Chainlit)
        # use these to look the trace up in Phoenix or post annotations.
        if "result" in sdk_event and "data" not in sdk_event:
            usage = self._extract_usage(sdk_event.get("result"))
            trace_id, span_id = self._current_trace_and_span_ids()
            yield ev.done(usage=usage, trace_id=trace_id, span_id=span_id)
            return

        # Reasoning text streaming.
        if sdk_event.get("reasoning") and "reasoningText" in sdk_event:
            text = sdk_event.get("reasoningText") or ""
            if text:
                yield ev.thinking_delta(text)
            return

        # Plain text streaming (`TextStreamEvent`).
        if "data" in sdk_event and isinstance(sdk_event.get("data"), str):
            text = sdk_event["data"]
            if text:
                yield ev.text_delta(text)
            return

        # Tool input streaming (`ToolUseStreamEvent`). Emit `tool.start` once.
        if sdk_event.get("type") == "tool_use_stream":
            current_tool = sdk_event.get("current_tool_use") or {}
            tool_use_id = current_tool.get("toolUseId")
            name = current_tool.get("name")
            if tool_use_id and name and tool_use_id not in self._tools_started:
                self._tools_started.add(tool_use_id)
                yield ev.tool_start(
                    tool_use_id=tool_use_id,
                    name=name,
                    input_partial=current_tool.get("input"),
                )
            return

        # Tool result (`ToolResultEvent`).
        if sdk_event.get("type") == "tool_result":
            tool_result = sdk_event.get("tool_result") or {}
            tool_use_id = tool_result.get("toolUseId", "")
            status = tool_result.get("status", "ok")
            summary = self._summarize_tool_result(tool_result)
            yield ev.tool_end(tool_use_id=tool_use_id, status=status, summary=summary)
            return

        # All other event types are ignored in v0.

    @staticmethod
    def _current_trace_and_span_ids() -> tuple[str | None, str | None]:
        """Return the current OTel trace/span IDs as 32- and 16-hex strings.

        Returns (None, None) if OTel is not installed or no span is
        active. Hex-encoded matches what Phoenix shows in its UI.
        """
        try:
            from opentelemetry import trace
        except Exception:
            return None, None
        span = trace.get_current_span()
        if span is None:
            return None, None
        ctx = span.get_span_context()
        if not ctx or not ctx.is_valid:
            return None, None
        return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")

    @staticmethod
    def _extract_usage(result: Any) -> dict[str, Any] | None:
        if result is None:
            return None
        metrics = getattr(result, "metrics", None)
        if metrics is None:
            return None
        try:
            summary = metrics.get_summary()
        except Exception:
            return None
        return summary if isinstance(summary, dict) else None

    @staticmethod
    def _summarize_tool_result(tool_result: dict[str, Any]) -> str | None:
        content = tool_result.get("content")
        if not content:
            return None
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(str(text))
            joined = " ".join(parts).strip()
            return _truncate(joined) if joined else None
        if isinstance(content, str):
            return _truncate(content)
        return None


def _truncate(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
