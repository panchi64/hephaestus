"""The Discord messaging channel: a bot that relays DMs and allowlisted channels to the hub."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

import discord

from routes.deps import OPERATOR_ID
from services.channels import ApprovalRequest, ChannelHost, ChannelHub, Inbound
from services.credential_store import CredentialStore

from .approvals import approval_text, approval_view, parse_custom_id
from .chunking import chunk
from .config import DiscordConfig

logger = logging.getLogger(__name__)

KEY = "discord"
TOKEN_SERVICE = "discord_bot"
_TYPING_CAP_S = 600
_QUEUED_REACTION = "⏳"


@dataclass(frozen=True)
class ChannelStatus:
    configured: bool
    connected: bool
    bot_user: str | None
    error: str | None


class _Client(discord.Client):
    def __init__(self, owner: DiscordChannel) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(intents=intents)
        self._owner: DiscordChannel = owner

    async def on_message(self, message: discord.Message) -> None:
        await self._owner.handle_message(message)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        await self._owner.handle_interaction(interaction)


class DiscordChannel:
    key: str = KEY
    label: str = "DISCORD"

    def __init__(
        self, hub: ChannelHub, credentials: CredentialStore, config: DiscordConfig
    ) -> None:
        self._hub: ChannelHub = hub
        self._credentials: CredentialStore = credentials
        self._config: DiscordConfig = config
        self._host: ChannelHost | None = None
        self._client: _Client | None = None
        self._runner: asyncio.Task[None] | None = None
        self._token: str | None = None
        self._error: str | None = None
        self._typing: dict[str, asyncio.Task[None]] = {}
        self._lifecycle: asyncio.Lock = asyncio.Lock()

    # --- lifecycle ----------------------------------------------------------------

    async def start(self, hub: ChannelHost) -> None:
        async with self._lifecycle:
            self._host = hub
            await self._connect()

    async def stop(self) -> None:
        async with self._lifecycle:
            self._host = None
            await self._disconnect()

    async def restart_if_token_changed(self) -> None:
        """Reconnect when the stored token is no longer the one in use; idle if it is gone."""
        # Serialised with start and stop: overlapping restarts would each connect a client,
        # and only the last one would be closed.
        async with self._lifecycle:
            if self._host is None:
                return
            if await self._read_token() != self._token:
                await self._disconnect()
                await self._connect()

    async def _read_token(self) -> str | None:
        return await self._credentials.get_secret(OPERATOR_ID, TOKEN_SERVICE) or None

    async def _connect(self) -> None:
        self._token = await self._read_token()
        self._error = None
        if self._token is None:
            return
        client = _Client(self)
        self._client = client
        self._runner = asyncio.create_task(self._run(client, self._token))

    async def _run(self, client: _Client, token: str) -> None:
        try:
            await client.start(token)
        except discord.LoginFailure:
            self._error = "Discord rejected the bot token."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("discord: client stopped", exc_info=True)
            self._error = str(exc) or type(exc).__name__

    async def _disconnect(self) -> None:
        for task in self._typing.values():
            _ = task.cancel()
        self._typing.clear()
        client, runner = self._client, self._runner
        self._client = self._runner = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        if runner is not None:
            _ = runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner

    def status(self) -> ChannelStatus:
        client = self._client
        connected = client is not None and client.is_ready()
        bot = client.user if client is not None else None
        return ChannelStatus(
            configured=self._token is not None,
            connected=connected,
            bot_user=bot.name if bot is not None else None,
            error=self._error,
        )

    # --- messages in --------------------------------------------------------------

    async def handle_message(self, message: discord.Message) -> None:
        client, host = self._client, self._host
        if client is None or host is None:
            return
        if message.author.bot or (client.user is not None and message.author.id == client.user.id):
            return
        if message.guild is None:
            address = f"dm:{message.author.id}"
        elif str(message.channel.id) in await self._config.allowed_channels():
            address = f"channel:{message.channel.id}"
        else:
            return
        if not message.content.strip():
            return
        result = await host.inbound(KEY, str(message.author.id), address, message.content)
        await self._acknowledge(message, address, result)

    async def _acknowledge(self, message: discord.Message, address: str, result: Inbound) -> None:
        match result.outcome:
            case "refused":
                return
            case "fresh" | "failed":
                if result.reply:
                    _ = await message.reply(result.reply)
            case "queued":
                await message.add_reaction(_QUEUED_REACTION)
            case "started":
                self._show_typing(address, message.channel)

    def _show_typing(self, address: str, target: discord.abc.Messageable) -> None:
        self._stop_typing(address)

        async def work() -> None:
            async with target.typing():
                await asyncio.sleep(_TYPING_CAP_S)

        self._typing[address] = asyncio.create_task(work())

    def _stop_typing(self, address: str) -> None:
        task = self._typing.pop(address, None)
        if task is not None:
            _ = task.cancel()

    # --- messages out -------------------------------------------------------------

    async def _target(self, address: str) -> discord.abc.Messageable:
        client = self._client
        kind, _, raw = address.partition(":")
        if client is None or not raw.isdigit():
            raise ConnectionError("Discord is not connected.")
        found = int(raw)
        if kind == "dm":
            return client.get_user(found) or await client.fetch_user(found)
        channel = client.get_channel(found) or await client.fetch_channel(found)
        if isinstance(channel, discord.abc.Messageable):
            return channel
        raise ConnectionError(f"Discord channel {raw} cannot take messages.")

    async def send(self, address: str, text: str) -> None:
        self._stop_typing(address)
        target = await self._target(address)
        for piece in chunk(text):
            _ = await target.send(piece)

    async def request_approval(self, address: str, request: ApprovalRequest) -> None:
        # The turn now waits on the operator, not the model.
        self._stop_typing(address)
        target = await self._target(address)
        _ = await target.send(approval_text(request), view=approval_view(request))

    async def approval_fallback(self) -> str | None:
        if not await self._config.approvals_to_dm():
            return None
        senders = await self._hub.allowed_senders(KEY)
        return f"dm:{senders[0]}" if len(senders) == 1 else None

    # --- buttons ------------------------------------------------------------------

    async def handle_interaction(self, interaction: discord.Interaction) -> None:
        host = self._host
        if host is None or interaction.type is not discord.InteractionType.component:
            return
        data = interaction.data
        press = parse_custom_id(str(data.get("custom_id", ""))) if data is not None else None
        if press is None:
            return
        _ = await interaction.response.defer(ephemeral=True)
        decided = await host.decide(
            KEY, str(interaction.user.id), press.run_id, press.token, approve=press.approve
        )
        _ = await interaction.followup.send(decided.message, ephemeral=True)
        message = interaction.message
        if decided.settled and message is not None:
            content = f"{message.content[:1900]}\n**{decided.message}**"
            _ = await message.edit(content=content, view=None)
