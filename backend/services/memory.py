"""Long-term memory — store, recall by meaning, and manage memories.

The capability behind `MEM-*`. A memory is encrypted at rest (content + vector);
recall is **hybrid**: cosine over embeddings (the "by meaning" path) and BM25 (the keyword
path), fused with Reciprocal Rank Fusion on the memory-only :mod:`services.recall_index`.
This satisfies `MEM-2`'s "recall by meaning, keyword fallback" in one pass: a memory with
no vector contributes through the keyword signal alone. Pinned memories reach the agent
through its instructions (`MEM-4`, ``tools/memory.memory_catalog_instructions``), so recall
ranks them like any other.

Embeddings are compared **only within the same model/space** (`EMB-2`): a memory
embedded by a different model than the current one falls back to sparse, so a
model change degrades recall rather than corrupting it.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import override

import numpy as np
from sqlalchemy import Engine, func, or_
from sqlmodel import Session, col, select

from core.db import get_owned, in_session
from core.exceptions import DegradedCapabilityError, NotFoundError
from core.vault import Vault
from models.memory import Memory
from services.embeddings import (
    Embedder,
    decode_vector,
    embed_and_seal_rows,
    embed_query,
    encode_vector,
)
from services.ranking import cosine as _cosine
from services.ranking import tokens as _tokens
from services.recall_index import Collection, Doc, Loaded, Query, RecallIndex


@dataclass(frozen=True)
class MemoryView:
    """A decrypted memory for listing/editing (content in the clear to the owner)."""

    id: str
    content: str
    description: str
    pinned: bool
    created_at: datetime
    updated_at: datetime
    has_embedding: bool


#: How much of a memory's first line stands in for a description it was saved without.
DESCRIPTION_FALLBACK_CHARS = 80


@dataclass(frozen=True)
class MemoryCatalog:
    """The pinned memories as ``(id, description)`` pairs, and how many memories are not listed."""

    pinned: list[tuple[str, str]]
    others: int


@dataclass(frozen=True)
class RecallHit:
    memory: MemoryView
    score: float
    # How it surfaced: semantic (dense only), keyword (sparse only), or both.
    matched_by: str


@dataclass(frozen=True)
class DuplicateGroup:
    """Near-duplicate memories an audit flags for consolidation (`MEM-3`)."""

    memory_ids: list[str]
    similarity: float


class MemoryCollection(Collection[None]):
    name: str = "memories"

    @override
    def signature(self, session: Session, owner_id: str) -> Hashable:
        stmt = (
            select(Memory.embedding_model, func.count(), func.max(Memory.updated_at))
            .where(Memory.owner_id == owner_id)
            .group_by(col(Memory.embedding_model))
        )
        return frozenset(session.exec(stmt).all())

    @override
    def stamps(self, session: Session, owner_id: str) -> dict[str, Hashable]:
        stmt = select(Memory.id, Memory.updated_at, Memory.embedding_model).where(
            Memory.owner_id == owner_id
        )
        return {mid: (at, model) for mid, at, model in session.exec(stmt).all()}

    @override
    def load(
        self, session: Session, vault: Vault, owner_id: str, ids: list[str]
    ) -> list[Loaded[None]]:
        rows = session.exec(select(Memory).where(col(Memory.id).in_(ids))).all()
        return [
            Loaded(
                Doc(row.id, row.created_at, None),
                vault.decrypt_str(row.content_enc),
                decode_vector(vault, row.embedding_enc) if row.embedding_enc else None,
                row.embedding_model,
            )
            for row in rows
        ]


class MemoryStore:
    def __init__(self, engine: Engine, vault: Vault, embedder: Embedder) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._embedder: Embedder = embedder
        self._index: RecallIndex = RecallIndex(engine, vault)
        self._collection: MemoryCollection = MemoryCollection()

    @property
    def index(self) -> RecallIndex:
        """The recall index memories are searched through."""
        return self._index

    @property
    def embedder(self) -> Embedder:
        """The embedder for new memories and queries; assignable when the endpoint changes."""
        return self._embedder

    @embedder.setter
    def embedder(self, value: Embedder) -> None:
        self._embedder = value

    # --- write path -------------------------------------------------------

    async def remember(
        self,
        owner_id: str,
        content: str,
        *,
        description: str | None = None,
        pinned: bool = False,
    ) -> MemoryView:
        """Store a memory. A blank ``description`` reads as the content's first line."""
        model, dim, vector_enc = await self._embed_for_storage(owner_id, content)
        memory = Memory(
            owner_id=owner_id,
            content_enc=self._vault.encrypt_str(content),
            description_enc=self._seal_description(description),
            embedding_enc=vector_enc,
            embedding_model=model,
            embedding_dim=dim,
            pinned=pinned,
        )

        def work(session: Session) -> MemoryView:
            session.add(memory)
            session.flush()
            return self._view(memory)

        return await in_session(self._engine, work)

    async def update(
        self,
        owner_id: str,
        memory_id: str,
        *,
        content: str | None = None,
        description: str | None = None,
        pinned: bool | None = None,
    ) -> MemoryView:
        """Change what is given; a blank ``description`` falls back to the first line."""
        _ = await self._require(owner_id, memory_id)
        # Re-embed only when the content actually changed (EMB-2 provenance).
        new_vector = (
            await self._embed_for_storage(owner_id, content) if content is not None else None
        )

        def work(session: Session) -> MemoryView:
            memory = session.get(Memory, memory_id)
            assert memory is not None
            if content is not None and new_vector is not None:
                memory.content_enc = self._vault.encrypt_str(content)
                memory.embedding_model, memory.embedding_dim, memory.embedding_enc = new_vector
            if description is not None:
                memory.description_enc = self._seal_description(description)
            if pinned is not None:
                memory.pinned = pinned
            memory.updated_at = datetime.now(UTC)
            session.add(memory)
            session.flush()
            return self._view(memory)

        return await in_session(self._engine, work)

    async def delete(self, owner_id: str, memory_id: str) -> None:
        _ = await self._require(owner_id, memory_id)

        def work(session: Session) -> None:
            memory = session.get(Memory, memory_id)
            if memory is not None:
                session.delete(memory)

        await in_session(self._engine, work)

    async def delete_all(self, owner_id: str) -> int:
        """Delete every one of the owner's memories, pinned or not. Returns how many."""

        def work(session: Session) -> int:
            rows = session.exec(select(Memory).where(Memory.owner_id == owner_id)).all()
            for row in rows:
                session.delete(row)
            return len(rows)

        return await in_session(self._engine, work)

    async def consolidate(
        self,
        owner_id: str,
        keep_id: str,
        merge_ids: list[str],
        *,
        content: str,
        description: str,
        pinned: bool | None = None,
    ) -> MemoryView:
        """Rewrite ``keep_id`` as the merged memory and delete ``merge_ids``, in one commit.

        ``pinned=None`` pins the result if any of the merged memories was pinned.

        Raises:
            ValueError: ``merge_ids`` is empty or contains ``keep_id``.
            NotFoundError: one of the ids is not the owner's memory; nothing changes.
        """
        if not merge_ids:
            raise ValueError("nothing to merge")
        if keep_id in merge_ids:
            raise ValueError(f"{keep_id!r} is both kept and merged")
        ids = [keep_id, *dict.fromkeys(merge_ids)]
        model, dim, vector_enc = await self._embed_for_storage(owner_id, content)

        def work(session: Session) -> MemoryView:
            rows: list[Memory] = []
            for memory_id in ids:
                row = session.get(Memory, memory_id)
                if row is None or row.owner_id != owner_id:
                    raise NotFoundError(f"memory {memory_id!r} not found")
                rows.append(row)
            kept, *merged = rows
            kept.content_enc = self._vault.encrypt_str(content)
            kept.description_enc = self._seal_description(description)
            kept.embedding_model, kept.embedding_dim, kept.embedding_enc = model, dim, vector_enc
            kept.pinned = any(row.pinned for row in rows) if pinned is None else pinned
            kept.updated_at = datetime.now(UTC)
            session.add(kept)
            for row in merged:
                session.delete(row)
            session.flush()
            return self._view(kept)

        return await in_session(self._engine, work)

    # --- read path --------------------------------------------------------

    async def list_memories(self, owner_id: str) -> list[MemoryView]:
        """The chronological timeline, newest first (`MEM-1`)."""

        def work(session: Session) -> list[MemoryView]:
            rows = session.exec(
                select(Memory)
                .where(Memory.owner_id == owner_id)
                .order_by(col(Memory.created_at).desc())
            ).all()
            return [self._view(row) for row in rows]

        return await in_session(self._engine, work)

    async def count(self, owner_id: str) -> int:
        """How many memories the owner has — a scalar count that never decrypts
        (the overview readout, not the timeline)."""

        def work(session: Session) -> int:
            return session.exec(
                select(func.count()).select_from(Memory).where(Memory.owner_id == owner_id)
            ).one()

        return await in_session(self._engine, work)

    async def get(self, owner_id: str, memory_id: str) -> MemoryView:
        return self._view(await self._require(owner_id, memory_id))

    async def recall(
        self, owner_id: str, query: str, *, limit: int = 5, since: datetime | None = None
    ) -> list[RecallHit]:
        """Hybrid recall: dense (meaning) + sparse (keyword), fused.

        A degraded embedder (or a query that can't embed) collapses cleanly to
        keyword-only — the `MEM-2` fallback. ``since`` drops memories older than it."""
        query_vec, query_model = await embed_query(self._embedder, owner_id, query)
        ranked = await self._index.search(
            self._collection,
            owner_id,
            Query(_tokens(query), query_vec, query_model),
            limit=limit,
            keep=None if since is None else lambda doc: doc.at is not None and doc.at >= since,
        )
        ids = [hit.doc.id for hit in ranked]

        def work(session: Session) -> list[RecallHit]:
            rows = session.exec(
                select(Memory).where(Memory.owner_id == owner_id, col(Memory.id).in_(ids))
            ).all()
            views = {row.id: self._view(row) for row in rows}
            return [
                RecallHit(memory=views[hit.doc.id], score=hit.score, matched_by=hit.matched_by)
                for hit in ranked
                if hit.doc.id in views
            ]

        return await in_session(self._engine, work)

    async def catalog(self, owner_id: str, *, limit: int) -> MemoryCatalog:
        """The pinned memories' index lines, sorted by id so the block keeps its bytes while
        the set holds. Past ``limit`` the oldest pins drop out and are counted with the rest."""

        def work(session: Session) -> MemoryCatalog:
            total = session.exec(
                select(func.count()).select_from(Memory).where(Memory.owner_id == owner_id)
            ).one()
            pinned = session.exec(
                select(Memory)
                .where(Memory.owner_id == owner_id, col(Memory.pinned).is_(True))
                .order_by(col(Memory.created_at).desc())
                .limit(limit)
            ).all()
            entries = sorted((row.id, self._description(row)) for row in pinned)
            return MemoryCatalog(pinned=entries, others=total - len(entries))

        return await in_session(self._engine, work)

    async def audit(self, owner_id: str, *, threshold: float = 0.92) -> list[DuplicateGroup]:
        """Flag near-duplicate memories for consolidation (`MEM-3`).

        Pairs whose embeddings exceed ``threshold`` cosine similarity are grouped.
        Detection only: merging or deleting is a separate, deliberate call."""

        def work(session: Session) -> list[DuplicateGroup]:
            rows = session.exec(select(Memory).where(Memory.owner_id == owner_id)).all()
            embedded = [
                (
                    row.id,
                    row.embedding_model,
                    np.asarray(decode_vector(self._vault, row.embedding_enc)),
                )
                for row in rows
                if row.embedding_enc is not None
            ]
            return self._duplicate_groups(embedded, threshold)

        return await in_session(self._engine, work)

    async def reembed(
        self, owner_id: str, *, current_model: str | None = None, batch_size: int = 64
    ) -> int:
        """Re-embed memories whose vector is missing, or was produced by a model
        other than ``current_model`` — the heal path after the operator changes the
        embedding model. EMB-2 segregates vectors by model, so a stale vector silently
        falls back to keyword recall until this re-embeds it into the current space.
        Best-effort: a degraded embedder stops the run and leaves the rest for next
        time. Returns how many memories were re-embedded."""

        def pending(session: Session) -> list[tuple[str, str]]:
            # Filter stale rows in SQL so only those that need re-embedding are
            # loaded and decrypted (not the whole table).
            query = select(Memory).where(Memory.owner_id == owner_id)
            if current_model is None:
                query = query.where(col(Memory.embedding_enc).is_(None))
            else:
                query = query.where(
                    or_(
                        col(Memory.embedding_enc).is_(None),
                        col(Memory.embedding_model) != current_model,
                    )
                )
            rows = session.exec(query).all()
            return [(row.id, self._vault.decrypt_str(row.content_enc)) for row in rows]

        rows = await in_session(self._engine, pending)
        items = [(rid, text) for rid, text in rows if text.strip()]
        return await embed_and_seal_rows(
            engine=self._engine,
            vault=self._vault,
            embedder=self._embedder,
            owner_id=owner_id,
            model_cls=Memory,
            pending=items,
            batch_size=batch_size,
        )

    # --- internals --------------------------------------------------------

    async def _embed_for_storage(
        self, owner_id: str, content: str
    ) -> tuple[str | None, int | None, str | None]:
        """Embed content for storage, best-effort. Returns (model, dim, enc_vector);
        all-None when the embedder is unavailable (the memory is still stored, and
        recalls for it fall back to keyword)."""
        try:
            batch = await self._embedder.embed(owner_id, [content])
        except DegradedCapabilityError:
            return None, None, None
        vector = batch.vectors[0]
        return batch.model, batch.dim, encode_vector(self._vault, vector)

    def _duplicate_groups(
        self, embedded: list[tuple[str, str | None, np.ndarray]], threshold: float
    ) -> list[DuplicateGroup]:
        groups: list[DuplicateGroup] = []
        used: set[str] = set()
        for i, (id_a, model_a, vec_a) in enumerate(embedded):
            if id_a in used:
                continue
            cluster = [id_a]
            best = 0.0
            for id_b, model_b, vec_b in embedded[i + 1 :]:
                if id_b in used or model_b != model_a:
                    continue
                sim = _cosine(vec_a, vec_b)
                if sim >= threshold:
                    cluster.append(id_b)
                    used.add(id_b)
                    best = max(best, sim)
            if len(cluster) > 1:
                used.add(id_a)
                groups.append(DuplicateGroup(memory_ids=cluster, similarity=best))
        return groups

    async def _require(self, owner_id: str, memory_id: str) -> Memory:
        return await get_owned(self._engine, Memory, memory_id, owner_id, what="memory")

    def _seal_description(self, description: str | None) -> str | None:
        text = " ".join((description or "").split())
        return self._vault.encrypt_str(text) if text else None

    def _description(self, memory: Memory, content: str | None = None) -> str:
        if memory.description_enc is not None:
            return self._vault.decrypt_str(memory.description_enc)
        text = content if content is not None else self._vault.decrypt_str(memory.content_enc)
        lines = text.strip().splitlines()
        return lines[0].strip()[:DESCRIPTION_FALLBACK_CHARS] if lines else ""

    def _view(self, memory: Memory) -> MemoryView:
        content = self._vault.decrypt_str(memory.content_enc)
        return MemoryView(
            id=memory.id,
            content=content,
            description=self._description(memory, content),
            pinned=memory.pinned,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
            has_embedding=memory.embedding_enc is not None,
        )
