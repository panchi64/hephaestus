"""Which folders of the home a conversation may write beyond its own scratch."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session, upsert
from core.serde import as_utc
from models._fields import new_id, utcnow
from models.write_lease import WriteLease

from .paths import HOME, Place


def place_key(place: Place) -> str:
    """A place as a lease names it, its path under the home."""
    return place.path.removeprefix(f"{HOME}/")


class WriteLeases:
    def __init__(self, db_engine: Engine, ttl_s: float) -> None:
        self._db: Engine = db_engine
        self._ttl: timedelta = timedelta(seconds=ttl_s)

    async def lease(self, owner_id: str, conversation_id: str, place: Place) -> None:
        """Let this conversation write ``place`` until the lease lapses; leasing again
        refreshes it."""
        now = utcnow()

        def work(session: Session) -> None:
            stmt = (
                upsert(self._db, WriteLease)
                .values(
                    id=new_id(),
                    owner_id=owner_id,
                    conversation_id=conversation_id,
                    place=place_key(place),
                    created_at=now,
                    expires_at=now + self._ttl,
                )
                .on_conflict_do_update(
                    index_elements=["owner_id", "conversation_id", "place"],
                    set_={"expires_at": now + self._ttl},
                )
            )
            _ = session.exec(stmt)

        await in_session(self._db, work)

    async def held(self, owner_id: str, conversation_id: str) -> set[str]:
        """The places this conversation holds a live lease on, by :func:`place_key`."""
        if not conversation_id:
            return set()
        now = utcnow()

        def work(session: Session) -> set[str]:
            rows = session.exec(
                select(WriteLease)
                .where(WriteLease.owner_id == owner_id)
                .where(WriteLease.conversation_id == conversation_id)
            ).all()
            live = {row.place for row in rows if as_utc(row.expires_at) > now}
            lapsed = [row.id for row in rows if as_utc(row.expires_at) <= now]
            if lapsed:
                _ = session.exec(delete(WriteLease).where(col(WriteLease.id).in_(lapsed)))
            return live

        return await in_session(self._db, work)

    async def release(self, owner_id: str, conversation_id: str, place: Place) -> None:
        def work(session: Session) -> None:
            _ = session.exec(
                delete(WriteLease)
                .where(col(WriteLease.owner_id) == owner_id)
                .where(col(WriteLease.conversation_id) == conversation_id)
                .where(col(WriteLease.place) == place_key(place))
            )

        await in_session(self._db, work)
