"""FastAPI sandbox server.

One per ECS task. Owns one IPython kernel (see kernel.py) and exposes
the action surface the agent's `RemoteSandboxCodeInterpreter` calls
into. Every endpoint except `/healthz` requires the shared-secret
header from `auth.py`.

Lifecycle:
  - On startup: create /workspace, start the kernel, kick off a
    watchdog task that exits the process on idle/hard-lifetime
    timeout. ECS marks the task STOPPED when the entrypoint exits,
    which is exactly what we want (single-use, fail-safe).
  - On shutdown: shut down the kernel cleanly so child processes
    don't leak.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException

from auth import require_auth
from kernel import SandboxKernel
from models import (
    ExecuteCodeRequest,
    ExecuteCommandRequest,
    InstallPackagesRequest,
    ListFilesRequest,
    ReadFilesRequest,
    RemoveFilesRequest,
    WriteFilesRequest,
)

logging.basicConfig(
    level=os.environ.get("SANDBOX_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("sandbox.server")

WORKSPACE = Path(os.environ.get("SANDBOX_WORKSPACE", "/workspace")).resolve()
EXEC_TIMEOUT_S = float(os.environ.get("SANDBOX_EXEC_TIMEOUT_SECONDS", "120"))
COMMAND_TIMEOUT_S = float(os.environ.get("SANDBOX_COMMAND_TIMEOUT_SECONDS", "60"))
IDLE_TIMEOUT_S = float(os.environ.get("SANDBOX_IDLE_TIMEOUT_SECONDS", "1800"))
HARD_LIFETIME_S = float(os.environ.get("SANDBOX_HARD_LIFETIME_SECONDS", "10800"))
INSTALL_PACKAGES_ENABLED = os.environ.get("SANDBOX_INSTALL_PACKAGES_ENABLED", "0") == "1"
WATCHDOG_INTERVAL_S = 30.0

PROCESS_START_MONO = time.monotonic()
_last_activity_at = time.monotonic()
_kernel = SandboxKernel(workdir=str(WORKSPACE))


def _touch_activity() -> None:
    global _last_activity_at
    _last_activity_at = time.monotonic()


def _safe_workspace_path(rel: str) -> Path:
    """Resolve `rel` inside `/workspace`, refusing escapes.

    `..` segments and absolute paths are normalized away by `.resolve()`,
    after which we verify the result still lives under WORKSPACE. We
    strip a leading `/` so that an LLM passing `/workspace/foo.csv`
    or `foo.csv` both end up at `/workspace/foo.csv` — friendlier than
    making the LLM remember whether to lead with a slash.

    We also strip a leading `~/` (and treat bare `~` as `.`). LLMs
    sometimes write `~/analysis_outputs/foo.json` expecting tilde to
    expand to a home directory; Python's `open()` doesn't expand it,
    so the write goes to a literal `~` subdirectory (or, more often,
    fails silently because the parent doesn't exist). Treating `~/...`
    as workspace-relative makes the HTTP read path forgiving — though
    the actual fix for that pattern is the sandbox-artifacts skill,
    which forbids `~/` in the LLM's prompts.
    """
    cleaned = rel
    if cleaned == "~":
        cleaned = "."
    elif cleaned.startswith("~/"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    if not cleaned or cleaned == ".":
        return WORKSPACE
    target = (WORKSPACE / cleaned).resolve()
    if target != WORKSPACE and WORKSPACE not in target.parents:
        raise HTTPException(status_code=400, detail=f"path escapes workspace: {rel!r}")
    return target


async def _watchdog() -> None:
    """Self-destruct on idle or hard-lifetime timeout.

    Defence in depth against orphan tasks. The pool's crash sweep is
    the primary line of defence; the watchdog catches the case where
    the agent fails to call StopTask AND fails to start cleanly to do
    a sweep (e.g. agent process keeps crash-looping indefinitely).
    """
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_S)
        now = time.monotonic()
        idle_for = now - _last_activity_at
        live_for = now - PROCESS_START_MONO
        if idle_for > IDLE_TIMEOUT_S:
            log.warning("idle for %.0fs (>%.0f); exiting", idle_for, IDLE_TIMEOUT_S)
            os._exit(0)
        if live_for > HARD_LIFETIME_S:
            log.warning("alive for %.0fs (>%.0f); exiting", live_for, HARD_LIFETIME_S)
            os._exit(0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    _kernel.start()
    log.info(
        "sandbox up (workspace=%s, exec_timeout=%.0fs, idle_timeout=%.0fs, hard_lifetime=%.0fs)",
        WORKSPACE, EXEC_TIMEOUT_S, IDLE_TIMEOUT_S, HARD_LIFETIME_S,
    )
    watchdog_task = asyncio.create_task(_watchdog())
    try:
        yield
    finally:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _kernel.shutdown()


app = FastAPI(title="data-analyst-sandbox", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    """Pool readiness probe — no auth, used by the warmer to gate enqueue."""
    return {
        "ok": True,
        "kernel_alive": _kernel.is_alive(),
        "poisoned": _kernel.poisoned,
        "uptime_s": time.monotonic() - PROCESS_START_MONO,
    }


@app.post("/execute_code", dependencies=[Depends(require_auth)])
async def execute_code(payload: ExecuteCodeRequest) -> dict:
    if payload.language != "python":
        raise HTTPException(status_code=400, detail="only python is supported")
    _touch_activity()
    result = await asyncio.to_thread(_kernel.execute, payload.code, EXEC_TIMEOUT_S)
    return {
        "status": "success" if result.ok else "error",
        "data": {
            "ok": result.ok,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.error,
            "result": result.result,
            "timed_out": result.timed_out,
            "poisoned": result.poisoned,
            "elapsed_s": result.elapsed_s,
        },
    }


@app.post("/execute_command", dependencies=[Depends(require_auth)])
async def execute_command(payload: ExecuteCommandRequest) -> dict:
    _touch_activity()

    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            payload.command,
            shell=True,  # noqa: S602 — the LLM is the principal here, see plan
            capture_output=True,
            text=True,
            cwd=str(WORKSPACE),
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )

    try:
        proc = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired as e:
        return {
            "status": "error",
            "data": {
                "ok": False,
                "error": f"command exceeded timeout of {COMMAND_TIMEOUT_S:.0f}s",
                "stdout": e.stdout or "",
                "stderr": e.stderr or "",
                "exit_code": None,
                "timed_out": True,
            },
        }
    return {
        "status": "success" if proc.returncode == 0 else "error",
        "data": {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "exit_code": proc.returncode,
            "timed_out": False,
        },
    }


@app.post("/write_files", dependencies=[Depends(require_auth)])
async def write_files(payload: WriteFilesRequest) -> dict:
    _touch_activity()
    written: list[str] = []
    for fc in payload.content:
        target = _safe_workspace_path(fc.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(fc.text, encoding="utf-8")
        written.append(str(target.relative_to(WORKSPACE)))
    return {"status": "success", "data": {"written": written}}


@app.post("/read_files", dependencies=[Depends(require_auth)])
async def read_files(payload: ReadFilesRequest) -> dict:
    _touch_activity()
    files: list[dict] = []
    for rel in payload.paths:
        target = _safe_workspace_path(rel)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"file not found: {rel!r}")
        if not target.is_file():
            raise HTTPException(status_code=400, detail=f"not a regular file: {rel!r}")
        raw = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        # Try utf-8 first; if it decodes cleanly the host can use the text
        # field directly. Otherwise fall back to a base64 blob so binaries
        # (PNG, parquet, etc.) survive the JSON round-trip.
        try:
            text = raw.decode("utf-8")
            files.append({"path": rel, "mime": mime, "text": text})
        except UnicodeDecodeError:
            files.append(
                {
                    "path": rel,
                    "mime": mime,
                    "blob_b64": base64.b64encode(raw).decode("ascii"),
                }
            )
    return {"status": "success", "data": {"files": files}}


@app.post("/list_files", dependencies=[Depends(require_auth)])
async def list_files(payload: ListFilesRequest) -> dict:
    _touch_activity()
    target = _safe_workspace_path(payload.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"path not found: {payload.path!r}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {payload.path!r}")
    entries: list[dict] = []
    for child in sorted(target.iterdir()):
        entries.append(
            {
                "name": child.name,
                "is_dir": child.is_dir(),
                "size_bytes": child.stat().st_size if child.is_file() else None,
            }
        )
    return {
        "status": "success",
        "data": {"path": str(target.relative_to(WORKSPACE)) or ".", "entries": entries},
    }


@app.post("/remove_files", dependencies=[Depends(require_auth)])
async def remove_files(payload: RemoveFilesRequest) -> dict:
    _touch_activity()
    removed: list[str] = []
    for rel in payload.paths:
        target = _safe_workspace_path(rel)
        if target == WORKSPACE:
            raise HTTPException(status_code=400, detail="refusing to remove workspace root")
        if target.is_file() or target.is_symlink():
            target.unlink()
            removed.append(rel)
        elif target.is_dir():
            # Directory removal is intentionally rmdir-only (non-recursive).
            # Recursive deletion of LLM-authored trees is too easy a foot-gun.
            target.rmdir()
            removed.append(rel)
        else:
            raise HTTPException(status_code=404, detail=f"not found: {rel!r}")
    return {"status": "success", "data": {"removed": removed}}


@app.post("/install_packages", dependencies=[Depends(require_auth)])
async def install_packages(payload: InstallPackagesRequest) -> dict:
    """Install pip packages into the kernel's environment.

    Disabled by default: the sandbox image bakes in pandas/plotly/numpy/
    pyarrow/matplotlib so the LLM shouldn't normally need this. To enable
    (e.g. for an exploratory dev session that needs a one-off package)
    set `SANDBOX_INSTALL_PACKAGES_ENABLED=1` on the task definition.
    """
    if not INSTALL_PACKAGES_ENABLED:
        return {
            "status": "error",
            "data": {
                "ok": False,
                "error": "install_packages is disabled in this sandbox image",
            },
        }
    _touch_activity()
    if not payload.packages:
        return {"status": "success", "data": {"installed": []}}
    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir"]
    if payload.upgrade:
        cmd.append("--upgrade")
    cmd.extend(payload.packages)

    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — args list, not shell-string
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    proc = await asyncio.to_thread(_run)
    return {
        "status": "success" if proc.returncode == 0 else "error",
        "data": {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "exit_code": proc.returncode,
        },
    }
