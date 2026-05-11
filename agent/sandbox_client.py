"""HTTP client + Strands `CodeInterpreter` adapter for the ECS sandbox.

This is what the agent uses in place of `AgentCoreCodeInterpreter`. It
subclasses Strands' abstract `CodeInterpreter` so the action surface
(`executeCode`, `writeFiles`, `readFiles`, `executeCommand`, `listFiles`,
`removeFiles`) is identical from the model's perspective. Internally,
each action becomes one HTTP POST against the sandbox FastAPI server
running in a per-session ECS task.

Two extra public methods are NOT exposed to the LLM and exist only for
host-side display-tool support:
  - `read_text_file(path)` returns sandbox file contents as a UTF-8 string
  - `read_binary_file_as_data_url(path, mime)` returns a `data:<mime>;base64,...`
    URL ready to drop into a `ui.image` event

The sandbox task lifecycle (RunTask, StopTask, IP discovery, warm pool)
lives in `agent_server.sandbox_pool.SandboxPool` — this class is just a
client. `start_platform` and `cleanup_platform` are intentional no-ops:
the pool owns the kernel, not the wrapper.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx
from strands_tools.code_interpreter.code_interpreter import CodeInterpreter
from strands_tools.code_interpreter.models import (
    ExecuteCodeAction,
    ExecuteCommandAction,
    InitSessionAction,
    LanguageType,
    ListFilesAction,
    ReadFilesAction,
    RemoveFilesAction,
    WriteFilesAction,
)

log = logging.getLogger(__name__)


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Strands tool success envelope: `{status, content: [{text: <json>}]}`.

    `json.dumps` (not `str()`) on the inner payload — `str()` would emit
    Python `repr` with single quotes, which `json.loads` can't parse.
    That bug is the entire reason the old code at agent/server/main.py
    bypassed the strands_tools wrapper to call the bedrock-agentcore SDK
    directly. We don't repeat it.
    """
    return {"status": "success", "content": [{"text": json.dumps(payload, default=str)}]}


def _err(message: str, **extra: Any) -> dict[str, Any]:
    """Strands tool error envelope. `extra` adds debug fields the model can read."""
    payload: dict[str, Any] = {"ok": False, "error": message}
    payload.update(extra)
    return {"status": "error", "content": [{"text": json.dumps(payload, default=str)}]}


class RemoteSandboxCodeInterpreter(CodeInterpreter):
    """HTTP-backed `CodeInterpreter`. One instance ↔ one sandbox task."""

    def __init__(
        self,
        *,
        http_url: str,
        auth_token: str,
        session_name: str,
        request_timeout_s: float = 180.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        """
        Args:
            http_url: Base URL of the sandbox task, e.g. `http://10.0.1.42:8081`.
            auth_token: Shared secret echoed in `X-Sandbox-Auth` on every call.
            session_name: Conversation-scoped name; passed through to the kernel
                purely as a label for logs and for the LLM-visible response.
                The sandbox kernel is one-per-task, not one-per-name.
            request_timeout_s: Per-HTTP-call timeout. Should comfortably exceed
                `SANDBOX_EXEC_TIMEOUT_SECONDS` on the sandbox so soft timeouts
                surface as HTTP 200 errors, not as torn-down sockets.
            http_client: Optional pre-built `httpx.Client` (used in tests to
                inject a mock transport).
        """
        super().__init__()
        self._base_url = http_url.rstrip("/")
        self._auth_token = auth_token
        self._session_name = session_name
        self._headers = {
            "X-Sandbox-Auth": auth_token,
            "Content-Type": "application/json",
        }
        # `httpx.Client` is reused across calls — TCP+TLS reuse cuts repeated
        # latency on rapid-fire executeCode tool calls in long chats.
        self._client: httpx.Client = http_client or httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=request_timeout_s,
        )
        self._owns_client = http_client is None

    # --------------------------------------------------------- platform lifecycle
    def start_platform(self) -> None:
        """No-op. The pool starts the task before it hands us the URL."""

    def cleanup_platform(self) -> None:
        """No-op. The pool stops the task on session teardown.

        We DO close the httpx client here so we don't leak sockets when an
        agent process is recycled — the pool's `release()` runs first, but
        the wrapper's `__del__` may run later.
        """
        if self._owns_client:
            try:
                self._client.close()
            except Exception as e:  # noqa: BLE001
                log.debug("httpx client close raised: %s", e)

    # --------------------------------------------------------- abstract methods
    def init_session(self, action: InitSessionAction) -> dict[str, Any]:
        """Sessions are implicit: one task = one kernel. No remote call.

        We accept the action for protocol parity and return a confirmation
        carrying the session name so downstream tools can match.
        """
        name = action.session_name or self._session_name
        return _ok(
            {
                "ok": True,
                "session_name": name,
                "description": action.description,
                "implicit": True,
            }
        )

    def list_local_sessions(self) -> dict[str, Any]:
        return _ok({"ok": True, "sessions": [{"name": self._session_name}]})

    def execute_code(self, action: ExecuteCodeAction) -> dict[str, Any]:
        if action.language != LanguageType.PYTHON:
            return _err(f"unsupported language: {action.language}; only python")
        if action.clear_context:
            # `clear_context=True` would require restarting the kernel, which
            # we deliberately don't support — single-use tasks make this
            # unnecessary, and a kernel restart mid-session would lose all
            # the dataframes the LLM has been building.
            return _err(
                "clear_context is not supported on the remote sandbox; start a new conversation for a fresh kernel",
            )
        return self._post(
            "/execute_code",
            {
                "code": action.code,
                "language": "python",
                "session_name": action.session_name or self._session_name,
            },
        )

    def execute_command(self, action: ExecuteCommandAction) -> dict[str, Any]:
        return self._post("/execute_command", {"command": action.command})

    def write_files(self, action: WriteFilesAction) -> dict[str, Any]:
        body = {"content": [{"path": fc.path, "text": fc.text} for fc in action.content]}
        return self._post("/write_files", body)

    def read_files(self, action: ReadFilesAction) -> dict[str, Any]:
        return self._post("/read_files", {"paths": list(action.paths)})

    def list_files(self, action: ListFilesAction) -> dict[str, Any]:
        return self._post("/list_files", {"path": action.path})

    def remove_files(self, action: RemoveFilesAction) -> dict[str, Any]:
        return self._post("/remove_files", {"paths": list(action.paths)})

    def get_supported_languages(self) -> list[LanguageType]:
        # JS/TS in the AgentCore wrapper only existed because AgentCore
        # supported them. Our sandbox runs Python only, so we advertise
        # only Python — the LLM won't ask for languages we can't deliver.
        return [LanguageType.PYTHON]

    # ----------------------------------------------------- host-side helpers
    # These are NOT decorated as Strands tools. They're called from the
    # FastAPI request handler in agent/server/main.py to back the
    # display_dataframe / display_plotly / display_image tools without
    # round-tripping bytes through the LLM.

    def read_text_file(self, path: str) -> str:
        """Return the sandbox file at `path` as a UTF-8 string.

        Raises if the file is missing, isn't valid UTF-8, or the response
        is malformed. Used by `display_dataframe` (CSV/JSON) and
        `display_plotly` (figure JSON).
        """
        files = self._read_files_raw([path])
        if not files:
            raise RuntimeError(f"no file payload returned for {path!r}")
        if len(files) > 1:
            raise RuntimeError(f"expected exactly one file in response, got {len(files)}")
        f = files[0]
        text = f.get("text")
        if text is None:
            blob_b64 = f.get("blob_b64")
            if blob_b64:
                # Could happen for a binary file the host asked for as text.
                # Decode best-effort — the caller will likely fail downstream
                # if the bytes aren't actually UTF-8, which is the right
                # signal: don't paper over a wrong path.
                raw = base64.b64decode(blob_b64)
                return raw.decode("utf-8")
            raise RuntimeError(f"sandbox response had neither text nor blob_b64 for {path!r}")
        return text

    def read_binary_file_as_data_url(self, path: str, mime: str = "image/png") -> str:
        """Return the sandbox file at `path` as a `data:<mime>;base64,...` URL.

        Used by `display_image`. The sandbox already returns binary files
        as base64, so this is a one-step concat — no extra `executeCommand`
        + `base64 -w 0` round-trip the way the AgentCore version did.
        """
        files = self._read_files_raw([path])
        if not files:
            raise RuntimeError(f"no file payload returned for {path!r}")
        f = files[0]
        blob_b64 = f.get("blob_b64")
        if blob_b64 is None:
            text = f.get("text")
            if text is None:
                raise RuntimeError(f"sandbox response had neither text nor blob_b64 for {path!r}")
            # If the file decoded as UTF-8 (e.g. SVG), re-encode for the URL.
            blob_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return f"data:{mime};base64,{blob_b64}"

    # ---------------------------------------------------------- transport
    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(path, json=body)
        except httpx.HTTPError as e:
            log.warning("sandbox POST %s failed: %s", path, e)
            return _err(f"sandbox transport error: {e}", path=path)
        if resp.status_code >= 400:
            return _err(
                f"sandbox returned HTTP {resp.status_code}",
                path=path,
                http_status=resp.status_code,
                body=resp.text[:500],
            )
        try:
            data = resp.json()
        except ValueError as e:
            return _err(f"sandbox returned non-JSON body: {e}", path=path)
        # Sandbox returns `{status, data}`; we re-wrap into `{status, content: [{text}]}`
        # so the model sees a Strands-shaped response. The original `data` is
        # preserved verbatim under `result`.
        status = data.get("status", "success")
        payload = {"ok": status == "success", "result": data.get("data", {})}
        return {"status": status, "content": [{"text": json.dumps(payload, default=str)}]}

    def _read_files_raw(self, paths: list[str]) -> list[dict[str, Any]]:
        """Return the raw `files` list from a `/read_files` response.

        Used by the host-side helpers; they need direct access to the
        payload, not the LLM-shaped `{status, content}` envelope. Hits
        the same endpoint as the LLM-facing `read_files()` but parses
        the response directly.
        """
        resp = self._client.post("/read_files", json={"paths": paths})
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"sandbox /read_files reported non-success: {body!r}")
        files = body.get("data", {}).get("files")
        if not isinstance(files, list):
            raise RuntimeError(f"sandbox /read_files response missing `files` list: {body!r}")
        return files
