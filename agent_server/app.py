import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Protocol, runtime_checkable

from fastapi import FastAPI, HTTPException, Request, Response, status
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


@runtime_checkable
class LifespanResource(Protocol):
    """Anything with `start()` and `shutdown()` async methods.

    Used by `create_app` to weave per-process resources (e.g. a
    SandboxPool that needs to warm before the first chat session, or a
    background watcher) into the FastAPI lifespan. Resources are
    started in order before `yield` and shut down in reverse order.
    Started resources are cleaned up even if a later resource's
    `start()` raises, so a half-warmed boot doesn't leak.
    """

    async def start(self) -> None: ...
    async def shutdown(self) -> None: ...


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
    lifespan_resources: Sequence[LifespanResource] | None = None,
) -> FastAPI:
    """Build a FastAPI app that streams the v1 SSE protocol over /v1/chat.

    Args:
        agent_factory: Async callable invoked per-session to build the
            agent. Awaited inside the request handling /v1/chat call.
        reducer_factory: Callable invoked per-request to build a fresh reducer.
        settings: Optional preloaded settings; defaults to `load_base_settings()`.
        title, version: FastAPI metadata.
        lifespan_resources: Optional list of objects with async `start()`
            and `shutdown()`. Started in order before the app accepts
            traffic; shut down in reverse on app exit. Use this for
            things the agent factory will reach into per-request
            (sandbox pool, connection pool, etc) so they're warm by
            the time the first request lands.
    """
    resolved_settings = settings or load_base_settings()
    resources: list[LifespanResource] = list(lifespan_resources or [])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        observability = setup_observability(service_name=title)
        if resolved_settings.service_auth_secret is None:
            log.warning(
                "AGENT_SERVICE_AUTH_SECRET is unset — /v1/chat is unauthenticated. "
                "Acceptable only for local development."
            )
        # Start resources in declared order, tracking which ones
        # succeeded so we can unwind cleanly if a later one fails.
        started: list[LifespanResource] = []
        try:
            for resource in resources:
                await resource.start()
                started.append(resource)
        except Exception:
            for resource in reversed(started):
                try:
                    await resource.shutdown()
                except Exception as e:  # noqa: BLE001
                    log.warning("lifespan resource shutdown raised during failed boot: %s", e)
            observability.close()
            raise

        registry = SessionRegistry(resolved_settings, agent_factory)
        app.state.settings = resolved_settings
        app.state.registry = registry
        app.state.observability = observability
        app.state.lifespan_resources = resources
        try:
            yield
        finally:
            await registry.shutdown()
            for resource in reversed(started):
                try:
                    await resource.shutdown()
                except Exception as e:  # noqa: BLE001
                    log.warning("lifespan resource shutdown raised: %s", e)
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

    @app.delete("/v1/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def end_session(session_id: str, request: Request) -> Response:
        """Explicitly end a session and release its resources.

        Intended for callers (eval runners, batch clients) that know the
        conversation is finished and don't want to wait for the idle TTL
        to release the sandbox + MCP subprocesses. Idempotent — a 204 is
        returned whether or not the session existed.
        """
        registry: SessionRegistry = request.app.state.registry
        await registry.delete(session_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
