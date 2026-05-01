FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /uvx /usr/local/bin/

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Project metadata first for cache-friendly dep installs. NOTE: the root
# pyproject is shared between agent + frontend — Phase 4 splits this into
# uv workspaces so the frontend image isn't dragging boto3/langchain.
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY frontend /app/frontend
# Chainlit data layer schema — the entrypoint applies it idempotently
# against the deployed RDS on every container start (CREATE / ALTER ...
# IF NOT EXISTS, see scripts/local_stack/chainlit_schema.sql for the
# full story). We copy the SQL into the image rather than fetching it
# at runtime so a container in a private subnet without internet egress
# still works.
COPY scripts/local_stack/chainlit_schema.sql /app/chainlit_schema.sql

EXPOSE 8000

# entrypoint.py reads DB_SECRET_ARN from env, fetches DB credentials
# from SM via boto3, applies the schema, then exec's chainlit. Local
# dev (run_local_stack.sh) bypasses this entirely — it applies the
# schema via psql before launching Chainlit directly. So the code path
# in entrypoint.py only runs in the deployed container.
CMD ["uv", "run", "--no-dev", "--frozen", \
     "python", "/app/frontend/entrypoint.py"]
