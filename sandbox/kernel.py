"""Wraps a jupyter_client kernel for the sandbox HTTP server.

Why a separate kernel process and not `IPython.InteractiveShell`?

  An out-of-process kernel is interruptible. If the LLM authors
  `while True: pass` (or anything tight enough that no Python checkpoint
  fires), in-process IPython would block the FastAPI worker including
  `/healthz`, and the only escape is killing the whole container. With
  jupyter_client, we send SIGINT to the kernel process via
  `interrupt_kernel()`, the FastAPI server stays responsive, and if the
  kernel ignores SIGINT (numpy/blas tight loops, native-code C extensions)
  we escalate to `shutdown_kernel(now=True)` and mark the task poisoned
  so the pool can release it on session end.

State persistence between calls is the same as in-process IPython
(variables, imports, dataframes survive across `/execute_code` calls
within one container's lifetime — i.e. one chat session).
"""

from __future__ import annotations

import logging
import queue
import re
import time
from dataclasses import dataclass, field

from jupyter_client.manager import KernelManager

log = logging.getLogger("sandbox.kernel")

# Strip ANSI color codes from tracebacks before we hand them back to the
# LLM — colored escape sequences confuse the model and bloat the response.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@dataclass
class ExecuteResult:
    """Outcome of one `/execute_code` call.

    `poisoned=True` means the kernel was force-killed mid-execution and
    the SandboxKernel is no longer usable. The agent should release the
    task and let the pool launch a fresh one for the next session.
    """

    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    result: str | None = None
    timed_out: bool = False
    poisoned: bool = False
    elapsed_s: float = 0.0
    streams: list[dict] = field(default_factory=list)


class SandboxKernel:
    def __init__(self, kernel_name: str = "python3", workdir: str = "/workspace"):
        self.kernel_name = kernel_name
        self.workdir = workdir
        self.km: KernelManager | None = None
        self.kc = None  # BlockingKernelClient
        self.poisoned = False

    def start(self) -> None:
        """Start the kernel subprocess and a synchronous client.

        `wait_for_ready` blocks until the kernel responds on the shell
        channel — typically <2s. We use a 30s ceiling because cold ECS
        host filesystems with many initial imports can be slow.
        """
        log.info("starting kernel %s with cwd=%s", self.kernel_name, self.workdir)
        self.km = KernelManager(kernel_name=self.kernel_name)
        self.km.start_kernel(cwd=self.workdir)
        self.kc = self.km.client()
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=30)
        log.info("kernel ready (pid=%s)", getattr(self.km, "kernel", None) and self.km.kernel.pid)

    def is_alive(self) -> bool:
        if not self.km or not self.km.has_kernel:
            return False
        try:
            return self.km.is_alive()
        except Exception:
            return False

    def execute(self, code: str, timeout_s: float) -> ExecuteResult:
        """Run `code` in the kernel and collect its iopub messages.

        Drains iopub until we see a `status: idle` for our own message
        id (or until `timeout_s` elapses). On timeout: SIGINT first,
        wait ~3s for the kernel to honour it; on failure to honour,
        force shutdown and mark poisoned.
        """
        if self.poisoned or not self.kc:
            return ExecuteResult(
                ok=False,
                error="kernel is unavailable (poisoned or not started)",
                poisoned=True,
            )
        msg_id = self.kc.execute(code, store_history=False, allow_stdin=False)
        started = time.monotonic()
        deadline = started + timeout_s

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        error_text: str | None = None
        result_text: str | None = None
        finished = False
        timed_out = False

        while not finished:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                msg = self.kc.get_iopub_msg(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                # iopub is shared across executions; drop unrelated messages.
                continue
            msg_type = msg.get("msg_type")
            content = msg.get("content", {})
            if msg_type == "stream":
                name = content.get("name", "stdout")
                text = content.get("text", "")
                if name == "stdout":
                    stdout_chunks.append(text)
                else:
                    stderr_chunks.append(text)
            elif msg_type == "execute_result":
                data = content.get("data", {}) or {}
                if "text/plain" in data:
                    result_text = data["text/plain"]
            elif msg_type == "error":
                tb_lines = content.get("traceback", []) or []
                error_text = _strip_ansi("\n".join(tb_lines))
            elif msg_type == "status":
                if content.get("execution_state") == "idle":
                    finished = True
            # display_data, update_display_data, clear_output: ignored —
            # the agent emits the user-visible artifacts via display_*
            # tools, not via inline iopub display data.

        elapsed = time.monotonic() - started

        if timed_out:
            self._on_timeout(msg_id)
            return ExecuteResult(
                ok=False,
                stdout="".join(stdout_chunks),
                stderr="".join(stderr_chunks),
                error=f"execution exceeded timeout of {timeout_s:.0f}s",
                timed_out=True,
                poisoned=self.poisoned,
                elapsed_s=elapsed,
            )

        return ExecuteResult(
            ok=error_text is None,
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
            error=error_text,
            result=result_text,
            timed_out=False,
            poisoned=False,
            elapsed_s=elapsed,
        )

    def _on_timeout(self, msg_id: str) -> None:
        """Try a soft interrupt; escalate to force-kill if it doesn't honour SIGINT.

        Pure-Python loops respond to SIGINT within a Python opcode boundary —
        the kernel emits a `KeyboardInterrupt` traceback and goes idle, all
        within ~milliseconds. Tight numpy/blas/native loops may not honour
        SIGINT until the C call returns, which can be never. The 3s grace
        is empirically generous for the soft-interrupt case; if we miss it,
        force-kill and mark poisoned.
        """
        if not self.km:
            self.poisoned = True
            return
        log.warning("execute timed out; sending SIGINT to kernel")
        try:
            self.km.interrupt_kernel()
        except Exception as e:
            log.warning("interrupt_kernel raised: %s", e)
            self.poisoned = True
            self._force_shutdown()
            return

        grace_deadline = time.monotonic() + 3.0
        while time.monotonic() < grace_deadline:
            try:
                msg = self.kc.get_iopub_msg(timeout=0.5)  # type: ignore[union-attr]
            except queue.Empty:
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            if (
                msg.get("msg_type") == "status"
                and msg.get("content", {}).get("execution_state") == "idle"
            ):
                log.info("kernel honoured SIGINT and returned to idle")
                return

        log.warning("kernel did not honour SIGINT within grace; force-killing")
        self.poisoned = True
        self._force_shutdown()

    def _force_shutdown(self) -> None:
        try:
            if self.km and self.km.has_kernel:
                self.km.shutdown_kernel(now=True)
        except Exception as e:
            log.warning("force shutdown raised: %s", e)

    def shutdown(self) -> None:
        try:
            if self.kc:
                self.kc.stop_channels()
        except Exception as e:
            log.debug("stop_channels raised: %s", e)
        try:
            if self.km and self.km.has_kernel:
                self.km.shutdown_kernel(now=False)
        except Exception as e:
            log.debug("shutdown_kernel raised: %s", e)
