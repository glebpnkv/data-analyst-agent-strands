import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BaseSettings:
    """Server-level settings shared across all agents.

    Agent-specific config (region, model id, role ARNs, etc.) is read by
    each agent's factory directly from env — keeping it out of the shared
    surface so the scaffold doesn't grow per-agent fields.
    """

    service_auth_secret: str | None
    session_ttl_seconds: int
    max_sessions: int


def load_base_settings() -> BaseSettings:
    return BaseSettings(
        service_auth_secret=os.environ.get("AGENT_SERVICE_AUTH_SECRET") or None,
        session_ttl_seconds=int(os.environ.get("AGENT_SESSION_TTL_SECONDS", "1800")),
        max_sessions=int(os.environ.get("AGENT_MAX_SESSIONS", "32")),
    )
