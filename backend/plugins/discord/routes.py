"""Discord settings: connection state, and who and where may talk to the agent.

The bot token is set through the declared ``discord_bot`` service key (``PUT
/credentials/discord_bot``); the channel reconnects itself when it changes.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, field_validator

from routes import deps
from services.channels import ChannelHub

from .channel import KEY, DiscordChannel
from .config import DiscordConfig, is_snowflake

router = APIRouter(prefix="/discord", tags=["discord"])


class DiscordStatus(BaseModel):
    configured: bool
    connected: bool
    bot_user: str | None
    error: str | None
    allowed_users: list[str]
    allowed_channels: list[str]
    approvals_to_dm: bool


class DiscordConfigIn(BaseModel):
    allowed_users: list[str]
    allowed_channels: list[str]
    approvals_to_dm: bool

    @field_validator("allowed_users", "allowed_channels")
    @classmethod
    def _ids(cls, ids: list[str]) -> list[str]:
        cleaned = [i.strip() for i in ids if i.strip()]
        if not all(is_snowflake(i) for i in cleaned):
            raise ValueError("Discord ids are numbers")
        return cleaned


async def _status(request: Request) -> DiscordStatus:
    channel = deps.service(request, DiscordChannel)
    config = deps.service(request, DiscordConfig)
    state = channel.status()
    return DiscordStatus(
        configured=state.configured,
        connected=state.connected,
        bot_user=state.bot_user,
        error=state.error,
        allowed_users=await deps.service(request, ChannelHub).allowed_senders(KEY),
        allowed_channels=await config.allowed_channels(),
        approvals_to_dm=await config.approvals_to_dm(),
    )


@router.get("/status", response_model=DiscordStatus)
async def status(request: Request) -> DiscordStatus:
    return await _status(request)


@router.put("/config", response_model=DiscordStatus)
async def set_config(body: DiscordConfigIn, request: Request) -> DiscordStatus:
    config = deps.service(request, DiscordConfig)
    await deps.service(request, ChannelHub).set_allowed_senders(KEY, body.allowed_users)
    await config.set_allowed_channels(body.allowed_channels)
    await config.set_approvals_to_dm(body.approvals_to_dm)
    return await _status(request)
