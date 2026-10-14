"""Cross-chat search — find and read the operator's *other* conversations.

Recall is hybrid: BM25 over each message's text and cosine over its embedding, fused with
Reciprocal Rank Fusion, then collapsed to one hit per conversation (its best message is the
snippet). Ranking runs on the memory-only :mod:`services.recall_index`, so a query decrypts
only the messages it returns. A message with no comparable vector contributes through the
keyword signal alone (`EMB-2`).

``read`` reuses the conversation store's own active-path projection so there is no
second copy of the tree/active-leaf logic here.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from datetime import datetime
from typing import override

from sqlalchemy import ColumnElement, Engine, func
from sqlmodel import Session, col, select

from core.db import in_session
from core.vault import Vault
from models.conversation import Conversation, Message
from services import ranking
from services.conversations import ConversationStore
from services.embeddings import Embedder, decode_vector, embed_query
from services.recall_index import Collection, Doc, Loaded, Query, RecallIndex
from services.sealing import open_sealed

_SNIPPET_LEN = 240
#: Messages ranked per conversation asked for, before collapsing to one hit each.
_FANOUT = 8


class MessageCollection(Collection[str]):
    """Searchable messages; ``meta`` is the conversation id. A compaction checkpoint is a
    summary of turns still in the table, so it is never indexed."""

    name: str = "messages"

    @staticmethod
    def _owned(owner_id: str) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
        return (
            col(Conversation.owner_id) == owner_id,
            col(Message.compacted).is_(False),
        )

    @override
    def signature(self, session: Session, owner_id: str) -> Hashable:
        stmt = (
            select(Message.embedding_model, func.count(), func.max(Message.created_at))
            .join(Conversation, col(Message.conversation_id) == col(Conversation.id))
            .where(*self._owned(owner_id))
            .group_by(col(Message.embedding_model))
        )
        return frozenset(session.exec(stmt).all())

    @override
    def stamps(self, session: Session, owner_id: str) -> dict[str, Hashable]:
        stmt = (
            select(Message.id, Message.embedding_model)
            .join(Conversation, col(Message.conversation_id) == col(Conversation.id))
            .where(*self._owned(owner_id))
        )
        return {mid: model for mid, model in session.exec(stmt).all()}

    @override
    def load(
        self, session: Session, vault: Vault, owner_id: str, ids: list[str]
    ) -> list[Loaded[str]]:
        rows = session.exec(select(Message).where(col(Message.id).in_(ids))).all()
        loaded: list[Loaded[str]] = []
        for row in rows:
            text = vault.decrypt_str(row.text)
            if not text.strip():
                continue
            vector = (
                decode_vector(vault, row.embedding_enc) if row.embedding_enc is not None else None
            )
            loaded.append(
                Loaded(
                    Doc(row.id, row.created_at, row.conversation_id),
                    text,
                    vector,
                    row.embedding_model,
                )
            )
        return loaded


@dataclass(frozen=True)
class ChatSearchHit:
    """One conversation surfaced by a search, with its best-matching excerpt."""

    conversation_id: str
    title: str | None
    snippet: str
    score: float
    matched_by: str  # semantic | keyword | both
    updated_at: datetime


@dataclass(frozen=True)
class ChatTranscript:
    """A found conversation's active-path turns, rendered for the agent to read."""

    conversation_id: str
    title: str | None
    text: str


class ConversationSearch:
    def __init__(
        self, engine: Engine, vault: Vault, embedder: Embedder, store: ConversationStore
    ) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._embedder: Embedder = embedder
        self._store: ConversationStore = store
        self._index: RecallIndex = RecallIndex(engine, vault)
        self._collection: MessageCollection = MessageCollection()

    @property
    def embedder(self) -> Embedder:
        """The embedder for search queries; assignable when the endpoint changes."""
        return self._embedder

    @embedder.setter
    def embedder(self, value: Embedder) -> None:
        self._embedder = value

    async def search(
        self,
        owner_id: str,
        query: str,
        *,
        limit: int = 5,
        exclude_conversation_id: str | None = None,
        since: datetime | None = None,
    ) -> list[ChatSearchHit]:
        """Hybrid recall across the owner's conversations, best hit per conversation.

        ``exclude_conversation_id`` drops the current thread (the agent already has
        it in context). Ephemeral threads are never searched. A degraded embedder
        collapses cleanly to keyword-only."""
        query_vec, query_model = await embed_query(self._embedder, owner_id, query)
        query_tokens = ranking.tokens(query)
        searchable = await self._searchable(owner_id, exclude_conversation_id, since)
        ranked = await self._index.search(
            self._collection,
            owner_id,
            Query(query_tokens, query_vec, query_model),
            limit=limit * _FANOUT,
            keep=lambda doc: doc.meta in searchable,
        )
        best: dict[str, tuple[str, float, str]] = {}
        for hit in ranked:
            if hit.doc.meta not in best:
                best[hit.doc.meta] = (hit.doc.id, hit.score, hit.matched_by)
        chosen = list(best.items())[:limit]

        def work(session: Session) -> list[ChatSearchHit]:
            hits: list[ChatSearchHit] = []
            for conversation_id, (message_id, score, matched_by) in chosen:
                message = session.get(Message, message_id)
                conversation = session.get(Conversation, conversation_id)
                if message is None or conversation is None:
                    continue
                hits.append(
                    ChatSearchHit(
                        conversation_id=conversation_id,
                        title=open_sealed(self._vault, conversation.title_enc, conversation.title),
                        snippet=snippet(self._vault.decrypt_str(message.text), query_tokens),
                        score=score,
                        matched_by=matched_by,
                        updated_at=conversation.updated_at,
                    )
                )
            return hits

        return await in_session(self._engine, work)

    async def _searchable(
        self, owner_id: str, exclude: str | None, since: datetime | None
    ) -> frozenset[str]:
        """The owner's conversations a search may surface: not ephemeral, not the
        caller's own thread, and touched since ``since``."""

        def work(session: Session) -> frozenset[str]:
            stmt = (
                select(Conversation.id)
                .where(Conversation.owner_id == owner_id)
                .where(col(Conversation.ephemeral).is_(False))
            )
            if exclude is not None:
                stmt = stmt.where(Conversation.id != exclude)
            if since is not None:
                stmt = stmt.where(Conversation.updated_at >= since)
            return frozenset(session.exec(stmt).all())

        return await in_session(self._engine, work)

    async def read(self, owner_id: str, conversation_id: str) -> ChatTranscript | None:
        """The active-path transcript of one conversation, or None if it isn't the
        owner's. Reuses the store's projection (no duplicate tree logic)."""
        summary = await self._store.get_summary(conversation_id, owner_id)
        if summary is None:
            return None
        views = await self._store.messages_view(conversation_id)
        lines: list[str] = []
        for view in views:
            # A compaction checkpoint is chassis bookkeeping, not something either party
            # said — reading it back as "Assistant" would put words in the model's mouth.
            if view.role == "compaction":
                continue
            content = view.content.strip()
            if content:
                label = "User" if view.role == "user" else "Assistant"
                lines.append(f"{label}: {content}")
        return ChatTranscript(
            conversation_id=conversation_id, title=summary.title, text="\n\n".join(lines)
        )


def snippet(text: str, query_tokens: set[str]) -> str:
    """A one-line excerpt, windowed around the first matching token when there is one,
    else the head of the text."""
    flat = " ".join(text.split())
    if len(flat) <= _SNIPPET_LEN:
        return flat
    lowered = flat.lower()
    positions = [lowered.find(token) for token in query_tokens]
    hit = min((p for p in positions if p >= 0), default=-1)
    if hit < 0:
        return flat[:_SNIPPET_LEN].rstrip() + "…"
    start = max(0, hit - _SNIPPET_LEN // 3)
    end = start + _SNIPPET_LEN
    excerpt = flat[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return f"{prefix}{excerpt}{suffix}"
