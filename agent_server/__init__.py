"""Shared FastAPI scaffolding for hosting an agent over HTTP+SSE.

Each agent provides a small `server/main.py` that:
1. Defines an `AgentFactory` (a callable returning a `ManagedAgent`).
2. Picks a streaming reducer (e.g. `StrandsEventReducer` for Strands agents,
   or its own for other SDKs).
3. Calls `create_app(...)` and binds the result to a module-level `app`
   variable that uvicorn loads.

The contract emitted by `/v1/chat` is the v1 SSE event protocol defined in
`agent_server.events`.
"""

from .app import create_app
from .config import BaseSettings, load_base_settings
from .display_tools import (
    make_display_dataframe_tool,
    make_display_image_tool,
    make_display_plotly_tool,
)
from .observability import Observability, setup as setup_observability
from .sessions import AgentFactory, ManagedAgent
from .streaming import EventReducer, ReducerFactory, StrandsEventReducer
from .ui_emitter import UIEmitter, get_current_emitter

__all__ = [
    "AgentFactory",
    "BaseSettings",
    "EventReducer",
    "ManagedAgent",
    "Observability",
    "ReducerFactory",
    "StrandsEventReducer",
    "UIEmitter",
    "create_app",
    "get_current_emitter",
    "load_base_settings",
    "make_display_dataframe_tool",
    "make_display_image_tool",
    "make_display_plotly_tool",
    "setup_observability",
]
