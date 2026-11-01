"""A live preview: a dev server the agent runs in its conversation's machine.

The server listens in the machine; a relay there exposes it on the machine's network
address, which smol publishes on a host loopback port. The backend reverse-proxies that
port out to a sandboxed iframe; only this host can reach it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreviewHandle:
    """A running preview: the unguessable token that addresses it, the loopback host port
    the proxy forwards to, and the port the server listens on in its machine."""

    token: str
    host_port: int
    server_port: int
    command: tuple[str, ...]

    @property
    def path(self) -> str:
        """The token-gated route the operator's iframe points at."""
        return f"/previews/{self.token}/"

    def url_for(self, entry: str | None = None) -> str:
        """The proxy URL for a server ``entry`` path under this preview (e.g.
        ``"index.html"``), or the root when omitted."""
        return self.path + (entry or "").lstrip("/")
