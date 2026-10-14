"""Embedding backfill and reindex over persisted messages."""

from __future__ import annotations

from sqlalchemy import or_
from sqlmodel import Session, col, select

from core.db import in_session
from models.conversation import Conversation, Message
from services.embeddings import embed_and_seal_rows

from .core import _StoreCore


class _EmbeddingMixin(_StoreCore):
    async def backfill_embeddings(self, owner_id: str, *, batch_size: int = 64) -> int:
        """Lift messages with *no* vector into the index — the startup backlog case
        (persisted before an embedding endpoint existed). The NULL-only slice of
        :meth:`reindex_embeddings`."""
        return await self.reindex_embeddings(owner_id, current_model=None, batch_size=batch_size)

    async def reindex_embeddings(
        self, owner_id: str, *, current_model: str | None = None, batch_size: int = 64
    ) -> int:
        """Best-effort: (re-)embed content-bearing messages whose vector is missing,
        or — when ``current_model`` is given — was produced by a different model than
        the one now configured. The latter is the heal path after the operator changes
        the embedding model: EMB-2 segregates vectors by model, so stale vectors fall
        back to keyword search until re-embedded into the current space. Returns how
        many were embedded. A no-op when the embedder is unavailable, so a backlog
        stays keyword-searchable until an embedder exists, then this lifts it."""
        if self._embedder is None:
            return 0

        def pending(session: Session) -> list[tuple[str, str]]:
            query = (
                select(Message.id, Message.text)
                .join(Conversation, col(Message.conversation_id) == Conversation.id)
                .where(Conversation.owner_id == owner_id)
            )
            if current_model is None:
                query = query.where(col(Message.embedding_enc).is_(None))
            else:
                query = query.where(
                    or_(
                        col(Message.embedding_enc).is_(None),
                        col(Message.embedding_model) != current_model,
                    )
                )
            rows = session.exec(query).all()
            return [(rid, self._vault.decrypt_str(text)) for rid, text in rows]

        rows = await in_session(self._engine, pending)
        items = [(rid, text) for rid, text in rows if text.strip()]
        assert self._embedder is not None  # guarded at the top of this method
        return await embed_and_seal_rows(
            engine=self._engine,
            vault=self._vault,
            embedder=self._embedder,
            owner_id=owner_id,
            model_cls=Message,
            pending=items,
            batch_size=batch_size,
        )
