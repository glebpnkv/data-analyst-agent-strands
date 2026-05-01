import hmac

from fastapi import Request
from fastapi.responses import JSONResponse

SERVICE_AUTH_HEADER = "X-Service-Auth"
PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})


async def service_auth_middleware(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    expected = request.app.state.settings.service_auth_secret
    if expected is None:
        # Auth disabled — only acceptable for local dev. Logged at boot in app.py.
        return await call_next(request)

    # Returning a Response directly (rather than raising HTTPException) is
    # required: BaseHTTPMiddleware does not translate raised exceptions into
    # FastAPI's HTTPException handlers.
    provided = request.headers.get(SERVICE_AUTH_HEADER, "")
    if not hmac.compare_digest(provided, expected):
        return JSONResponse(
            status_code=401,
            content={"detail": "invalid or missing service auth header"},
        )
    return await call_next(request)
