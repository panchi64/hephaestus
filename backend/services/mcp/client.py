"""Building a live MCP client from a registered server row.

**We do not hand-roll an MCP client.** Pydantic AI ships ``MCPToolset``, which speaks the
full protocol over any transport and *is* an ``AbstractToolset`` — so a connected server
drops straight into the same stack every built-in category rides. This module only
translates a stored row into the right transport; the library does the rest.

Two conventions the rest of the track depends on:

- **The toolset comes back un-prefixed.** Discovery wants the tool names the server
  actually publishes, and those names are what the per-tool policy rows are keyed by. The
  agent-facing prefix (``{slug}_``) is applied one layer up, where the tools are composed.
- **Credentials become transport auth, not tool arguments.** For the HTTP transports they
  are turned into request headers here. A stdio server has no headers: its auth channel is
  the sealed ``env`` map the operator supplies, so credentials are not applied to it.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping

from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from pydantic_ai.mcp import MCPToolset

from core.exceptions import DegradedCapabilityError
from models.external_tool import McpServer
from services.shapes import parse_json

# The transports a server can be registered with. "http" is MCP's Streamable HTTP.
TRANSPORTS = ("stdio", "sse", "http")

# How long a connect attempt waits before it is called a failure. Short on purpose: the
# operator is watching a Connect button, and a dead endpoint should say so quickly.
DEFAULT_CONNECT_TIMEOUT_S = 10.0


def auth_headers(credentials: Mapping[str, object] | None) -> dict[str, str]:
    """Turn stored credentials into the headers an HTTP-transport server is dialled with.

    ``bearer`` and ``basic`` use the standard ``Authorization`` forms; ``api_key`` uses
    ``X-API-Key``, the de-facto header for key-style MCP auth. Anything missing yields no
    header rather than a malformed one — a server that needs auth then answers 401, which
    is a far better operator signal than a header carrying ``None``.
    """
    if not credentials:
        return {}
    method = credentials.get("method")
    token = credentials.get("token")
    if method == "bearer" and token:
        return {"Authorization": f"Bearer {token}"}
    if method == "api_key" and token:
        return {"X-API-Key": str(token)}
    if method == "basic":
        username = credentials.get("username") or ""
        password = credentials.get("password") or ""
        if username or password:
            raw = f"{username}:{password}".encode()
            return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}
    return {}


def parse_args(args_json: str) -> list[str]:
    """The stored argv, defensively — a corrupt column yields no arguments rather than
    taking the whole surface down."""
    try:
        parsed = parse_json(args_json)
    except TypeError, ValueError:
        return []
    return [str(a) for a in parsed] if isinstance(parsed, list) else []


def build_client(
    row: McpServer,
    *,
    env: dict[str, str] | None = None,
    credentials: Mapping[str, object] | None = None,
    timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
) -> MCPToolset[object]:
    """The Pydantic AI MCP toolset for one registered server.

    ``env``/``credentials`` are passed already-decrypted by the caller, so this stays a
    pure translation and never touches the vault.
    """
    if row.transport == "stdio":
        if not row.command:
            raise DegradedCapabilityError(
                f"MCP server {row.name!r} is registered as stdio but has no command"
            )
        transport = StdioTransport(row.command, parse_args(row.args_json), env=env or None)
    elif row.transport in ("sse", "http"):
        if not row.url:
            raise DegradedCapabilityError(
                f"MCP server {row.name!r} is registered as {row.transport} but has no URL"
            )
        headers = auth_headers(credentials) or None
        transport = (
            SSETransport(row.url, headers=headers)
            if row.transport == "sse"
            else StreamableHttpTransport(row.url, headers=headers)
        )
    else:
        raise DegradedCapabilityError(
            f"MCP server {row.name!r} has an unknown transport {row.transport!r}"
        )
    return MCPToolset(transport, id=row.slug, init_timeout=timeout_s)
