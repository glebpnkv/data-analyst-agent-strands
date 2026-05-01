"""Container entrypoint: fetch DB credentials, apply schema, exec Chainlit.

The deployed frontend doesn't have psql in the image (we'd add 50+ MB
to install postgresql-client). Instead this script:

  1. Reads `DB_SECRET_ARN` (set by the CDK compute stack) and fetches
     the JSON secret payload from Secrets Manager via boto3.
  2. Builds an asyncpg-shape DATABASE_URL and exports it for the
     Chainlit process.
  3. Applies `scripts/local_stack/chainlit_schema.sql` over an asyncpg
     connection — same SQL, same idempotency (CREATE/ALTER ... IF NOT
     EXISTS).
  4. Exec's `chainlit run app.py ...` so Chainlit replaces this process
     and ECS sees a single container, not a parent + child.

Local dev path (run_local_stack.sh) doesn't go through this entrypoint
— that script applies the schema via psql against the local container
and Chainlit runs directly. So this code path only fires inside the
deployed container.

If `DB_SECRET_ARN` is unset, we assume DATABASE_URL was supplied
externally (or no persistence is wanted) and skip the SM fetch + schema
apply. That keeps the entrypoint usable for ad-hoc `docker run` testing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("frontend.entrypoint")

# Hosted in the agent repo at scripts/local_stack/chainlit_schema.sql.
# The Dockerfile copies it into /app/chainlit_schema.sql at image-build
# time so we don't have to ship the whole repo into the runtime image.
SCHEMA_PATH = Path("/app/chainlit_schema.sql")

CHAINLIT_HOST = os.environ.get("CHAINLIT_HOST", "0.0.0.0")
CHAINLIT_PORT = os.environ.get("CHAINLIT_PORT", "8000")


def main() -> None:
    db_secret_arn = os.environ.get("DB_SECRET_ARN", "").strip()
    if db_secret_arn:
        try:
            database_url = _resolve_database_url(db_secret_arn)
        except Exception:
            log.exception("Failed to fetch DB credentials; aborting before chainlit start")
            sys.exit(1)
        os.environ["DATABASE_URL"] = database_url
        log.info("DATABASE_URL set from %s", db_secret_arn)

        try:
            asyncio.run(_apply_schema(database_url))
        except Exception:
            log.exception("Failed to apply chainlit schema; aborting before chainlit start")
            sys.exit(1)
    else:
        log.info(
            "DB_SECRET_ARN unset — skipping SM fetch + schema apply. "
            "DATABASE_URL kept as-is (%s).",
            "set" if os.environ.get("DATABASE_URL") else "unset",
        )

    _exec_chainlit()


def _resolve_database_url(secret_arn: str) -> str:
    import boto3  # noqa: PLC0415  (lazy: only when DB persistence is enabled)

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_arn)
    payload = json.loads(response["SecretString"])

    # CDK's rds.Credentials.from_generated_secret produces this exact
    # JSON shape: { username, password, host, port, dbname, engine,
    # dbInstanceIdentifier }.
    user = payload["username"]
    password = payload["password"]
    host = payload["host"]
    port = int(payload["port"])
    dbname = payload.get("dbname") or "chainlit"

    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{dbname}"


async def _apply_schema(database_url: str) -> None:
    if not SCHEMA_PATH.exists():
        log.warning("schema file missing at %s; skipping apply", SCHEMA_PATH)
        return

    # Lazy imports so the entrypoint stays importable in environments
    # without sqlalchemy installed (e.g. unit tests).
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    sql = SCHEMA_PATH.read_text()
    # Postgres' DDL is multi-statement and IF NOT EXISTS-friendly, but
    # asyncpg can't take a single multi-statement string via execute().
    # Strip SQL line comments first — `--` inside our schema's comment
    # lines contains semicolons (e.g. "container start;") that would
    # otherwise split a single comment block into bogus statements. Our
    # schema has no string literals containing `--`, so a plain prefix
    # strip per line is safe.
    statements = _split_sql(sql)
    log.info("applying %d schema statement(s)", len(statements))

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            for stmt in statements:
                await conn.execute(text(stmt))
    finally:
        await engine.dispose()
    log.info("schema apply complete")


def _split_sql(sql: str) -> list[str]:
    """Strip line comments, then split on semicolons. Returns non-empty stripped statements."""
    lines: list[str] = []
    for line in sql.splitlines():
        idx = line.find("--")
        if idx >= 0:
            line = line[:idx]
        lines.append(line)
    cleaned = "\n".join(lines)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


def _exec_chainlit() -> None:
    # Replace this process with chainlit so PID 1 inside the container
    # is the actual app — clean shutdown signal handling, no zombie
    # parent waiting on a child.
    import chainlit  # noqa: F401, PLC0415  (verify install before exec)

    args = [
        sys.executable,
        "-m",
        "chainlit",
        "run",
        "/app/frontend/app.py",
        "--host",
        CHAINLIT_HOST,
        "--port",
        CHAINLIT_PORT,
        "--headless",
    ]
    log.info("exec %s", " ".join(args))
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
