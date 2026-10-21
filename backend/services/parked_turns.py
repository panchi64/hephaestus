"""The sealed write-through behind every parked run (``runs/parked.py``).

A run waiting on the operator is one row, sealed with the vault because its continuation is
the operator's conversation. Writes go through a lock-aware write-behind worker, so parking
never waits on the database and never fails on a locked vault: while it is locked the
writes wait and land when it opens. After unlock, :meth:`SealedParkedStore.load` hands every
stored park back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import Engine
from sqlmodel import Session, col, delete, select

from core.db import in_session
from core.vault import Vault
from core.worker import WriteBehindWorker
from models.parked_turn import ParkedTurnRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredPark:
    """One parked run as stored: its identity, and its continuation as plain JSON text, or
    ``None`` when the sealed payload could not be opened."""

    run_id: str
    owner_id: str
    conversation_id: str
    kind: str
    prompt_message_id: str | None
    last_event_id: int
    payload: str | None
    parked_at: datetime | None = None


@dataclass(frozen=True)
class _Job:
    op: Literal["record", "forget"]
    run_id: str
    park: StoredPark | None = None


class SealedParkedStore:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._worker: WriteBehindWorker[_Job] = WriteBehindWorker(
            self._write,
            name="parked-turns",
            unlocked=vault.unlocked_event,
            on_drop=self._on_drop,
        )

    async def start(self) -> None:
        await self._worker.start()

    async def stop(self) -> None:
        await self._worker.stop()

    async def flush(self) -> None:
        await self._worker.join()

    def record(self, park: StoredPark) -> None:
        self._worker.submit(_Job("record", park.run_id, park))

    def forget(self, run_id: str) -> None:
        self._worker.submit(_Job("forget", run_id))

    async def load(self) -> list[StoredPark]:
        """Every stored park, oldest first; one whose payload cannot be opened has none.
        Needs the vault open."""

        def work(session: Session) -> list[ParkedTurnRecord]:
            query = select(ParkedTurnRecord).order_by(col(ParkedTurnRecord.parked_at))
            return list(session.exec(query))

        parks: list[StoredPark] = []
        for row in await in_session(self._engine, work):
            try:
                payload = self._vault.decrypt_str(row.payload_enc)
            except Exception:
                logger.exception("parked turns: run %s cannot be decrypted", row.run_id)
                payload = None
            parks.append(
                StoredPark(
                    run_id=row.run_id,
                    owner_id=row.owner_id,
                    conversation_id=row.conversation_id,
                    kind=row.kind,
                    prompt_message_id=row.prompt_message_id,
                    last_event_id=row.last_event_id,
                    payload=payload,
                    parked_at=row.parked_at,
                )
            )
        return parks

    async def _write(self, job: _Job) -> None:
        if job.op == "record":
            park = job.park
            assert park is not None
            assert park.payload is not None
            payload_enc = self._vault.encrypt_str(park.payload)

            def record(session: Session) -> None:
                _ = session.merge(
                    ParkedTurnRecord(
                        run_id=park.run_id,
                        owner_id=park.owner_id,
                        conversation_id=park.conversation_id,
                        kind=park.kind,
                        prompt_message_id=park.prompt_message_id,
                        last_event_id=park.last_event_id,
                        payload_enc=payload_enc,
                    )
                )

            await in_session(self._engine, record)
        else:

            def forget(session: Session) -> None:
                _ = session.exec(
                    delete(ParkedTurnRecord).where(col(ParkedTurnRecord.run_id) == job.run_id)
                )

            await in_session(self._engine, forget)

    @staticmethod
    def _on_drop(job: _Job, exc: Exception) -> None:
        logger.error("parked turns: could not %s run %s: %s", job.op, job.run_id, exc)
