"""Pydantic request/response models for the sandbox HTTP API.

Field shapes mirror the Strands `code_interpreter` action models so the
client (`agent/sandbox_client.py`) can translate without repacking. Where
Strands uses `content=[FileContent(path, text)]`, so do we.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ExecuteCodeRequest(BaseModel):
    code: str
    language: Literal["python"] = "python"
    # `session_name` is informational — the kernel is one-per-container,
    # not one-per-session-name. The agent passes its conversation-scoped
    # name so it shows up in our logs alongside agent-side traces.
    session_name: str | None = None


class ExecuteCommandRequest(BaseModel):
    command: str


class FileContent(BaseModel):
    path: str
    text: str


class WriteFilesRequest(BaseModel):
    content: list[FileContent] = Field(default_factory=list)


class ReadFilesRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class ListFilesRequest(BaseModel):
    path: str = "."


class RemoveFilesRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class InstallPackagesRequest(BaseModel):
    packages: list[str] = Field(default_factory=list)
    upgrade: bool = False


class FilePayload(BaseModel):
    path: str
    mime: str
    # Exactly one of `text` or `blob_b64` is set per file: text for
    # UTF-8-decodable content (CSVs, plotly JSON), blob_b64 for binary
    # (PNG, JPEG). Lets the host-side `read_binary_file_as_data_url`
    # build a `data:image/png;base64,...` URL without a second round
    # trip through `executeCommand` + `base64 -w 0`.
    text: str | None = None
    blob_b64: str | None = None


class StatusResponse(BaseModel):
    status: Literal["success", "error"]
    # Free-form payload. Each endpoint documents what it puts here.
    # Kept as `dict[str, Any]` so we don't need a discriminated union
    # for every endpoint — the schema is owned by the wire contract,
    # not by Pydantic.
    data: dict[str, Any] = Field(default_factory=dict)
