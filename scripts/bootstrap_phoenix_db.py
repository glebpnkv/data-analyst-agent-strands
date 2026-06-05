"""Create the Phoenix logical database on the existing RDS instance.

Runs inside the deployed `frontend` ECS task (it already has boto3,
asyncpg, sqlalchemy in its image and is the only task allowed through
the RDS security group at deploy time). The shell wrapper
`scripts/bootstrap_phoenix_db.sh` invokes this via
`aws ecs execute-command`.

Why this lives here and not in CloudFormation: RDS does not expose
`CREATE DATABASE` as an IaC primitive. The only ways to create a
logical database inside an RDS instance are (a) a one-shot SQL call
against the running cluster, or (b) registering a CloudFormation custom
resource to do the same. (a) is simpler, idempotent (CREATE DATABASE
... IF NOT EXISTS isn't supported on Postgres, but we catch the
"already exists" SQLSTATE), and easy to re-run.

Env vars (set by the frontend ECS task def — same as entrypoint.py):
- DB_SECRET_ARN   ARN of the auto-generated RDS master-creds secret
- AWS_REGION      region for the boto3 client (falls back to AWS_DEFAULT_REGION)

Optional:
- PHOENIX_DB_NAME    target logical DB name; default "phoenix"
- PHOENIX_DB_OWNER   role that owns the new DB; default the master username
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scripts.bootstrap_phoenix_db")

# Postgres SQLSTATE: duplicate_database. We treat this as success so the
# script is safe to re-run, including after a partial deploy.
DUPLICATE_DATABASE_SQLSTATE = "42P04"


def main() -> int:
    secret_arn = os.environ.get("DB_SECRET_ARN", "").strip()
    if not secret_arn:
        log.error("DB_SECRET_ARN is required (set by the frontend task def)")
        return 2

    target_db = os.environ.get("PHOENIX_DB_NAME") or "phoenix"
    target_owner_override = os.environ.get("PHOENIX_DB_OWNER", "").strip()

    try:
        master_url, master_user = _resolve_master_url_and_user(secret_arn)
    except Exception:
        log.exception("Failed to fetch DB credentials")
        return 1

    owner = target_owner_override or master_user

    try:
        asyncio.run(_create_db_if_missing(master_url, target_db, owner))
    except Exception:
        log.exception("CREATE DATABASE failed")
        return 1

    return 0


def _resolve_master_url_and_user(secret_arn: str) -> tuple[str, str]:
    """Build an asyncpg URL pointing at the master DB on this RDS instance."""
    import boto3  # noqa: PLC0415

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_arn)
    payload = json.loads(response["SecretString"])

    user = payload["username"]
    password = payload["password"]
    host = payload["host"]
    port = int(payload["port"])
    # Connect to the master DB (the one named after the username, set up
    # by `Credentials.from_generated_secret`). CREATE DATABASE is allowed
    # while connected to any DB other than the target.
    dbname = payload.get("dbname") or "chainlit"

    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{dbname}", user


async def _create_db_if_missing(master_url: str, target_db: str, owner: str) -> None:
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    # CREATE DATABASE cannot run inside a transaction. asyncpg via
    # SQLAlchemy needs AUTOCOMMIT isolation on the connection to issue
    # it directly.
    engine = create_async_engine(master_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            try:
                # Identifier quoting via format-string is fine here — the
                # values come from env (operator-controlled), not user
                # input. Postgres rejects invalid identifiers anyway.
                await conn.execute(text(f'CREATE DATABASE "{target_db}" OWNER "{owner}"'))
                log.info("created database %s OWNER %s", target_db, owner)
            except Exception as e:  # asyncpg.exceptions.DuplicateDatabaseError
                sqlstate = getattr(e, "sqlstate", None) or _walk_for_sqlstate(e)
                if sqlstate == DUPLICATE_DATABASE_SQLSTATE:
                    log.info("database %s already exists; nothing to do", target_db)
                else:
                    raise
    finally:
        await engine.dispose()


def _walk_for_sqlstate(exc: BaseException) -> str | None:
    """SQLAlchemy wraps the asyncpg error — walk the __cause__ chain."""
    cur: BaseException | None = exc
    while cur is not None:
        candidate = getattr(cur, "sqlstate", None)
        if candidate:
            return candidate
        cur = cur.__cause__
    return None


if __name__ == "__main__":
    sys.exit(main())
