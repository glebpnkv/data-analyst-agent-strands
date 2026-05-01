"""Logging + tracing setup for the agent FastAPI service.

What you get out of the box:
- Stdout logs at the configured level (default INFO) with timestamps and
  module names — including the agent_server.display_tools logger that
  reports every display_* tool call.
- A per-process run directory at `runs/<service-name>/<timestamp>/`
  containing:
    server.log             — duplicate of stdout, useful for `tail -f`
    strands_traces.jsonl   — Strands' OpenTelemetry spans, one JSON per line

Optional OTLP export (Langfuse, Phoenix, Jaeger, anything OTel-shaped)
turns on automatically when `OTEL_EXPORTER_OTLP_ENDPOINT` is set —
Strands' own `setup_otlp_exporter()` reads the standard `OTEL_*`
environment variables. Example for Langfuse Cloud:

    export OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
    export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(public:secret)>"
    export AGENT_OTLP_ENABLE=1

Env vars consumed here:
- AGENT_LOG_LEVEL          — default "INFO"
- AGENT_RUN_DIR            — default "runs"; can be relative or absolute
- AGENT_OTLP_ENABLE        — "1" / "true" / "yes" to enable OTLP export
"""

import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)


@dataclass
class Observability:
    """Handles installed at boot; close on app shutdown."""

    run_dir: Path
    log_file: IO[str] | None = None
    trace_file: IO[str] | None = None
    closables: list[object] = field(default_factory=list)

    def close(self) -> None:
        for fp in (self.log_file, self.trace_file):
            if fp is not None:
                with suppress(Exception):
                    fp.close()


def setup(*, service_name: str) -> Observability:
    """Configure logging + tracing for the FastAPI service. Idempotent."""
    level_name = (os.environ.get("AGENT_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    base_run_dir = Path(os.environ.get("AGENT_RUN_DIR") or "runs")
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = base_run_dir / service_name / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    log_file_path = run_dir / "server.log"
    log_file = open(log_file_path, "a", encoding="utf-8")

    _configure_root_logging(level=level, log_file=log_file)

    trace_file = _setup_strands_telemetry(run_dir)

    log.info("observability ready: logs + traces under %s", run_dir.resolve())
    log.info("AGENT_LOG_LEVEL=%s, AGENT_RUN_DIR=%s", level_name, base_run_dir)
    if trace_file is None:
        log.info("Strands telemetry not configured (strands not importable?)")
    if _otlp_enabled():
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "(default)"
        log.info("OTLP export enabled, endpoint=%s", endpoint)
    else:
        log.debug("OTLP export disabled (set AGENT_OTLP_ENABLE=1 + OTEL_EXPORTER_OTLP_ENDPOINT to enable)")

    return Observability(run_dir=run_dir, log_file=log_file, trace_file=trace_file)


def _configure_root_logging(*, level: int, log_file: IO[str]) -> None:
    """Reset the root logger so our format wins over uvicorn's default."""
    root = logging.getLogger()
    # Remove any handlers installed by other code paths (uvicorn, basicConfig).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)
    root.addHandler(stream_handler)

    file_handler = logging.StreamHandler(log_file)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    root.setLevel(level)

    # Be explicit about levels for our own packages so a future
    # third-party basicConfig() can't silence them.
    for name in ("agent_server", "agent_server.display_tools", "server", "strands"):
        logging.getLogger(name).setLevel(level)

    # Quieten the chattier libraries unless the user asked for DEBUG.
    if level > logging.DEBUG:
        for name in ("httpx", "httpcore", "botocore", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)


def _setup_strands_telemetry(run_dir: Path) -> IO[str] | None:
    """File-based span export plus optional OTLP. Mirrors main.py setup."""
    try:
        from strands.telemetry import StrandsTelemetry
    except Exception as e:
        log.debug("Strands telemetry unavailable: %s", e)
        return None

    trace_path = run_dir / "strands_traces.jsonl"
    trace_file = open(trace_path, "a", encoding="utf-8")

    telemetry = StrandsTelemetry()
    telemetry.setup_console_exporter(
        out=trace_file,
        formatter=lambda span: span.to_json() + "\n",
    )

    enable_otlp = _otlp_enabled()
    if enable_otlp:
        try:
            telemetry.setup_otlp_exporter()
        except Exception as e:
            log.warning("OTLP exporter setup failed: %s", e)

        # Strands' own spans use OTel GenAI conventions (gen_ai.usage.*),
        # but Phoenix's token/cost UI is built around OpenInference
        # (llm.token_count.*, llm.model_name). The Bedrock instrumentor
        # patches boto3 bedrock-runtime calls and emits OpenInference-
        # conformant child spans nested inside Strands' parents — best of
        # both worlds: agent-level structure from Strands + per-LLM-call
        # token/cost detail from OpenInference.
        _instrument_bedrock_for_openinference()

    telemetry.setup_meter(
        enable_console_exporter=False,
        enable_otlp_exporter=enable_otlp,
    )

    return trace_file


def _instrument_bedrock_for_openinference() -> None:
    try:
        from openinference.instrumentation.bedrock import BedrockInstrumentor
    except Exception as e:
        log.debug("openinference-instrumentation-bedrock not installed: %s", e)
        return

    instrumentor = BedrockInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        log.debug("Bedrock instrumentor already installed")
        return

    try:
        instrumentor.instrument()
        log.info("OpenInference Bedrock instrumentation enabled (Phoenix will show tokens + cost)")
    except Exception as e:
        log.warning("OpenInference Bedrock instrumentation failed: %s", e)


def _otlp_enabled() -> bool:
    raw = (os.environ.get("AGENT_OTLP_ENABLE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}
