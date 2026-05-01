FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# uv is the canonical install tool for this repo.
COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /uvx /usr/local/bin/

WORKDIR /app

# System deps for boto3/SSL and for the AWS Glue MCP server subprocess.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
 && rm -rf /var/lib/apt/lists/*

# Project metadata first for cache-friendly dep installs.
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project

# Shared server scaffolding + this agent's code.
COPY agent_server /app/agent_server
COPY agent /app/agent

# /app makes `agent_server` importable; the agent dir makes `agent` and
# `server` importable as top-level modules (matches uvicorn --app-dir).
ENV PYTHONPATH=/app:/app/agent

EXPOSE 8080

# Single-worker: the agent is async + holds per-session subprocess state.
CMD ["uv", "run", "--no-dev", "--frozen", "uvicorn", \
     "server.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--app-dir", "/app/agent"]
