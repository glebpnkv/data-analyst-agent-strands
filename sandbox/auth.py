"""Shared-secret bearer auth for sandbox endpoints.

The agent injects a per-process token into the sandbox container's
`SANDBOX_AUTH_TOKEN` env var via `RunTask` `containerOverrides`. Every
sandbox request must echo it back in the `X-Sandbox-Auth` header.

The security boundary is the security group, not this header — the
sandbox SG only allows ingress from the agent task SG. The header is
belt-and-braces: it stops a confused agent from talking to the wrong
sandbox if multiple are reachable, and it makes log-sniffing or
accidental misconfiguration into a 401 instead of a foot-gun.
"""

import hmac
import os

from fastapi import Header, HTTPException, status

EXPECTED_TOKEN = os.environ.get("SANDBOX_AUTH_TOKEN", "")


def require_auth(x_sandbox_auth: str = Header(default="")) -> None:
    """FastAPI dependency: 401 unless `X-Sandbox-Auth` matches the env token.

    Constant-time compare so the response time doesn't leak information
    about how many leading characters of the token matched.
    """
    if not EXPECTED_TOKEN:
        # Misconfiguration: the container started without a token. Refuse
        # everything rather than running open. (`/healthz` doesn't take this
        # dep, so the pool's readiness probe still works.)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sandbox started without SANDBOX_AUTH_TOKEN",
        )
    if not hmac.compare_digest(EXPECTED_TOKEN, x_sandbox_auth):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Sandbox-Auth header",
        )
