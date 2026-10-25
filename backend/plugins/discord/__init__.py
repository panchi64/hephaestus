"""Discord — talk to the agent from a Discord bot: DMs and allowlisted channels continue
chats, answers and approvals come back there. Off by default because it needs a bot token."""

from __future__ import annotations

import asyncio

from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext, PluginInfo
from services.channels import ChannelHub
from services.credential_store import CredentialStore, ServiceInfo
from services.plugins.contributions.channels import Messaging
from services.settings_store import SettingsStore

from . import routes as discord_routes
from .channel import TOKEN_SERVICE, DiscordChannel
from .config import DiscordConfig

_CREDENTIALS = (
    ServiceInfo(
        TOKEN_SERVICE,
        "Discord (bot token)",
        "The Discord plugin — the token of the bot that relays your messages and answers.",
        "https://discord.com/developers/applications",
    ),
)


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    hub = ctx.services.get(ChannelHub)
    credentials = ctx.services.get(CredentialStore)
    config = DiscordConfig(ctx.services.get(SettingsStore))
    channel = DiscordChannel(hub, credentials, config)
    reconnects: set[asyncio.Task[None]] = set()

    def on_credentials_changed() -> None:
        task = asyncio.get_running_loop().create_task(channel.restart_if_token_changed())
        reconnects.add(task)
        task.add_done_callback(reconnects.discard)

    credentials.on_change(on_credentials_changed)
    return FeatureRuntime(
        services=(channel, config),
        contributions=(Messaging(hub, channel),),
    )


MANIFEST = FeatureManifest(
    name="discord",
    plugin=PluginInfo(
        title="Discord",
        description=(
            "Chat with the agent from Discord: direct messages and chosen channels continue "
            + "a chat, and answers and approvals are sent back there."
        ),
        default_enabled=False,
    ),
    after=("channels",),
    routers=(discord_routes.router,),
    credentials=_CREDENTIALS,
    build=_build,
)
