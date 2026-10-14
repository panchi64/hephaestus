"""External MCP tool servers (`MCP-1`…`MCP-3`).

Two pieces: :mod:`services.mcp.client` translates a stored registration into one of
Pydantic AI's MCP client toolsets (we never hand-roll the protocol), and
:mod:`services.mcp.registry` owns the operator's side — register, connect + discover,
reconnect, disable, remove — plus the per-tool enable/trust decisions that ride the shared
:mod:`services.external_policy` store.
"""

from .client import DEFAULT_CONNECT_TIMEOUT_S, TRANSPORTS, auth_headers, build_client
from .registry import (
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    STATUS_ERROR,
    McpRegistry,
    McpServerView,
    McpToolView,
)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "STATUS_CONNECTED",
    "STATUS_DISCONNECTED",
    "STATUS_ERROR",
    "TRANSPORTS",
    "McpRegistry",
    "McpServerView",
    "McpToolView",
    "auth_headers",
    "build_client",
]
