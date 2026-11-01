"""``alert_channels``: a built-in plugin's out-of-band notification channel (email, push),
fanned out to only while the plugin is switched on."""

from __future__ import annotations

from dataclasses import dataclass
from typing import override

from services.notification_channels import NotificationChannel
from services.notifications import NotificationService

from .base import BUILTIN_ONLY, Described, TypedContribution
from .registry import CONTRIBUTIONS


@dataclass(frozen=True)
class AlertChannel:
    notifications: NotificationService
    channel: NotificationChannel

    @property
    def name(self) -> str:
        return self.channel.key


class AlertChannels(TypedContribution[AlertChannel]):
    @override
    async def start(self, plugin: str, items: tuple[AlertChannel, ...]) -> None:
        for item in items:
            item.notifications.add_channel(item.channel)

    @override
    async def stop(self, plugin: str, items: tuple[AlertChannel, ...]) -> None:
        for item in items:
            item.notifications.remove_channel(item.channel)

    @override
    def describe_one(self, item: AlertChannel) -> Described:
        return {"name": item.channel.label}


ALERT_CHANNELS = CONTRIBUTIONS.register(
    AlertChannels("alert_channels", "Alert channels", AlertChannel, tiers=BUILTIN_ONLY)
)
