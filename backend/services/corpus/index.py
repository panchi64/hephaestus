"""``CorpusIndex`` — the one retrieval capability across every knowledge source.

This is the service the plan collapses two hand-rolled hybrid recalls into. It holds a
registry of :class:`SourceAdapter`, and ``retrieve`` fans a query out to the selected
adapters and **RRF-fuses** their per-source hits into one ranking. RRF is rank-based, so
scores from different stores (memory's fused score, a folder chunk's cosine) fuse cleanly
without normalization — a hit's globally-unique ``gid`` keys the fusion so two sources'
refs never collide.

It also owns the ``/rag`` surface's reads: ``list_sources`` (flatten every adapter's
status), ``stats`` (embedding model/dims from the registry + totals), and the folder
lifecycle (``add_folder``/``remove_folder``/``reindex``/``rebuild``) delegated to the
one :class:`FolderAdapter`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime

from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import DegradedCapabilityError, NotFoundError
from models.corpus import CorpusSource
from services import ranking
from services.corpus.adapter import CorpusDocument, CorpusHit, SourceAdapter, SourceStatus
from services.corpus.chunk_store import CorpusChunkStore, item_of
from services.corpus.folder import FolderAdapter
from services.embeddings import Embedder, embed_query
from services.registry import ModelRegistry

logger = logging.getLogger(__name__)


class CorpusIndex:
    def __init__(
        self,
        embedder: Embedder,
        registry: ModelRegistry,
        chunk_store: CorpusChunkStore,
        folder: FolderAdapter,
    ) -> None:
        self._embedder = embedder
        self._registry = registry
        self._chunks = chunk_store
        self._folder = folder
        # An ordered list, not a kind-keyed map: several stub surface adapters share
        # the "surface" kind, so keying by kind would drop all but the last.
        self._adapters: list[SourceAdapter] = []

    async def _out_of_scope_source_ids(
        self, owner_id: str, visible_projects: tuple[str | None, ...] | None
    ) -> frozenset[str]:
        """Sources belonging to a project the caller may **not** see.

        Expressed as an exclusion rather than an inclusion on purpose: only a source
        explicitly filed under a project is ever hidden, so an adapter that knows nothing
        about projects (memory, conversation search) keeps working untouched and every
        unfiled source stays reachable. Empty — and therefore free — whenever the caller
        is unscoped or nothing has been filed yet.
        """
        if visible_projects is None:
            return frozenset()
        visible = set(visible_projects)

        def work(session: Session) -> frozenset[str]:
            rows = session.exec(
                select(CorpusSource.id, CorpusSource.project_id)
                .where(CorpusSource.owner_id == owner_id)
                .where(col(CorpusSource.project_id).is_not(None))
            ).all()
            return frozenset(sid for sid, pid in rows if pid not in visible)

        return await in_session(self._folder.engine, work)

    def register(self, adapter: SourceAdapter) -> None:
        self._adapters.append(adapter)

    # --- retrieval --------------------------------------------------------

    async def retrieve(
        self,
        owner_id: str,
        query: str,
        *,
        sources: list[str] | None = None,
        source_ids: list[str] | None = None,
        limit: int = 8,
        visible_projects: tuple[str | None, ...] | None = None,
        since: datetime | None = None,
    ) -> list[CorpusHit]:
        """Fan the query out to the selected sources and RRF-fuse the results.

        ``sources`` filters to a subset of adapter kinds (default: all). ``source_ids``
        instead targets specific chunked sources by id (e.g. a chat's own attached files,
        whose ids the agent reads from the attachment marker): it goes straight to the
        chunk store across kinds — no adapter fan-out or cross-source fusion needed for a
        single logical group — and still honors ``kb_excluded``, so an excluded file is
        unreachable even by id. A degraded embedder collapses every source to keyword-only
        — the same fallback memory and conversation recall already use.

        ``visible_projects`` is the project scope (``services.projects``), and note that
        recall applies it **as a union, not a narrowing**: unfiled sources — which is
        every source that is not explicitly filed under a project — stay reachable from
        inside a project, because a project chat that could not reach the operator's
        general knowledge would be a worse assistant. What it excludes is the other
        direction: another project's sources, which is the contamination rule.

        ``since`` drops anything written or changed before it.
        """
        query_vec, query_model = await embed_query(self._embedder, owner_id, query)
        query_tokens = ranking.tokens(query)
        excluded = await self._out_of_scope_source_ids(owner_id, visible_projects)

        if source_ids:
            hits = await self._chunks.retrieve(
                owner_id,
                None,
                query_vec,
                query_model,
                query_tokens,
                limit=limit,
                source_ids=source_ids,
                since=since,
            )
            return [hit for hit in hits if hit.source_id not in excluded]

        selected = [
            adapter
            for adapter in self._adapters
            if sources is None or adapter.source_kind in sources
        ]
        per_source = await asyncio.gather(
            *(
                adapter.retrieve(
                    owner_id, query, query_vec, query_model, query_tokens, limit=limit, since=since
                )
                for adapter in selected
            )
        )
        if excluded:
            per_source = [
                [hit for hit in hits if hit.source_id not in excluded] for hits in per_source
            ]

        # Each adapter already ranked its own hits; fuse the sources by rank (RRF over
        # position). Rank-based fusion is the only sound way to combine results whose
        # raw scores live on different scales (a memory's fused score vs. a chunk cosine).
        by_gid: dict[str, CorpusHit] = {}
        ranked_lists: list[list[str]] = []
        for hits in per_source:
            ranked_lists.append([hit.gid for hit in hits])
            for hit in hits:
                by_gid[hit.gid] = hit

        fused = ranking.rrf_lists(ranked_lists)
        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [replace(by_gid[gid], score=score) for gid, score in ranked]

    async def read(
        self,
        owner_id: str,
        gid: str,
        *,
        visible_projects: tuple[str | None, ...] | None = None,
    ) -> CorpusDocument | None:
        """One item in full by a hit's ``gid``: a surface's own record, or a whole file
        stitched back from its chunks."""
        source_id, sep, ref = gid.partition(":")
        if not sep or not ref:
            return None
        surface = self._surface_adapter(source_id)
        if surface is not None:
            return await surface.read(owner_id, ref)
        if source_id in await self._out_of_scope_source_ids(owner_id, visible_projects):
            return None
        item = item_of(ref)
        found = await self._chunks.read_item(owner_id, source_id, item)
        if found is None:
            return None
        text, at = found
        return CorpusDocument(title=item, text=text, at=at)

    # --- the /rag surface -------------------------------------------------

    async def list_sources(self, owner_id: str) -> list[SourceStatus]:
        """Every source's status row, flattened — adapters that manage many instances
        (the folder adapter) contribute several rows."""
        rows: list[SourceStatus] = []
        for adapter in self._adapters:
            status = await adapter.status(owner_id)
            if isinstance(status, list):
                rows.extend(status)
            else:
                rows.append(status)
        return rows

    async def stats(self, owner_id: str) -> CorpusStats:
        """The corpus-wide readout: the active embedding model + dims (from the
        registry; degraded ⇒ unset), total chunked docs, and source/collection count."""
        model, dims = await self._embedding_facts(owner_id)
        total_docs = await self._chunks.count_all(owner_id)
        sources = await self.list_sources(owner_id)
        return CorpusStats(
            embedding_model=model,
            dims=dims,
            total_docs=total_docs,
            total_collections=len(sources),
        )

    async def _embedding_facts(self, owner_id: str) -> tuple[str | None, int | None]:
        try:
            spec = await self._registry.resolve_embedding_spec(owner_id)
        except DegradedCapabilityError, NotFoundError:
            return None, None
        return spec.model, None

    # --- folder lifecycle (delegated to the one FolderAdapter) ------------

    async def add_folder(
        self, owner_id: str, path: str, *, project_id: str | None = None
    ) -> CorpusSource:
        return await self._folder.add_folder(owner_id, path, project_id=project_id)

    async def remove_folder(self, owner_id: str, source_id: str) -> bool:
        return await self._folder.remove_folder(owner_id, source_id)

    async def reindex(self, owner_id: str, source_id: str | None = None) -> int:
        """Re-embed pending/stale chunks. Without ``source_id``, heal every adapter.
        With one: a surface id heals just that adapter; a folder id re-crawls only that
        folder (picking up changed files); an unknown id is a ``NotFoundError``."""
        if source_id is None:
            total = 0
            for adapter in self._adapters:
                total += await adapter.reindex(owner_id)
            return total
        surface = self._surface_adapter(source_id)
        if surface is not None:
            return await surface.reindex(owner_id)
        if not await self._folder.rebuild(owner_id, source_id):
            raise NotFoundError(f"source {source_id!r} not found")
        return 0

    async def rebuild(self, owner_id: str, source_id: str) -> bool:
        """Re-crawl a folder from scratch (folders only; surfaces own their content)."""
        return await self._folder.rebuild(owner_id, source_id)

    def _surface_adapter(self, source_id: str) -> SourceAdapter | None:
        """The surface adapter with this fixed id, or ``None`` (folders carry dynamic
        ids and are handled by the one folder adapter, not matched here)."""
        for adapter in self._adapters:
            if getattr(adapter, "SOURCE_ID", None) == source_id:
                return adapter
        return None


@dataclass(frozen=True)
class CorpusStats:
    """The corpus-wide stats readout (the ``/corpus/stats`` source of truth)."""

    embedding_model: str | None
    dims: int | None
    total_docs: int
    total_collections: int
