"""The generic chunk store — CRUD + hybrid rank over ``corpus_chunk``.

The shared backend for every *chunked* source (folders and uploads today). It is
the corpus counterpart of ``MemoryStore`` /
``ConversationSearch``: rows are encrypted at rest (text + vector) and ranked hybrid
(cosine + BM25) on the memory-only :mod:`services.recall_index`; ``CorpusIndex`` fuses
the result with the other sources.

Inserts are **idempotent per item**: a re-crawl of an unchanged item hashes to the same
values and is skipped, so reindex never dupes. The dedup key is ``(item, content)``, not
content alone — two byte-identical files under one source (a repeated LICENSE, two empty
``__init__.py``) are two items and must both be indexed, or one of them is invisible to
search and every hit on the shared text is cited to the other's path.

``upsert`` only ever *inserts*, so a source whose origin can lose or change items needs
``prune_stale`` to reconcile the other direction. Embedding reuses the one shared loop,
``embed_and_seal_rows(model_cls=CorpusChunk)``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Hashable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import override

import numpy as np
from sqlalchemy import Engine, func, or_
from sqlmodel import Session, col, delete, select, update

from core.db import in_session
from core.vault import Vault
from models.corpus import CorpusChunk
from services.chunking import Chunk
from services.corpus.adapter import CorpusHit
from services.embeddings import Embedder, decode_vector, embed_and_seal_rows
from services.recall_index import Collection as RecallCollection
from services.recall_index import Doc, Loaded, Query, RecallIndex


def content_hash(text: str) -> str:
    """The dedup/idempotency key for a chunk — sha256 of its text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def item_of(external_ref: str) -> str:
    """The origin item an ``external_ref`` points into — its ``"<item>#<offset>"`` head.

    The offset moves whenever an edit shifts the text, so the *item* is the stable half
    and the only thing two crawls can compare. Split on the **last** ``#`` so a path that
    itself contains one stays intact."""
    return external_ref.rsplit("#", 1)[0]


# How many ids one delete statement binds. A wide re-crawl (a whole subtree removed) can
# doom more rows than SQLite will accept parameters for in a single statement, so the
# prune goes out in batches rather than betting on the driver's limit.
_DELETE_BATCH = 500


@dataclass(frozen=True)
class ChunkMeta:
    source_kind: str
    source_id: str
    external_ref: str
    kb_excluded: bool


class ChunkCollection(RecallCollection[ChunkMeta]):
    name: str = "chunks"

    @override
    def signature(self, session: Session, owner_id: str) -> Hashable:
        stmt = (
            select(
                CorpusChunk.embedding_model,
                CorpusChunk.kb_excluded,
                func.count(),
                func.max(CorpusChunk.updated_at),
            )
            .where(CorpusChunk.owner_id == owner_id)
            .group_by(col(CorpusChunk.embedding_model), col(CorpusChunk.kb_excluded))
        )
        return frozenset(session.exec(stmt).all())

    @override
    def stamps(self, session: Session, owner_id: str) -> dict[str, Hashable]:
        stmt = select(
            CorpusChunk.id,
            CorpusChunk.updated_at,
            CorpusChunk.embedding_model,
            CorpusChunk.kb_excluded,
        ).where(CorpusChunk.owner_id == owner_id)
        return {cid: (at, model, excluded) for cid, at, model, excluded in session.exec(stmt).all()}

    @override
    def load(
        self, session: Session, vault: Vault, owner_id: str, ids: list[str]
    ) -> list[Loaded[ChunkMeta]]:
        rows = session.exec(select(CorpusChunk).where(col(CorpusChunk.id).in_(ids))).all()
        return [
            Loaded(
                Doc(
                    row.id,
                    row.updated_at,
                    ChunkMeta(row.source_kind, row.source_id, row.external_ref, row.kb_excluded),
                ),
                vault.decrypt_str(row.text_enc),
                decode_vector(vault, row.embedding_enc) if row.embedding_enc else None,
                row.embedding_model,
            )
            for row in rows
        ]


#: Longest run of words two consecutive chunks are checked for sharing.
_MAX_OVERLAP = 256


def stitch(passages: list[str]) -> str:
    """Join consecutive chunks back into one text, dropping the words each repeats from
    the one before (chunk windows overlap)."""
    words: list[str] = []
    for passage in passages:
        incoming = passage.split()
        shared = next(
            (
                size
                for size in range(min(len(words), len(incoming), _MAX_OVERLAP), 0, -1)
                if words[-size:] == incoming[:size]
            ),
            0,
        )
        words.extend(incoming[shared:])
    return " ".join(words)


class CorpusChunkStore:
    def __init__(self, engine: Engine, vault: Vault, embedder: Embedder) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._embedder: Embedder = embedder
        self._index: RecallIndex = RecallIndex(engine, vault)
        self._collection: ChunkCollection = ChunkCollection()

    @property
    def vault(self) -> Vault:
        """The corpus's at-rest key. Exposed so an adapter that seals a column on its own
        registry table (``FolderAdapter``'s host path) uses the same vault the chunks it
        produces are sealed with, rather than being handed a second one at construction."""
        return self._vault

    # --- write path -------------------------------------------------------

    async def upsert(
        self,
        owner_id: str,
        source_kind: str,
        source_id: str,
        base_ref: str,
        chunks: list[Chunk],
        *,
        kb_excluded: bool = False,
    ) -> int:
        """Insert each chunk's sealed text with a null vector, skipping any whose content
        hash this source already holds **for this same item** (idempotent reindex).
        Returns how many new chunks were inserted. ``kb_excluded`` is stamped on every new
        chunk so a source that's currently scoped out of the knowledge base stays out
        across a reindex (the source row is authoritative; the caller passes its state).

        Insert-only: a caller whose origin can drop or edit items pairs this with
        :meth:`prune_stale`."""
        prepared = [
            (
                content_hash(chunk.text),
                f"{base_ref}#{chunk.offset}",
                chunk.ordinal,
                self._vault.encrypt_str(chunk.text),
            )
            for chunk in chunks
            if chunk.text.strip()
        ]

        def work(session: Session) -> int:
            # Scoped to *this item*, not the whole source. The hash set exists to stop a
            # re-crawl re-inserting a chunk it already holds for the same file; widened to
            # the source it also swallows a different file that happens to be byte-identical
            # — the second file is then never indexed at all, and every retrieval hit on the
            # shared text is cited against the first path that claimed it. `autoescape`
            # matters: a prefix goes into a LIKE, and paths are full of `_` (`__init__.py`
            # is the collision case), which would otherwise match any character.
            existing = set(
                session.exec(
                    select(CorpusChunk.content_hash).where(
                        CorpusChunk.owner_id == owner_id,
                        CorpusChunk.source_id == source_id,
                        col(CorpusChunk.external_ref).startswith(f"{base_ref}#", autoescape=True),
                    )
                ).all()
            )
            inserted = 0
            for chash, ref, ordinal, text_enc in prepared:
                if chash in existing:
                    continue
                existing.add(chash)  # dedup within this batch too
                session.add(
                    CorpusChunk(
                        owner_id=owner_id,
                        source_kind=source_kind,
                        source_id=source_id,
                        external_ref=ref,
                        content_hash=chash,
                        ordinal=ordinal,
                        text_enc=text_enc,
                        kb_excluded=kb_excluded,
                    )
                )
                inserted += 1
            return inserted

        return await in_session(self._engine, work)

    async def prune_stale(
        self,
        owner_id: str,
        source_id: str,
        *,
        live_refs: Collection[str],
        live_hashes: Mapping[str, set[str]],
    ) -> int:
        """Reconcile a source down to what its origin still holds — the *delete* half of a
        re-index, which the insert-only :meth:`upsert` cannot do on its own.

        Two kinds of row go. One whose item is absent from ``live_refs``: the file was
        deleted, renamed, grew past the crawler's size cap, or stopped being text-like, so
        nothing behind it exists any more and leaving it in means recall keeps quoting
        content the operator removed. And one whose item *is* live but whose content hash
        is absent from ``live_hashes`` for it: the file was edited, so that slice is
        superseded and would otherwise sit alongside the new text as a second, stale
        version of the same passage.

        An item in ``live_refs`` with **no** ``live_hashes`` entry is left entirely alone.
        That is the "seen but unreadable" case — the crawler knows the file is there but
        couldn't open it — and a transient permission error must never erase a file's
        index. Callers pass hashes only for items they actually read.

        Returns how many chunks were removed."""
        live = set(live_refs)

        def work(session: Session) -> int:
            # Only the three structural columns — the sealed text and vector are never
            # needed to decide this, and they are by far the widest columns in the row.
            rows = session.exec(
                select(CorpusChunk.id, CorpusChunk.external_ref, CorpusChunk.content_hash).where(
                    CorpusChunk.owner_id == owner_id,
                    CorpusChunk.source_id == source_id,
                )
            ).all()
            doomed: list[str] = []
            for row_id, ref, chash in rows:
                item = item_of(ref)
                keep = live_hashes.get(item)
                if item not in live or (keep is not None and chash not in keep):
                    doomed.append(row_id)
            for start in range(0, len(doomed), _DELETE_BATCH):
                _ = session.exec(
                    delete(CorpusChunk).where(
                        col(CorpusChunk.id).in_(doomed[start : start + _DELETE_BATCH])
                    )
                )
            return len(doomed)

        return await in_session(self._engine, work)

    async def delete_source(self, owner_id: str, source_id: str) -> None:
        """Drop every chunk a source contributed (folder removal)."""

        def work(session: Session) -> None:
            _ = session.exec(
                delete(CorpusChunk).where(
                    col(CorpusChunk.owner_id) == owner_id,
                    col(CorpusChunk.source_id) == source_id,
                )
            )

        await in_session(self._engine, work)

    async def set_excluded(self, owner_id: str, source_id: str, value: bool) -> None:
        """Flip ``kb_excluded`` on every chunk a source contributed — the retroactive
        knowledge-base toggle. The write itself needs no vault key (a clear column), but
        callers serialize it against (re)index jobs via their lock-aware worker (the
        uploads adapter does), so in practice it parks while the vault is locked. The
        denormalized flag is what ambient ``retrieve`` filters on, so this is what makes
        an already-indexed file vanish from (or return to) general recall. Idempotent."""

        def work(session: Session) -> None:
            _ = session.exec(
                update(CorpusChunk)
                .where(
                    col(CorpusChunk.owner_id) == owner_id,
                    col(CorpusChunk.source_id) == source_id,
                )
                .values(kb_excluded=value)
            )

        await in_session(self._engine, work)

    async def reembed(
        self,
        owner_id: str,
        source_id: str | None = None,
        *,
        source_kind: str | None = None,
        current_model: str | None = None,
        batch_size: int = 64,
    ) -> int:
        """Embed chunks whose vector is missing, or was produced by a model other than
        ``current_model`` (the EMB-2 heal path after an embedding-model change). Scoped
        to one ``source_id`` or one ``source_kind`` when given, else the owner's whole
        chunk store (the global heal). A per-surface heal passes its ``source_kind`` so
        it touches only its own chunks, not every other surface's. Reuses the one shared
        embed→seal loop. Returns how many chunks were embedded."""

        def pending(session: Session) -> list[tuple[str, str]]:
            query = select(CorpusChunk).where(CorpusChunk.owner_id == owner_id)
            if source_id is not None:
                query = query.where(CorpusChunk.source_id == source_id)
            if source_kind is not None:
                query = query.where(CorpusChunk.source_kind == source_kind)
            if current_model is None:
                query = query.where(col(CorpusChunk.embedding_enc).is_(None))
            else:
                query = query.where(
                    or_(
                        col(CorpusChunk.embedding_enc).is_(None),
                        col(CorpusChunk.embedding_model) != current_model,
                    )
                )
            rows = session.exec(query).all()
            return [(row.id, self._vault.decrypt_str(row.text_enc)) for row in rows]

        rows = await in_session(self._engine, pending)
        items = [(rid, text) for rid, text in rows if text.strip()]
        return await embed_and_seal_rows(
            engine=self._engine,
            vault=self._vault,
            embedder=self._embedder,
            owner_id=owner_id,
            model_cls=CorpusChunk,
            pending=items,
            batch_size=batch_size,
        )

    # --- read path --------------------------------------------------------

    async def read_item(
        self, owner_id: str, source_id: str, item: str
    ) -> tuple[str, datetime] | None:
        """One origin item's chunks joined in order, with when it last changed. A named
        read, so an item scoped out of the knowledge base still reads."""

        def work(session: Session) -> tuple[str, datetime] | None:
            rows = session.exec(
                select(CorpusChunk)
                .where(CorpusChunk.owner_id == owner_id)
                .where(CorpusChunk.source_id == source_id)
                .order_by(col(CorpusChunk.ordinal))
            ).all()
            chunks = [row for row in rows if item_of(row.external_ref) == item]
            if not chunks:
                return None
            text = stitch([self._vault.decrypt_str(row.text_enc) for row in chunks])
            return text, max(row.updated_at for row in chunks)

        return await in_session(self._engine, work)

    async def count(self, owner_id: str, source_id: str) -> int:
        """How many chunks a source has indexed (never decrypts)."""

        def work(session: Session) -> int:
            return session.exec(
                select(func.count())
                .select_from(CorpusChunk)
                .where(CorpusChunk.owner_id == owner_id, CorpusChunk.source_id == source_id)
            ).one()

        return await in_session(self._engine, work)

    async def count_items(
        self, owner_id: str, source_kind: str, source_id: str | None = None
    ) -> int:
        """How many distinct source *items* a kind has indexed (never decrypts).

        An item is one origin document/file, regardless of how many chunks it split
        into — the count the ``/rag`` row labels "DOCS". An item is identified the same
        way :meth:`prune_stale` identifies one, via :func:`item_of`. Scoped to one
        ``source_id`` when given (a single folder's file count), else the whole kind
        (e.g. every document). ``external_ref`` is structural/unencrypted, so this only
        loads short ref strings."""

        def work(session: Session) -> int:
            # Counts indexed items regardless of kb_excluded — the /rag DOCS readout
            # reflects what's been indexed/managed, not what ambient recall returns; an
            # excluded file is still indexed and reachable from the uploads page.
            query = select(CorpusChunk.external_ref).where(
                CorpusChunk.owner_id == owner_id,
                CorpusChunk.source_kind == source_kind,
            )
            if source_id is not None:
                query = query.where(CorpusChunk.source_id == source_id)
            refs = session.exec(query).all()
            return len({item_of(ref) for ref in refs})

        return await in_session(self._engine, work)

    async def count_all(self, owner_id: str) -> int:
        """Total chunks across every chunked source (the corpus-stats readout)."""

        def work(session: Session) -> int:
            return session.exec(
                select(func.count())
                .select_from(CorpusChunk)
                .where(CorpusChunk.owner_id == owner_id)
            ).one()

        return await in_session(self._engine, work)

    async def retrieve(
        self,
        owner_id: str,
        source_kind: str | None,
        query_vec: np.ndarray | None,
        query_model: str | None,
        query_tokens: set[str],
        *,
        limit: int,
        source_id: str | None = None,
        source_ids: list[str] | None = None,
        since: datetime | None = None,
    ) -> list[CorpusHit]:
        """Hybrid recall over a source's chunks, scored but **unfused** (the index
        fuses across sources). Filtered to ``source_kind`` (pass ``None`` to span every
        kind — a targeted read by id), to one ``source_id``, or to a set of
        ``source_ids`` (e.g. a chat's own attached files). ``kb_excluded`` governs
        **ambient recall only**: a kind-scoped/fan-out read hides excluded chunks, but an
        explicit ``source_ids`` fetch overrides exclusion — a caller that names a file
        (the chat reading its own attachment from the marker) still gets it, while it
        stays out of every other chat's general recall. A degraded query vector collapses
        to sparse-only."""

        wanted = set(source_ids) if source_ids is not None else None

        def keep(doc: Doc[ChunkMeta]) -> bool:
            meta = doc.meta
            if source_kind is not None and meta.source_kind != source_kind:
                return False
            if source_id is not None and meta.source_id != source_id:
                return False
            if since is not None and (doc.at is None or doc.at < since):
                return False
            if wanted is not None:
                return meta.source_id in wanted
            return not meta.kb_excluded

        ranked = await self._index.search(
            self._collection,
            owner_id,
            Query(query_tokens, query_vec, query_model),
            limit=limit,
            keep=keep,
        )
        ids = [hit.doc.id for hit in ranked]

        def work(session: Session) -> list[CorpusHit]:
            rows = session.exec(select(CorpusChunk).where(col(CorpusChunk.id).in_(ids))).all()
            text = {row.id: self._vault.decrypt_str(row.text_enc) for row in rows}
            return [
                CorpusHit(
                    gid=f"{hit.doc.meta.source_id}:{hit.doc.meta.external_ref}",
                    source_id=hit.doc.meta.source_id,
                    ref=hit.doc.meta.external_ref,
                    text=text[hit.doc.id],
                    dense_score=hit.dense,
                    sparse_score=hit.sparse,
                    matched_by=hit.matched_by,
                    at=hit.doc.at,
                )
                for hit in ranked
                if hit.doc.id in text
            ]

        return await in_session(self._engine, work)
