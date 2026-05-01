"""Typed SSE event payloads emitted by /v1/chat.

Wire format: each SSE event has `event: <type>` and `data: <json>`.
Protocol version: v1.
"""

import json
from typing import Any

PROTOCOL_VERSION = "v1"


def text_delta(content: str) -> dict[str, str]:
    return _sse("text.delta", {"content": content})


def thinking_delta(content: str) -> dict[str, str]:
    return _sse("thinking.delta", {"content": content})


def tool_start(tool_use_id: str, name: str, input_partial: dict[str, Any] | None = None) -> dict[str, str]:
    return _sse(
        "tool.start",
        {"id": tool_use_id, "name": name, "input": input_partial or {}},
    )


def tool_end(
    tool_use_id: str,
    status: str,
    summary: str | None = None,
) -> dict[str, str]:
    payload: dict[str, Any] = {"id": tool_use_id, "status": status}
    if summary is not None:
        payload["summary"] = summary
    return _sse("tool.end", payload)


def done(usage: dict[str, Any] | None = None) -> dict[str, str]:
    payload: dict[str, Any] = {"protocol": PROTOCOL_VERSION}
    if usage is not None:
        payload["usage"] = usage
    return _sse("done", payload)


def error(message: str, code: str = "internal_error") -> dict[str, str]:
    return _sse("error", {"message": message, "code": code})


# --- UI events: inline rich content surfaced by display_* tools. ---------


def ui_dataframe(
    *,
    title: str,
    schema: list[dict[str, str]],
    rows: list[list[Any]],
    truncated: bool = False,
) -> dict[str, str]:
    """Inline tabular data.

    Args:
        title: Human-readable title shown above the table.
        schema: List of {"name": str, "type": str} column descriptors.
        rows: Row-major data; each row is a list aligned with `schema`.
        truncated: True if `rows` is a prefix of a larger result set.
    """
    return _sse(
        "ui.dataframe",
        {"title": title, "schema": schema, "rows": rows, "truncated": truncated},
    )


def ui_plotly(*, title: str, figure: dict[str, Any]) -> dict[str, str]:
    """Inline Plotly chart. `figure` is a Plotly figure JSON dict."""
    return _sse("ui.plotly", {"title": title, "figure": figure})


def ui_image(*, title: str, url: str, mime: str = "image/png") -> dict[str, str]:
    """Inline image.

    Args:
        title: Alt text / caption.
        url: Either an `https://` URL, an `s3://` URI (signed by the
            frontend), or a `data:<mime>;base64,...` data URL.
        mime: MIME type, e.g. `image/png`, `image/jpeg`, `image/svg+xml`.
    """
    return _sse("ui.image", {"title": title, "url": url, "mime": mime})


def _sse(event_type: str, payload: dict[str, Any]) -> dict[str, str]:
    return {"event": event_type, "data": json.dumps(payload, default=str)}
