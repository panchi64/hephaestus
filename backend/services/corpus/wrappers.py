"""Adapter wrappers — plug the existing rich stores into the corpus seam.

The whole point of ADAPTER-UNIFIED: ``MemoryStore`` and ``ConversationSearch`` already
do hybrid recall over their own encrypted tables, so they enroll into the corpus
**untouched, zero migration** — a thin wrapper translates their hit shape to
:class:`CorpusHit` and their counts to :class:`SourceStatus`. They embed on their own
write path, so ``reindex`` delegates to their existing re-embed (memory) or is a no-op
(the conversation index re-embeds via its own coordinator).

There was a stub adapter here too — a placeholder that listed a planned surface with no
content, so the ``/rag`` list could show the whole picture before every extraction
pipeline existed. Its last holder was deep research, which is now a *conversation* and so
is already covered by the conversation adapter above; a placeholder with nothing left to
hold is just a source the operator can never index, so it went with it.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from core.exceptions import NotFoundError
from services.conversation_search import ConversationSearch
from services.corpus.adapter import CorpusDocument, CorpusHit, SourceAdapter, SourceStatus
from services.memory import MemoryStore


class MemoryAdapter(SourceAdapter):
    """Long-term memory as a corpus source — wraps :class:`MemoryStore`."""

    source_kind = "memory"
    SOURCE_ID = "surf-memory"

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def retrieve(
        self,
        owner_id: str,
        query: str,
        query_vec: np.ndarray | None,
        query_model: str | None,
        query_tokens: set[str],
        *,
        limit: int,
        since: datetime | None = None,
    ) -> list[CorpusHit]:
        # MemoryStore.recall embeds + hybrid-fuses internally, so it takes the raw query
        # string (not the index's pre-computed vector). Its hits come back ranked; the
        # index fuses this list with the others by rank.
        hits = await self._store.recall(owner_id, query, limit=limit, since=since)
        return [
            CorpusHit(
                gid=f"{self.SOURCE_ID}:{hit.memory.id}",
                source_id=self.SOURCE_ID,
                ref=hit.memory.id,
                text=hit.memory.content,
                score=hit.score,
                matched_by=hit.matched_by,
                at=hit.memory.created_at,
                title="Memory",
            )
            for hit in hits
        ]

    async def read(self, owner_id: str, ref: str) -> CorpusDocument | None:
        try:
            memory = await self._store.get(owner_id, ref)
        except NotFoundError:
            return None
        return CorpusDocument(title="Memory", text=memory.content, at=memory.created_at)

    async def status(self, owner_id: str) -> SourceStatus:
        count = await self._store.count(owner_id)
        return SourceStatus(
            source_id=self.SOURCE_ID,
            kind="surface",
            label="Memory",
            doc_count=count,
            status="indexed",
            last_indexed_at=None,
            href="/memory",
        )

    async def reindex(self, owner_id: str, *, current_model: str | None = None) -> int:
        return await self._store.reembed(owner_id, current_model=current_model)


class ConversationAdapter(SourceAdapter):
    """Cross-chat history as a corpus source — wraps :class:`ConversationSearch`."""

    source_kind = "conversations"
    SOURCE_ID = "surf-conversations"

    def __init__(self, search: ConversationSearch) -> None:
        self._search = search

    async def retrieve(
        self,
        owner_id: str,
        query: str,
        query_vec: np.ndarray | None,
        query_model: str | None,
        query_tokens: set[str],
        *,
        limit: int,
        since: datetime | None = None,
    ) -> list[CorpusHit]:
        # ConversationSearch.search embeds + hybrid-fuses internally; it takes the raw
        # query and returns hits already ranked (one per conversation).
        hits = await self._search.search(owner_id, query, limit=limit, since=since)
        return [
            CorpusHit(
                gid=f"{self.SOURCE_ID}:{hit.conversation_id}",
                source_id=self.SOURCE_ID,
                ref=hit.conversation_id,
                text=hit.snippet,
                score=hit.score,
                matched_by=hit.matched_by,
                at=hit.updated_at,
                title=hit.title or "(untitled conversation)",
            )
            for hit in hits
        ]

    async def read(self, owner_id: str, ref: str) -> CorpusDocument | None:
        transcript = await self._search.read(owner_id, ref)
        if transcript is None:
            return None
        return CorpusDocument(
            title=transcript.title or "(untitled conversation)", text=transcript.text
        )

    async def status(self, owner_id: str) -> SourceStatus:
        return SourceStatus(
            source_id=self.SOURCE_ID,
            kind="surface",
            label="Conversations",
            doc_count=0,
            status="indexed",
            last_indexed_at=None,
            href="/chat",
        )
