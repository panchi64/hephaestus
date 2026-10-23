"""The write-behind drainer's persistence: how each queued job lands in the database."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import override

from sqlalchemy import delete
from sqlmodel import Session, col

from core.db import in_session
from core.exceptions import DegradedCapabilityError
from models.conversation import Conversation, Message
from services.embeddings import encode_vector

from .core import StoreCore
from .rows import PersistJob, defer_fk

logger = logging.getLogger(__name__)


class DrainerMixin(StoreCore):
    _save_failed_listener: Callable[[str], None] | None = None

    def set_save_failed_listener(self, listener: Callable[[str], None] | None) -> None:
        """Replace the callback given the conversation id when a write is dropped for good.
        It runs on the drainer's task, so it must not block."""
        self._save_failed_listener = listener

    @override
    async def _persist(self, job: PersistJob) -> None:
        if job.kind == "messages":
            await self._persist_messages(job)
        elif job.kind == "active_leaf":
            await self._persist_active_leaf(job)
        elif job.kind == "delete":
            await self._persist_delete(job)
        elif job.kind == "pin":
            await self._persist_pin(job)
        elif job.kind == "unblock":
            await self._persist_unblock(job)
        elif job.kind == "block":
            await self._persist_block(job)
        # Deliberately not in a `finally`: a raise here is retried by the worker, and
        # the conversation must stay pinned across those attempts. A job that exhausts
        # its retries releases through `_on_drop` instead.
        self._release(job)

    async def _persist_messages(self, job: PersistJob) -> None:
        # Embed off the DB thread, before the write; a missing embedder yields no vectors.
        vectors = await self._embed_rows(job)

        def work(session: Session) -> None:
            # Deleted while queued: do not resurrect it as orphaned message rows.
            conversation = session.get(Conversation, job.conversation_id)
            if conversation is None:
                return
            for row in job.rows:
                model, dim, vector_enc = vectors.get(row.id, (None, None, None))
                session.add(
                    Message(
                        id=row.id,
                        conversation_id=job.conversation_id,
                        parent_id=row.parent_id,
                        seq=row.seq,
                        kind=row.kind,
                        text=self._vault.encrypt_str(row.text),
                        blob=self._vault.encrypt_str(row.blob),
                        attachment_ids=row.attachment_ids,
                        blocked_reason=row.blocked_reason,
                        compacted=row.compacted,
                        compacted_through=row.compacted_through,
                        llm_ms=row.llm_ms,
                        ttft_ms=row.ttft_ms,
                        tool_ms=row.tool_ms,
                        embedding_enc=vector_enc,
                        embedding_model=model,
                        embedding_dim=dim,
                    )
                )
            conversation.active_leaf_id = job.active_leaf_id
            conversation.model = job.model
            conversation.updated_at = datetime.now(UTC)

        await in_session(self._engine, work)

    async def _embed_rows(
        self, job: PersistJob
    ) -> dict[str, tuple[str | None, int | None, str | None]]:
        """Embed each row's text for cross-chat recall as ``row_id -> (model, dim,
        encrypted_vector)``; empty when there is nothing to embed or no embedder."""
        if self._embedder is None:
            return {}
        texts = [(row.id, row.text) for row in job.rows if row.text.strip()]
        if not texts:
            return {}

        owner_id = await self.owner_of(job.conversation_id)
        if owner_id is None:
            return {}
        # Best-effort: a raise here would burn the drainer's retry budget and could drop
        # the turn, which keyword recall covers without a vector.
        try:
            batch = await self._embedder.embed(owner_id, [text for _id, text in texts])
        except DegradedCapabilityError:
            return {}
        except Exception:
            logger.exception(
                "embedding messages for conversation %s failed; persisting without vectors",
                job.conversation_id,
            )
            return {}
        return {
            row_id: (batch.model, batch.dim, encode_vector(self._vault, vector))
            for (row_id, _text), vector in zip(texts, batch.vectors, strict=False)
        }

    async def _persist_active_leaf(self, job: PersistJob) -> None:
        def work(session: Session) -> None:
            conversation = session.get(Conversation, job.conversation_id)
            if conversation is not None:
                conversation.active_leaf_id = job.active_leaf_id
                conversation.model = job.model
                conversation.updated_at = datetime.now(UTC)

        await in_session(self._engine, work)

    async def _persist_delete(self, job: PersistJob) -> None:
        def work(session: Session) -> None:
            conversation = session.get(Conversation, job.conversation_id)
            if conversation is None:
                return
            if job.deleted_ids:
                defer_fk(session)  # remove the subtree in one statement, FK-safe
                _ = session.exec(delete(Message).where(col(Message.id).in_(job.deleted_ids)))
            conversation.active_leaf_id = job.active_leaf_id
            conversation.model = job.model
            conversation.updated_at = datetime.now(UTC)

        await in_session(self._engine, work)

    async def _persist_pin(self, job: PersistJob) -> None:
        def work(session: Session) -> None:
            # No updated_at bump: a pin is a bookmark, not activity.
            row = session.get(Message, job.message_id) if job.message_id else None
            if row is not None:
                row.pinned = job.pinned

        await in_session(self._engine, work)

    async def _persist_unblock(self, job: PersistJob) -> None:
        def work(session: Session) -> None:
            # No updated_at bump: retiring a stop marker is bookkeeping, and the resuming
            # turn floats the conversation itself.
            row = session.get(Message, job.message_id) if job.message_id else None
            if row is not None:
                row.blocked_reason = None

        await in_session(self._engine, work)

    async def _persist_block(self, job: PersistJob) -> None:
        def work(session: Session) -> None:
            # FIFO behind the prompt's insert, so the row exists and that insert moved
            # `updated_at`.
            row = session.get(Message, job.message_id) if job.message_id else None
            if row is not None:
                row.blocked_reason = job.blocked_reason

        await in_session(self._engine, work)

    @override
    def _on_drop(self, job: PersistJob, exc: Exception) -> None:
        logger.error(
            "permanently failed to persist %s for conversation %s (%d rows, %d deletes): %s",
            job.kind,
            job.conversation_id,
            len(job.rows),
            len(job.deleted_ids),
            exc,
        )
        # Terminal: release the pin or the conversation stays resident forever.
        self._release(job)
        if self._save_failed_listener is not None:
            try:
                self._save_failed_listener(job.conversation_id)
            except Exception:
                logger.exception("save-failed listener raised for %s", job.conversation_id)
