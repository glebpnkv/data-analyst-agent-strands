import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import service_auth_middleware
from .config import BaseSettings, load_base_settings
from .events import error as sse_error
from .observability import setup as setup_observability
from .sessions import AgentFactory, SessionRegistry
from .streaming import ReducerFactory
from .ui_emitter import UIEmitter, reset_current_emitter, set_current_emitter

log = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None)
    prompt: str = Field(min_length=1)


def create_app(
    *,
    agent_factory: AgentFactory,
    reducer_factory: ReducerFactory,
    settings: BaseSettings | None = None,
    title: str = "agent-service",
    version: str = "0.1.0",
) -> FastAPI:
    """Build a FastAPI app that streams the v1 SSE protocol over /v1/chat.

    Args:
        agent_factory: Callable invoked per-session to build the agent.
        reducer_factory: Callable invoked per-request to build a fresh reducer.
        settings: Optional preloaded settings; defaults to `load_base_settings()`.
        title, version: FastAPI metadata.
    """
    resolved_settings = settings or load_base_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        observability = setup_observability(service_name=title)
        if resolved_settings.service_auth_secret is None:
            log.warning(
                "AGENT_SERVICE_AUTH_SECRET is unset — /v1/chat is unauthenticated. "
                "Acceptable only for local development."
            )
        registry = SessionRegistry(resolved_settings, agent_factory)
        app.state.settings = resolved_settings
        app.state.registry = registry
        app.state.observability = observability
        try:
            yield
        finally:
            await registry.shutdown()
            observability.close()

    app = FastAPI(title=title, version=version, lifespan=lifespan)
    app.add_middleware(BaseHTTPMiddleware, dispatch=service_auth_middleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/v1/chat")
    async def chat(req: ChatRequest, request: Request) -> EventSourceResponse:
        registry: SessionRegistry = request.app.state.registry
        try:
            session = await registry.get_or_create(req.session_id)
        except Exception as e:
            log.exception("failed to create session")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"session creation failed: {e}",
            ) from e

        async def stream():
            reducer = reducer_factory()
            emitter = UIEmitter()
            token = set_current_emitter(emitter)
            try:
                async with session.lock:
                    async for sdk_event in session.managed.agent.stream_async(req.prompt):
                        # Drain any UI events queued by tools that just ran.
                        # Tools complete before Strands yields the next event,
                        # so this catches everything emitted up to this point.
                        for ui_event in emitter.drain_nowait():
                            yield ui_event
                        for sse_event in reducer.reduce(sdk_event):
                            yield sse_event
                    # Final drain after the stream closes.
                    for ui_event in emitter.drain_nowait():
                        yield ui_event
            except Exception as e:
                log.exception("stream failed for session %s", session.session_id)
                yield sse_error(message=str(e))
            finally:
                reset_current_emitter(token)

        return EventSourceResponse(stream())

    return app
