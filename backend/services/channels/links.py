"""Which conversation each channel address continues."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session
from models.channel_link import ChannelLink


@dataclass(frozen=True)
class LinkView:
    id: str
    channel: str
    address: str
    conversation_id: str
    created_at: datetime


class ChannelLinks:
    def __init__(self, engine: Engine) -> None:
        self._engine: Engine = engine

    async def conversation(self, owner_id: str, channel: str, address: str) -> str | None:
        def work(session: Session) -> str | None:
            row = _find(session, owner_id, channel, address)
            return None if row is None else row.conversation_id

        return await in_session(self._engine, work)

    async def link(self, owner_id: str, channel: str, address: str, conversation_id: str) -> None:
        def work(session: Session) -> None:
            row = _find(session, owner_id, channel, address) or ChannelLink(
                owner_id=owner_id, channel=channel, address=address, conversation_id=""
            )
            row.conversation_id = conversation_id
            session.add(row)

        await in_session(self._engine, work)

    async def unlink(self, owner_id: str, channel: str, address: str) -> None:
        def work(session: Session) -> None:
            _ = session.exec(
                delete(ChannelLink)
                .where(col(ChannelLink.owner_id) == owner_id)
                .where(col(ChannelLink.channel) == channel)
                .where(col(ChannelLink.address) == address)
            )

        await in_session(self._engine, work)

    async def remove(self, owner_id: str, link_id: str) -> None:
        def work(session: Session) -> None:
            _ = session.exec(
                delete(ChannelLink)
                .where(col(ChannelLink.owner_id) == owner_id)
                .where(col(ChannelLink.id) == link_id)
            )

        await in_session(self._engine, work)

    async def addresses(self, owner_id: str, conversation_id: str) -> list[tuple[str, str]]:
        """Every ``(channel, address)`` continuing ``conversation_id``."""

        def work(session: Session) -> list[tuple[str, str]]:
            rows = session.exec(
                select(ChannelLink)
                .where(ChannelLink.owner_id == owner_id)
                .where(ChannelLink.conversation_id == conversation_id)
            ).all()
            return [(row.channel, row.address) for row in rows]

        return await in_session(self._engine, work)

    async def list(self, owner_id: str) -> list[LinkView]:
        def work(session: Session) -> list[LinkView]:
            rows = session.exec(
                select(ChannelLink)
                .where(ChannelLink.owner_id == owner_id)
                .order_by(col(ChannelLink.created_at))
            ).all()
            return [
                LinkView(row.id, row.channel, row.address, row.conversation_id, row.created_at)
                for row in rows
            ]

        return await in_session(self._engine, work)


def _find(session: Session, owner_id: str, channel: str, address: str) -> ChannelLink | None:
    return session.exec(
        select(ChannelLink)
        .where(ChannelLink.owner_id == owner_id)
        .where(ChannelLink.channel == channel)
        .where(ChannelLink.address == address)
    ).first()
