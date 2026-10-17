"""The sealed write-through behind every conversation inbox (``runs/inbox.py``).

Writes go through a lock-aware write-behind worker, so a send never waits on the database
and never fails on a locked vault: while the vault is locked the writes park and land the
moment it opens. On boot, :meth:`SealedInboxJournal.load` hands every inbox back to the registry.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, override

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import Engine
from sqlmodel import Session, col, delete, select

from core.db import in_session
from core.vault import Vault
from core.worker import WriteBehindWorker
from models.inbox_item import InboxItem, InboxWaiting
from runs import InboxJournal, QueuedMessage, RunRegistry, TurnPayload
from runs.events import MessageSource

logger = logging.getLogger(__name__)

_SOURCE: TypeAdapter[MessageSource] = TypeAdapter(MessageSource)


class _Payload(BaseModel):
    attachment_ids: list[str] = []
    file_refs: list[str] = []
    command: str | None = None
    endpoint_id: str | None = None
    model: str | None = None
    permission_level: str | None = None
    agent: str | None = None


@dataclass(frozen=True)
class _Job:
    op: Literal["record", "forget", "waiting"]
    owner_id: str
    conversation_id: str
    message: QueuedMessage | None = None
    message_ids: tuple[str, ...] = ()
    waiting: bool = False
    stopped: bool = False


@dataclass(frozen=True)
class LoadedInbox:
    owner_id: str
    conversation_id: str
    messages: list[QueuedMessage]
    waiting: bool
    stopped: bool = False


class SealedInboxJournal(InboxJournal):
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._worker: WriteBehindWorker[_Job] = WriteBehindWorker(
            self._write,
            name="inbox-journal",
            unlocked=vault.unlocked_event,
            on_drop=self._on_drop,
        )

    async def start(self) -> None:
        await self._worker.start()

    async def stop(self) -> None:
        await self._worker.stop()

    async def flush(self) -> None:
        await self._worker.join()

    @override
    def record(self, owner_id: str, conversation_id: str, message: QueuedMessage) -> None:
        self._worker.submit(_Job("record", owner_id, conversation_id, message=message))

    @override
    def forget(self, owner_id: str, conversation_id: str, message_ids: Sequence[str]) -> None:
        self._worker.submit(
            _Job("forget", owner_id, conversation_id, message_ids=tuple(message_ids))
        )

    @override
    def mark_waiting(
        self, owner_id: str, conversation_id: str, waiting: bool, stopped: bool
    ) -> None:
        self._worker.submit(
            _Job("waiting", owner_id, conversation_id, waiting=waiting, stopped=stopped)
        )

    async def load(self) -> list[LoadedInbox]:
        """Every stored inbox. Needs the vault open."""

        def work(session: Session) -> tuple[list[InboxItem], list[InboxWaiting]]:
            items = list(session.exec(select(InboxItem).order_by(col(InboxItem.seq))))
            waiting = list(session.exec(select(InboxWaiting)))
            return items, waiting

        items, waiting = await in_session(self._engine, work)
        found: dict[tuple[str, str], LoadedInbox] = {}
        for row in waiting:
            key = (row.owner_id, row.conversation_id)
            found[key] = LoadedInbox(
                row.owner_id, row.conversation_id, [], waiting=True, stopped=row.stopped
            )
        for row in items:
            key = (row.owner_id, row.conversation_id)
            inbox = found.setdefault(
                key, LoadedInbox(row.owner_id, row.conversation_id, [], waiting=False)
            )
            message = self._message(row)
            if message is not None:
                inbox.messages.append(message)
        return list(found.values())

    async def restore_into(self, registry: RunRegistry) -> list[LoadedInbox]:
        """Put every stored inbox back into ``registry``; returns what was restored."""
        loaded = await self.load()
        for each in loaded:
            registry.inbox(each.conversation_id, each.owner_id).restore(
                each.messages, waiting=each.waiting, stopped=each.stopped, record=False
            )
        return loaded

    def _message(self, row: InboxItem) -> QueuedMessage | None:
        try:
            source = _SOURCE.validate_python(row.source)
        except ValidationError:
            logger.warning("inbox: skipping a stored message with source %r", row.source)
            return None
        payload = None
        if row.payload_enc is not None:
            parsed = _Payload.model_validate_json(self._vault.decrypt_str(row.payload_enc))
            payload = TurnPayload(
                attachment_ids=tuple(parsed.attachment_ids),
                file_refs=tuple(parsed.file_refs),
                command=parsed.command,
                endpoint_id=parsed.endpoint_id,
                model=parsed.model,
                permission_level=parsed.permission_level,
                agent=parsed.agent,
            )
        return QueuedMessage(
            id=row.id,
            text=self._vault.decrypt_str(row.text_enc),
            queued_at=row.queued_at,
            source=source,
            sender=row.sender,
            held=row.held,
            payload=payload,
            notice=row.notice,
            seq=row.seq,
        )

    async def _write(self, job: _Job) -> None:
        if job.op == "record":
            message = job.message
            assert message is not None
            text_enc = self._vault.encrypt_str(message.text)
            payload_enc = (
                None
                if message.payload is None
                else self._vault.encrypt_str(
                    _Payload(
                        attachment_ids=list(message.payload.attachment_ids),
                        file_refs=list(message.payload.file_refs),
                        command=message.payload.command,
                        endpoint_id=message.payload.endpoint_id,
                        model=message.payload.model,
                        permission_level=message.payload.permission_level,
                        agent=message.payload.agent,
                    ).model_dump_json()
                )
            )

            def record(session: Session) -> None:
                _ = session.merge(
                    InboxItem(
                        id=message.id,
                        owner_id=job.owner_id,
                        conversation_id=job.conversation_id,
                        seq=message.seq,
                        source=message.source,
                        sender=message.sender,
                        held=message.held,
                        notice=message.notice,
                        queued_at=message.queued_at,
                        text_enc=text_enc,
                        payload_enc=payload_enc,
                    )
                )

            await in_session(self._engine, record)
        elif job.op == "forget":

            def forget(session: Session) -> None:
                _ = session.exec(delete(InboxItem).where(col(InboxItem.id).in_(job.message_ids)))

            await in_session(self._engine, forget)
        else:

            def waiting(session: Session) -> None:
                row = session.get(InboxWaiting, job.conversation_id)
                if job.waiting and row is None:
                    session.add(
                        InboxWaiting(
                            conversation_id=job.conversation_id,
                            owner_id=job.owner_id,
                            stopped=job.stopped,
                        )
                    )
                elif job.waiting and row is not None:
                    row.stopped = job.stopped
                    session.add(row)
                elif not job.waiting and row is not None:
                    session.delete(row)

            await in_session(self._engine, waiting)

    @staticmethod
    def _on_drop(job: _Job, exc: Exception) -> None:
        logger.error(
            "inbox: could not write a %s for conversation %s: %s",
            job.op,
            job.conversation_id,
            exc,
        )
