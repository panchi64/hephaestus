"""What a messaging channel is: somewhere the operator talks to the agent from outside the app.

A channel only speaks its platform. The :class:`~services.channels.hub.ChannelHub` owns
everything shared: who may write, which conversation an address continues, ``/new``,
relaying each turn's answer back, and settling approvals. So a new platform (Slack,
Telegram, Matrix) is one module that implements this protocol and calls the hub.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class PendingCall:
    tool_call_id: str
    tool: str
    summary: str
    detail: str | None


@dataclass(frozen=True)
class ApprovalRequest:
    """A parked run's undecided calls, as a channel shows them."""

    run_id: str
    conversation_id: str
    calls: tuple[PendingCall, ...]
    #: Names exactly these pending calls, so a button pressed after they were decided, or
    #: after the run parked again on others, is recognised as stale.
    token: str
    #: Questions the agent asked alongside; a channel can't answer them, only point to the app.
    questions: int


@dataclass(frozen=True)
class Inbound:
    outcome: Literal["refused", "fresh", "started", "queued", "failed"]
    #: What to tell the sender now, if anything.
    reply: str | None = None
    conversation_id: str | None = None


class ChannelHost(Protocol):
    """What a channel calls back into: the hub."""

    async def inbound(self, channel: str, sender: str, address: str, text: str) -> Inbound:
        """A message the operator sent from ``address``: start or continue its turn."""
        ...

    async def decide(
        self, channel: str, sender: str, run_id: str, token: str, *, approve: bool
    ) -> str:
        """Settle everything a run is parked on; returns what to tell the sender."""
        ...


class MessagingChannel(Protocol):
    #: Stable id, stored with each link, e.g. ``discord``.
    key: str
    label: str

    async def start(self, hub: ChannelHost) -> None:
        """Connect, and call the hub's ``inbound`` and ``decide`` as messages arrive."""
        ...

    async def stop(self) -> None: ...

    async def send(self, address: str, text: str) -> None:
        """Post ``text``, split however the platform needs."""
        ...

    async def request_approval(self, address: str, request: ApprovalRequest) -> None:
        """Show the pending calls with a way to approve or deny them all."""
        ...

    async def approval_fallback(self) -> str | None:
        """Where approvals for threads linked nowhere go, if the operator asked for that."""
        ...
