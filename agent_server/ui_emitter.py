"""Per-request UI event channel.

Tools that want to surface inline content (dataframes, charts, images) call
into a `UIEmitter` to push pre-formatted SSE events. The FastAPI request
handler creates one emitter per `/v1/chat` call, exposes it via a
ContextVar, and drains its queue alongside the agent's own SDK event
stream.

ContextVar gives us per-request scoping that propagates correctly across
`asyncio.to_thread` and Strands' tool execution model — no need to thread
the emitter through agent / tool constructor signatures.
"""

import asyncio
from contextvars import ContextVar


class UIEmitter:
    """A bounded queue of pre-formatted SSE event dicts."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()

    def emit(self, sse_event: dict[str, str]) -> None:
        self._queue.put_nowait(sse_event)

    def drain_nowait(self) -> list[dict[str, str]]:
        events: list[dict[str, str]] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return events


_current: ContextVar[UIEmitter | None] = ContextVar("ui_emitter", default=None)


def get_current_emitter() -> UIEmitter | None:
    """Return the emitter for the in-flight request, or None outside one."""
    return _current.get()


def set_current_emitter(emitter: UIEmitter | None):
    """Set the per-request emitter; returns a token suitable for `reset`."""
    return _current.set(emitter)


def reset_current_emitter(token) -> None:
    _current.reset(token)
