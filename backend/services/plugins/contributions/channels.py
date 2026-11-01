"""``channels``: a built-in plugin's messaging channel (Discord, …), connected only while the
plugin is switched on."""

from __future__ import annotations

from dataclasses import dataclass
from typing import override

from services.channels import ChannelHub, MessagingChannel

from .base import BUILTIN_ONLY, Described, TypedContribution
from .registry import CONTRIBUTIONS


@dataclass(frozen=True)
class Messaging:
    hub: ChannelHub
    channel: MessagingChannel

    @property
    def name(self) -> str:
        return self.channel.key


class MessagingChannels(TypedContribution[Messaging]):
    @override
    async def start(self, plugin: str, items: tuple[Messaging, ...]) -> None:
        for item in items:
            await item.hub.attach(item.channel)

    @override
    async def stop(self, plugin: str, items: tuple[Messaging, ...]) -> None:
        for item in items:
            await item.hub.detach(item.channel.key)

    @override
    def describe_one(self, item: Messaging) -> Described:
        return {"name": item.channel.label}


CHANNELS = CONTRIBUTIONS.register(
    MessagingChannels("channels", "Messaging channels", Messaging, tiers=BUILTIN_ONLY)
)
