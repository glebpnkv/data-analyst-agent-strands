"""Unit tests for RemoteSandboxCodeInterpreter.

The client is the LLM-facing adapter from Strands action models to the
sandbox HTTP API. These tests confirm:
  - Each Strands action becomes the right URL + body.
  - Sandbox responses get rewrapped into the Strands `{status, content}`
    envelope without losing the inner payload.
  - The two host-side helpers (read_text_file, read_binary_file_as_data_url)
    parse the `/read_files` response into the shapes the display tools
    expect.
  - Half-set/missing inputs fail loud (LLM gets an error, not a 500).

We never make a real HTTP call. The tests inject a `MagicMock` standing
in for `httpx.Client`, so the assertions are purely about the wire
contract: URL paths, JSON bodies, and how the wrapper interprets the
response. Drift in the contract surfaces here before it surfaces in
end-to-end runs.
"""

import json
from unittest.mock import MagicMock

import pytest
from strands_tools.code_interpreter.models import (
    ExecuteCodeAction,
    ExecuteCommandAction,
    FileContent,
    InitSessionAction,
    LanguageType,
    ListFilesAction,
    ReadFilesAction,
    RemoveFilesAction,
    WriteFilesAction,
)

from sandbox_client import RemoteSandboxCodeInterpreter


def _mock_client(response_data: dict, status_code: int = 200) -> MagicMock:
    """Build a MagicMock standing in for `httpx.Client` on a successful POST."""
    client = MagicMock()
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = response_data
    response.text = json.dumps(response_data)
    response.raise_for_status = MagicMock()
    client.post.return_value = response
    return client


def _make_ci(client: MagicMock, *, session_name: str = "s1") -> RemoteSandboxCodeInterpreter:
    return RemoteSandboxCodeInterpreter(
        http_url="http://sandbox.test:8081",
        auth_token="t0k",
        session_name=session_name,
        http_client=client,
    )


def _decode_content(result: dict) -> dict:
    """Pull the JSON payload out of the Strands `{status, content}` envelope."""
    return json.loads(result["content"][0]["text"])


# ---------------------------------------------------------------- execute_code


def test_execute_code_posts_to_execute_code_endpoint():
    client = _mock_client({"status": "success", "data": {"ok": True, "stdout": "5\n"}})
    ci = _make_ci(client)
    result = ci.execute_code(
        ExecuteCodeAction(type="executeCode", code="print(5)", language=LanguageType.PYTHON)
    )

    assert client.post.call_args.args[0] == "/execute_code"
    body = client.post.call_args.kwargs["json"]
    assert body == {"code": "print(5)", "language": "python", "session_name": "s1"}
    assert result["status"] == "success"
    inner = _decode_content(result)
    assert inner["ok"] is True
    assert inner["result"]["stdout"] == "5\n"


def test_execute_code_uses_action_session_name_when_provided():
    client = _mock_client({"status": "success", "data": {"ok": True}})
    ci = _make_ci(client, session_name="default-name")
    ci.execute_code(
        ExecuteCodeAction(type="executeCode", code="x=1", session_name="custom-name")
    )
    assert client.post.call_args.kwargs["json"]["session_name"] == "custom-name"


def test_execute_code_rejects_non_python_language():
    """We advertise only python; refusing JS/TS in the wrapper avoids
    wasting an HTTP round-trip plus a sandbox-side 400."""
    client = _mock_client({"status": "success", "data": {"ok": True}})
    ci = _make_ci(client)
    result = ci.execute_code(
        ExecuteCodeAction(type="executeCode", code="x=1", language=LanguageType.JAVASCRIPT)
    )
    assert client.post.called is False
    assert result["status"] == "error"
    inner = _decode_content(result)
    assert "unsupported language" in inner["error"]


def test_execute_code_rejects_clear_context():
    """clear_context would require a kernel restart, which we don't
    support — single-use tasks make it unnecessary, and a mid-session
    restart would lose all the dataframes."""
    client = _mock_client({"status": "success", "data": {"ok": True}})
    ci = _make_ci(client)
    result = ci.execute_code(
        ExecuteCodeAction(type="executeCode", code="x=1", clear_context=True)
    )
    assert client.post.called is False
    assert result["status"] == "error"
    assert "clear_context" in _decode_content(result)["error"]


# ---------------------------------------------------------------- execute_command


def test_execute_command_translates_to_post():
    client = _mock_client(
        {"status": "success", "data": {"ok": True, "stdout": "ok\n", "exit_code": 0}}
    )
    ci = _make_ci(client)
    result = ci.execute_command(ExecuteCommandAction(type="executeCommand", command="ls -la"))

    assert client.post.call_args.args[0] == "/execute_command"
    assert client.post.call_args.kwargs["json"] == {"command": "ls -la"}
    assert result["status"] == "success"


# ---------------------------------------------------------------- write/read/list/remove


def test_write_files_unpacks_pydantic_models_into_plain_dicts():
    """The wire contract uses plain dicts; the action model uses
    pydantic FileContent objects. The wrapper has to flatten them."""
    client = _mock_client({"status": "success", "data": {"written": ["a.txt", "b.csv"]}})
    ci = _make_ci(client)
    ci.write_files(
        WriteFilesAction(
            type="writeFiles",
            content=[
                FileContent(path="a.txt", text="hello"),
                FileContent(path="b.csv", text="x,y\n1,2\n"),
            ],
        )
    )
    body = client.post.call_args.kwargs["json"]
    assert body == {
        "content": [
            {"path": "a.txt", "text": "hello"},
            {"path": "b.csv", "text": "x,y\n1,2\n"},
        ]
    }


def test_read_files_passes_paths_through():
    client = _mock_client(
        {
            "status": "success",
            "data": {"files": [{"path": "a.txt", "mime": "text/plain", "text": "hi"}]},
        }
    )
    ci = _make_ci(client)
    ci.read_files(ReadFilesAction(type="readFiles", paths=["a.txt"]))
    assert client.post.call_args.args[0] == "/read_files"
    assert client.post.call_args.kwargs["json"] == {"paths": ["a.txt"]}


def test_list_files_passes_path():
    client = _mock_client({"status": "success", "data": {"path": ".", "entries": []}})
    ci = _make_ci(client)
    ci.list_files(ListFilesAction(type="listFiles", path="data"))
    assert client.post.call_args.kwargs["json"] == {"path": "data"}


def test_remove_files_passes_paths():
    client = _mock_client({"status": "success", "data": {"removed": ["x"]}})
    ci = _make_ci(client)
    ci.remove_files(RemoveFilesAction(type="removeFiles", paths=["x", "y"]))
    assert client.post.call_args.args[0] == "/remove_files"
    assert client.post.call_args.kwargs["json"] == {"paths": ["x", "y"]}


# ---------------------------------------------------------------- session-shaped


def test_init_session_is_a_no_op_locally_returning_session_name():
    """The kernel is per-task, so init_session has nothing to claim
    remotely — but Strands callers expect a confirmation. We return one
    without firing an HTTP call."""
    client = _mock_client({"status": "success"})
    ci = _make_ci(client, session_name="abc")
    result = ci.init_session(InitSessionAction(type="initSession", description="demo"))
    assert client.post.called is False
    inner = _decode_content(result)
    assert inner["session_name"] == "abc"
    assert inner["implicit"] is True


def test_list_local_sessions_returns_single_implicit_session():
    client = _mock_client({})
    ci = _make_ci(client, session_name="my-session")
    inner = _decode_content(ci.list_local_sessions())
    assert inner["sessions"] == [{"name": "my-session"}]


# ---------------------------------------------------------------- supported langs


def test_get_supported_languages_is_python_only():
    """Advertising python-only stops the LLM from asking for JS/TS,
    which our sandbox doesn't run."""
    ci = _make_ci(_mock_client({}))
    assert ci.get_supported_languages() == [LanguageType.PYTHON]


# ---------------------------------------------------------------- transport errors


def test_http_4xx_becomes_strands_error_envelope():
    """A 400 from the sandbox shouldn't blow up the Strands tool call —
    we surface it as `status=error` with the response body in the
    payload so the LLM can react."""
    client = MagicMock()
    response = MagicMock()
    response.status_code = 400
    response.text = "bad request body"
    client.post.return_value = response
    ci = _make_ci(client)
    result = ci.execute_command(ExecuteCommandAction(type="executeCommand", command="x"))
    assert result["status"] == "error"
    inner = _decode_content(result)
    assert inner["http_status"] == 400
    assert "bad request body" in inner["body"]


def test_transport_exception_becomes_strands_error_envelope():
    """httpx.HTTPError on connect/read shouldn't propagate — caller
    sees the same `{status: error}` envelope as any other failure."""
    import httpx
    client = MagicMock()
    client.post.side_effect = httpx.ConnectError("dns failed")
    ci = _make_ci(client)
    result = ci.execute_command(ExecuteCommandAction(type="executeCommand", command="x"))
    assert result["status"] == "error"
    assert "transport error" in _decode_content(result)["error"]


# ---------------------------------------------------------------- host-side helpers


def test_read_text_file_returns_text_field():
    client = _mock_client(
        {
            "status": "success",
            "data": {"files": [{"path": "a.csv", "mime": "text/csv", "text": "x,y\n1,2\n"}]},
        }
    )
    ci = _make_ci(client)
    text = ci.read_text_file("a.csv")
    assert text == "x,y\n1,2\n"


def test_read_text_file_decodes_utf8_blob_when_text_missing():
    """If the sandbox happens to return a binary file as a blob_b64 but
    we asked for text, decode it best-effort. Wrong-format paths fail
    here, which is the right signal — don't paper over a bug."""
    import base64

    raw = "hello".encode("utf-8")
    blob = base64.b64encode(raw).decode("ascii")
    client = _mock_client(
        {
            "status": "success",
            "data": {"files": [{"path": "x", "mime": "application/octet-stream", "blob_b64": blob}]},
        }
    )
    ci = _make_ci(client)
    assert ci.read_text_file("x") == "hello"


def test_read_text_file_raises_on_multi_file_response():
    """One ask, one file. Anything else is a contract violation we
    don't want to silently paper over."""
    client = _mock_client(
        {
            "status": "success",
            "data": {
                "files": [
                    {"path": "a", "text": "1"},
                    {"path": "b", "text": "2"},
                ]
            },
        }
    )
    ci = _make_ci(client)
    with pytest.raises(RuntimeError, match="exactly one file"):
        ci.read_text_file("a")


def test_read_binary_file_as_data_url_assembles_mime_and_blob():
    blob = "iVBORw0KGgoFAKE"  # not real PNG bytes, just tagged b64
    client = _mock_client(
        {
            "status": "success",
            "data": {"files": [{"path": "p.png", "mime": "image/png", "blob_b64": blob}]},
        }
    )
    ci = _make_ci(client)
    url = ci.read_binary_file_as_data_url("p.png", mime="image/png")
    assert url == f"data:image/png;base64,{blob}"


def test_read_binary_file_as_data_url_reencodes_text_payload():
    """SVG (text but renderable) flows through the `text` field of the
    sandbox response. The data URL still needs base64, so we re-encode."""
    import base64
    svg_text = "<svg></svg>"
    expected = base64.b64encode(svg_text.encode("utf-8")).decode("ascii")
    client = _mock_client(
        {
            "status": "success",
            "data": {"files": [{"path": "p.svg", "mime": "image/svg+xml", "text": svg_text}]},
        }
    )
    ci = _make_ci(client)
    url = ci.read_binary_file_as_data_url("p.svg", mime="image/svg+xml")
    assert url == f"data:image/svg+xml;base64,{expected}"
