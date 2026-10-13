"""A real, tiny MCP server over stdio — the fixture the discovery tests dial.

Run as a subprocess by :mod:`tests.test_mcp_discovery` so the registry is exercised
against an actual MCP handshake rather than a mock of one: the point of the slice is that
we speak the protocol through Pydantic AI's client, and only a real server proves it.

Deliberately two tools, so a test can show that trust granted to one leaves the other
approval-gated (`AE-3.6` is per tool, never per server).
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer("hephaestus-test")


@server.tool()
def echo(text: str) -> str:
    """Echo the text straight back."""
    return f"echo:{text}"


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    server.run()
