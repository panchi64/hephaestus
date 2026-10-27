"""Approval requests out to messaging channels, riding the notification fan-out.

A parked run already raises an ``approval_needed`` notification for every out-of-band
channel; this one turns it into the pending calls with buttons, on the channels that
continue the run's conversation.
"""

from __future__ import annotations

from services.notification_channels import ChannelHealth
from services.notification_view import NotificationView

from .hub import ChannelHub

APPROVAL_NEEDED = "approval_needed"


class ChannelApprovals:
    key: str = "messaging"
    label: str = "MESSAGING CHANNELS"

    def __init__(self, hub: ChannelHub) -> None:
        self._hub: ChannelHub = hub

    async def health(self, owner_id: str) -> ChannelHealth:
        del owner_id
        links = await self._hub.links.list(self._hub.owner_id)
        return ChannelHealth(
            key=self.key,
            label=self.label,
            configured=bool(links),
            status="nominal" if links else "off",
            detail=f"{len(links)} linked" if links else "no linked chats",
        )

    async def deliver(self, view: NotificationView) -> None:
        if view.kind != APPROVAL_NEEDED or view.run_id is None or view.conversation_id is None:
            return
        await self._hub.request_approvals(view.run_id, view.conversation_id)

    async def close(self) -> None:
        return None
