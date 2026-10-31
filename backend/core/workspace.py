"""What the routes and the auth gate see of the workspace session (`harness.workspace`)."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from core.exceptions import HephaestusError


class WorkspaceOpenError(HephaestusError):
    """The workspace did not open; the message is the reason shown on the unlock screen."""


class WorkspaceStatus(StrEnum):
    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    FAILED = "failed"


class Workspace(Protocol):
    @property
    def status(self) -> WorkspaceStatus: ...

    @property
    def failure(self) -> str | None:
        """Why the last open failed; None after a successful open."""
        ...

    @property
    def is_open(self) -> bool: ...

    async def open(self) -> None:
        """Mount, decrypt and wire the workspace; raises `WorkspaceOpenError`."""
        ...

    async def lock(self) -> None:
        """Close the workspace and zero the key."""
        ...
