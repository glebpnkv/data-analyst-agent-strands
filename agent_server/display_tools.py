"""Strands tool factories that surface inline UI content via the v1 SSE protocol.

The model calls one of these tools to render a dataframe, Plotly chart, or
image directly in the chat. Each tool grabs the per-request `UIEmitter`
from the ContextVar and pushes a pre-formatted `ui.*` SSE event onto its
queue; the FastAPI streaming loop drains the queue between Strands events.

CRITICAL DESIGN POINT: the model passes a *sandbox path*, not the data
itself. The tool reads the file server-side via the supplied loader
callbacks. Passing megabytes of base64 / JSON / CSV through tool
arguments forces the LLM to generate every byte as tokens — slow,
expensive, and a frequent cause of conversation hangs. The factories
still accept inline JSON when no loader is configured, but agents with
a sandbox should always supply the loaders.

These factories follow the same `(tool_decorator=...)` override pattern
as the agent's existing utils/tools.py so they can be tested without
Strands installed.
"""

import csv
import io
import json
import logging
from collections.abc import Callable
from typing import Any

from . import events as ev
from .ui_emitter import get_current_emitter

# Sandbox loader callbacks supplied by each agent's server/main.py.
# Text loader: read a UTF-8 text file (JSON, CSV, etc) from the sandbox.
# Image loader: read a binary file and return a `data:<mime>;base64,...` URL.
SandboxTextLoader = Callable[[str], str]  # (sandbox_path) -> text content
SandboxImageLoader = Callable[[str, str], str]  # (sandbox_path, mime) -> data URL

# JSON-string heuristic for the no-loader fallback: an inline payload
# starts with `[` or `{`, a sandbox path almost certainly does not.
_INLINE_JSON_PREFIXES = ("[", "{")

tool_log = logging.getLogger("agent_server.display_tools")


def _identity_decorator(func):
    return func


def _resolve_tool_decorator(
    tool_decorator: Callable | None,
) -> Callable:
    if tool_decorator is not None:
        return tool_decorator
    try:
        from strands import tool as strands_tool

        return strands_tool
    except Exception:
        return _identity_decorator


def make_display_dataframe_tool(
    *,
    tool_decorator: Callable | None = None,
    sandbox_text_loader: SandboxTextLoader | None = None,
    max_rows: int = 1000,
) -> Callable[..., str]:
    """Build the `display_dataframe` tool.

    Args:
        tool_decorator: Optional override (typically for tests).
        sandbox_text_loader: If provided, `source` arguments that don't
            look like inline JSON are treated as sandbox file paths and
            resolved server-side. This is the recommended path — it
            keeps row data out of the LLM context.
        max_rows: Hard cap on rows surfaced inline; excess is truncated
            and flagged via the `truncated` field of the ui.dataframe event.
    """
    tool_wrap = _resolve_tool_decorator(tool_decorator)

    @tool_wrap
    def display_dataframe(source: str, title: str = "") -> str:
        """Render a tabular result inline in the chat.

        STRONGLY PREFER passing a sandbox file path (CSV or JSON records).
        The tool reads and parses the file server-side. Inline JSON is
        only safe for tiny tables — the LLM has to emit every cell as
        tokens, which is slow and expensive for anything beyond a few rows.

        Args:
            source: One of:
                - sandbox path (RECOMMENDED), e.g.
                  `tmp/analysis_outputs/dataframes/results.csv` or `.json`.
                  Format is detected by extension; CSV and JSON-records
                  (`[{"col": 1}, ...]`) are supported.
                - inline JSON records (only for small tables). Starts
                  with `[`. The same shape as `pandas.DataFrame.to_dict('records')`.
            title: Short caption shown above the table.

        Returns:
            JSON status message. `ok=true` on success; `ok=false` with
            an `error` field on failure.
        """
        if not isinstance(source, str) or not source.strip():
            return json.dumps({"ok": False, "error": "source is required"})

        records: list[dict[str, Any]]
        looks_inline = source.lstrip().startswith(_INLINE_JSON_PREFIXES)

        if looks_inline:
            tool_log.info("display_dataframe parsing inline JSON (len=%d)", len(source))
            try:
                parsed = json.loads(source)
            except json.JSONDecodeError as exc:
                return json.dumps({"ok": False, "error": f"inline source is not valid JSON: {exc}"})
            if not isinstance(parsed, list) or (parsed and not all(isinstance(r, dict) for r in parsed)):
                return json.dumps({"ok": False, "error": "inline source must be a JSON array of objects"})
            records = parsed
        elif sandbox_text_loader is not None:
            tool_log.info("display_dataframe loading sandbox path: %r", source[:120])
            try:
                text = sandbox_text_loader(source)
            except Exception as e:
                tool_log.warning("sandbox_text_loader failed for %r: %s", source[:120], e)
                return json.dumps({"ok": False, "error": f"failed to load sandbox file at {source!r}: {e}"})
            try:
                records = _parse_dataframe_text(text, source)
            except ValueError as e:
                return json.dumps({"ok": False, "error": str(e)})
        else:
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "source looks like a sandbox path but no sandbox_text_loader is "
                        "configured for this agent. Pass inline JSON records starting with `[`."
                    ),
                }
            )

        # Stable column order: union of keys, in first-seen order.
        column_order: list[str] = []
        seen: set[str] = set()
        for record in records:
            for key in record.keys():
                if key not in seen:
                    seen.add(key)
                    column_order.append(key)

        truncated = len(records) > max_rows
        kept_records = records[:max_rows]
        rows = [[record.get(col) for col in column_order] for record in kept_records]
        schema = [{"name": col, "type": _infer_column_type(kept_records, col)} for col in column_order]

        emitter = get_current_emitter()
        if emitter is None:
            return json.dumps(
                {"ok": False, "error": "no UI emitter on this request; tool is being called outside an SSE stream"}
            )

        emitter.emit(ev.ui_dataframe(title=title, schema=schema, rows=rows, truncated=truncated))
        return json.dumps(
            {
                "ok": True,
                "rendered_rows": len(rows),
                "columns": column_order,
                "truncated": truncated,
            }
        )

    return display_dataframe


def make_display_plotly_tool(
    *,
    tool_decorator: Callable | None = None,
    sandbox_text_loader: SandboxTextLoader | None = None,
) -> Callable[..., str]:
    """Build the `display_plotly` tool.

    Args:
        tool_decorator: Optional override.
        sandbox_text_loader: If provided, `source` arguments that don't
            look like inline JSON are treated as sandbox file paths and
            resolved server-side. Strongly recommended — Plotly figure
            JSON for non-trivial charts can be hundreds of KB.
    """
    tool_wrap = _resolve_tool_decorator(tool_decorator)

    @tool_wrap
    def display_plotly(source: str, title: str = "") -> str:
        """Render an interactive Plotly chart inline in the chat.

        STRONGLY PREFER passing a sandbox path. Build the figure in the
        sandbox, persist with `fig.write_json("tmp/analysis_outputs/plotly/foo.json")`,
        then call this tool with the path. Passing the figure JSON inline
        forces the LLM to emit every byte of the figure as tokens.

        Args:
            source: One of:
                - sandbox path (RECOMMENDED), e.g.
                  `tmp/analysis_outputs/plotly/scatter.json`.
                - inline figure JSON (only for trivial charts). Starts with `{`.
                  The output of `fig.to_json()`.
            title: Short caption shown above the chart.

        Returns:
            JSON status message.
        """
        if not isinstance(source, str) or not source.strip():
            return json.dumps({"ok": False, "error": "source is required"})

        looks_inline = source.lstrip().startswith("{")
        figure: dict[str, Any]

        if looks_inline:
            tool_log.info("display_plotly parsing inline JSON (len=%d)", len(source))
            try:
                parsed = json.loads(source)
            except json.JSONDecodeError as exc:
                return json.dumps({"ok": False, "error": f"inline source is not valid JSON: {exc}"})
            if not isinstance(parsed, dict):
                return json.dumps({"ok": False, "error": "inline source must be a JSON object (a Plotly figure)"})
            figure = parsed
        elif sandbox_text_loader is not None:
            tool_log.info("display_plotly loading sandbox path: %r", source[:120])
            try:
                text = sandbox_text_loader(source)
            except Exception as e:
                tool_log.warning("sandbox_text_loader failed for %r: %s", source[:120], e)
                return json.dumps({"ok": False, "error": f"failed to load sandbox file at {source!r}: {e}"})
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                return json.dumps({"ok": False, "error": f"sandbox file at {source!r} is not valid JSON: {exc}"})
            if not isinstance(parsed, dict):
                return json.dumps({"ok": False, "error": f"sandbox file at {source!r} must contain a JSON object (Plotly figure)"})
            figure = parsed
        else:
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "source looks like a sandbox path but no sandbox_text_loader is "
                        "configured for this agent. Pass inline figure JSON starting with `{`."
                    ),
                }
            )

        emitter = get_current_emitter()
        if emitter is None:
            return json.dumps(
                {"ok": False, "error": "no UI emitter on this request; tool is being called outside an SSE stream"}
            )

        emitter.emit(ev.ui_plotly(title=title, figure=figure))
        return json.dumps({"ok": True})

    return display_plotly


def _parse_dataframe_text(text: str, source_hint: str) -> list[dict[str, Any]]:
    """Parse JSON-records or CSV text into a list of dict records.

    Format detection: file extension first (`.json` / `.jsonl` → JSON;
    `.csv` / `.tsv` → CSV); fall back to sniffing if the extension is
    missing or unrecognized.
    """
    lower = source_hint.lower()
    if lower.endswith((".json", ".jsonl")):
        return _parse_json_records_text(text)
    if lower.endswith((".csv", ".tsv")):
        delimiter = "\t" if lower.endswith(".tsv") else ","
        return _parse_csv_text(text, delimiter=delimiter)

    stripped = text.lstrip()
    if stripped.startswith(("[", "{")):
        return _parse_json_records_text(text)
    return _parse_csv_text(text, delimiter=",")


def _parse_json_records_text(text: str) -> list[dict[str, Any]]:
    parsed = json.loads(text)
    if isinstance(parsed, list):
        if parsed and not all(isinstance(r, dict) for r in parsed):
            raise ValueError("JSON list must contain only objects (records)")
        return parsed
    if isinstance(parsed, dict):
        # JSONL would be one object per line — handle both forms gracefully.
        return [parsed]
    raise ValueError("JSON content must be a list of objects (records)")


def _parse_csv_text(text: str, *, delimiter: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [dict(row) for row in reader]


_VALID_IMAGE_URL_SCHEMES = ("https://", "http://", "data:", "s3://")


def make_display_image_tool(
    *,
    tool_decorator: Callable | None = None,
    sandbox_image_loader: SandboxImageLoader | None = None,
) -> Callable[..., str]:
    """Build the `display_image` tool.

    Args:
        tool_decorator: Optional Strands tool decorator override.
        sandbox_image_loader: If provided, paths that don't look like a URL
            are treated as sandbox paths and resolved by this callback. The
            callback runs *in the agent service process*, not in the model
            context — keeping multi-MB image bytes out of the LLM's tokens.
            Without this loader the tool only accepts pre-built URLs.
    """
    tool_wrap = _resolve_tool_decorator(tool_decorator)

    @tool_wrap
    def display_image(source: str, title: str = "", mime: str = "image/png") -> str:
        """Render an image inline in the chat.

        STRONGLY PREFER passing a sandbox path (e.g. `workspace/foo.png`)
        when the image lives in the code interpreter sandbox. The tool
        reads and base64-encodes the file itself — do NOT base64-encode
        in the sandbox and pass the resulting megabytes through this
        argument: that will hang the conversation while the model emits
        the data URL token by token.

        Args:
            source: One of:
                - sandbox path (RECOMMENDED for sandbox images), e.g.
                  `workspace/glue-pipeline-xxx/plot.png`. Resolved
                  server-side, no token cost beyond the path.
                - `https://...` / `http://...` URL the user's browser can fetch.
                - `s3://...` URI (the frontend will sign it).
                - `data:<mime>;base64,...` data URL (avoid for large images).
            title: Short caption shown beside the image.
            mime: MIME type, e.g. `image/png`, `image/jpeg`, `image/svg+xml`.

        Returns:
            JSON status message. `ok=true` on success; `ok=false` with an
            actionable `error` field on failure.
        """
        if not isinstance(source, str) or not source.strip():
            return json.dumps({"ok": False, "error": "source is required"})

        url: str
        scheme_ok = any(source.startswith(scheme) for scheme in _VALID_IMAGE_URL_SCHEMES)
        if scheme_ok:
            url = source
        elif sandbox_image_loader is not None:
            tool_log.info("display_image loading sandbox path: %r", source[:120])
            try:
                url = sandbox_image_loader(source, mime)
            except Exception as e:
                tool_log.warning("sandbox_image_loader failed for %r: %s", source[:120], e)
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"failed to load sandbox image at {source!r}: {e}",
                    }
                )
        else:
            tool_log.warning("display_image rejected unsupported source: %r", source[:80])
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "source must be a sandbox path or a URL with one of these schemes: "
                        + ", ".join(_VALID_IMAGE_URL_SCHEMES)
                    ),
                    "received_prefix": source[:40],
                }
            )

        emitter = get_current_emitter()
        if emitter is None:
            return json.dumps(
                {"ok": False, "error": "no UI emitter on this request; tool is being called outside an SSE stream"}
            )

        url_kind = "data-url" if url.startswith("data:") else url.split("://", 1)[0]
        tool_log.info("display_image emitting (kind=%s, mime=%s, title=%r)", url_kind, mime, title[:60])
        emitter.emit(ev.ui_image(title=title, url=url, mime=mime))
        return json.dumps({"ok": True})

    return display_image


def _infer_column_type(records: list[dict[str, Any]], column: str) -> str:
    """Best-effort column type inference from non-null sample values."""
    for record in records:
        value = record.get(column)
        if value is None:
            continue
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, (list, dict)):
            return "json"
        return "string"
    return "string"
