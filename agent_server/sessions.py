"""Generic in-memory session registry parameterised on an agent factory.

The registry knows nothing about Strands, MCP clients, code interpreters,
or model providers. Each agent's `server/main.py` supplies an
`agent_factory` async callable that builds a `ManagedAgent` (the agent
plus a teardown to release per-session resources). The registry caches
one `ManagedAgent` per `session_id`, evicts on idle TTL, and calls
teardown on eviction or shutdown.

Async contract:
  - The factory is awaitable. This lets agents await per-session
    resource claims (e.g. a sandbox task from a SandboxPool) without
    juggling event-loop bridges from a thread pool.
  - `teardown` may be sync or async. The registry awaits coroutines
    and silently runs sync callables, so existing agents that pass a
    plain `lambda: client.close()` keep working.
"""

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .config import BaseSettings

log = logging.getLogger(__name__)


@dataclass
class ManagedAgent:
    """An agent plus a teardown hook.

    `agent` must expose `stream_async(prompt)` returning an async iterator
    of SDK-native events. `teardown` is called when the session is evicted
    or the app shuts down — use it to stop subprocesses, close clients,
    release sandbox sessions, etc.

    `teardown` may return None (sync cleanup) or an awaitable
    (async cleanup). The registry handles both — sync agents need
    no changes; agents that need to await resource releases (e.g.
    `await pool.release(claimed_task)`) just return a coroutine.
    """

    agent: Any
    teardown: Callable[[], Awaitable[None] | None] = field(default=lambda: None)


AgentFactory = Callable[[str], Awaitable[ManagedAgent]]


@dataclass
class AgentSession:
    session_id: str
    managed: ManagedAgent
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionRegistry:
    def __init__(self, settings: BaseSettings, agent_factory: AgentFactory) -> None:
        self._settings = settings
        self._agent_factory = agent_factory
        self._sessions: dict[str, AgentSession] = {}
        self._registry_lock = asyncio.Lock()

    async def get_or_create(self, session_id: str | None) -> AgentSession:
        sid = session_id or uuid.uuid4().hex
        async with self._registry_lock:
            await self._evict_expired_locked()
            session = self._sessions.get(sid)
            if session is not None:
                session.last_used_at = time.monotonic()
                return session

            if len(self._sessions) >= self._settings.max_sessions:
                await self._evict_oldest_locked()

            managed = await self._agent_factory(sid)
            session = AgentSession(session_id=sid, managed=managed)
            self._sessions[sid] = session
            log.info("created session %s (active=%d)", sid, len(self._sessions))
            return session

    async def delete(self, session_id: str) -> bool:
        """Explicitly end a session, releasing its resources immediately.

        Idle TTL (session_ttl_seconds) is the only other way a session
        terminates outside of app shutdown; for callers that know the
        conversation is finished (eval runners, batch clients, /chat
        wrappers that detect end-of-turn out-of-band) this short-circuits
        the wait and frees the claimed sandbox / MCP subprocesses now.
        Idempotent: returns True if a session was found and torn down,
        False if it was already gone.
        """
        async with self._registry_lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            log.info("explicit close of session %s (active=%d)", session_id, len(self._sessions))
            await self._safe_teardown(session.managed)
            return True

    async def shutdown(self) -> None:
        async with self._registry_lock:
            for sid, session in list(self._sessions.items()):
                await self._safe_teardown(session.managed)
                self._sessions.pop(sid, None)

    async def _evict_expired_locked(self) -> None:
        ttl = self._settings.session_ttl_seconds
        if ttl <= 0:
            return
        now = time.monotonic()
        expired_ids = [
            sid for sid, s in self._sessions.items()
            if now - s.last_used_at > ttl
        ]
        for sid in expired_ids:
            session = self._sessions.pop(sid)
            log.info("evicting expired session %s", sid)
            await self._safe_teardown(session.managed)

    async def _evict_oldest_locked(self) -> None:
        if not self._sessions:
            return
        sid = min(self._sessions, key=lambda k: self._sessions[k].last_used_at)
        session = self._sessions.pop(sid)
        log.info("evicting oldest session %s to stay under max_sessions", sid)
        await self._safe_teardown(session.managed)

    @staticmethod
    async def _safe_teardown(managed: ManagedAgent) -> None:
        try:
            result = managed.teardown()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            log.warning("session teardown raised: %s", e)
