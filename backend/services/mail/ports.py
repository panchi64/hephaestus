"""What the mail services need from the rest of the platform, as narrow protocols."""

from __future__ import annotations

from typing import Protocol

from services.registry import ResolvedModel


class BackgroundModelSource(Protocol):
    """The model registry's utility-model lookup."""

    async def resolve_background(self, *, owner_id: str) -> ResolvedModel: ...


class Notifier(Protocol):
    """The attention surface an urgent message raises a notice on."""

    async def notify(
        self, owner_id: str, kind: str, title: str, body: str | None = None
    ) -> object: ...
