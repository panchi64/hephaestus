"""A live preview: a dev server the agent runs in its workspace, reachable from the host.

The server runs in the platform VM, confined like any other command, and binds a port on
its own private loopback. The VM's relay reaches it through the workspace's preview
socket, and a host loopback port forwards to that (:class:`services.vm.PreviewPort`).
The backend reverse-proxies that port out to a sandboxed iframe; only this host can
reach it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.vm.host import PreviewForward


@dataclass(frozen=True)
class PreviewHandle:
    """A running preview: the unguessable token that addresses it, the loopback host port
    the proxy forwards to, and the port the server listens on in its workspace."""

    token: str
    host_port: int
    server_port: int
    command: tuple[str, ...]
    forward: PreviewForward | None = field(default=None, compare=False, repr=False)

    @property
    def path(self) -> str:
        """The token-gated route the operator's iframe points at."""
        return f"/previews/{self.token}/"

    def url_for(self, entry: str | None = None) -> str:
        """The proxy URL for a server ``entry`` path under this preview (e.g.
        ``"index.html"``), or the root when omitted. Owns the path-join so the
        ``/previews/{token}/`` scheme has one home, not a caller doing string math."""
        return self.path + (entry or "").lstrip("/")
