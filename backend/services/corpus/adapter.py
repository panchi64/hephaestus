"""The ``SourceAdapter`` seam — one knowledge source plugged into the corpus.

The corpus is a single retrieval index fed by many sources. Rather than force every
source into one table, each source is an **adapter**: it knows how to retrieve from
*its* store (a folder's chunk rows, the memory store, the conversation index) and how
to report *its* status to the ``/rag`` source list. Each adapter returns its hits
**already ranked** (best first); ``CorpusIndex`` fuses those per-source lists by rank
(Reciprocal Rank Fusion).

A hit's ``gid`` is globally unique across sources (``"{source_id}:{ref}"``) so the
fusion never collides two sources' refs. Fusion is purely **rank-based**, so a
source's own scores (a memory's fused score, a folder chunk's cosine) never have to
share a scale — only their ordering matters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import numpy as np


@dataclass(frozen=True)
class SourceStatus:
    """One row in the ``/rag`` source list — what the index knows about a source."""

    source_id: str
    kind: str  # "surface" | "folder"
    label: str
    doc_count: int
    status: str  # indexed | indexing | stale | error
    last_indexed_at: datetime | None
    error_hint: str | None = None
    href: str | None = None


@dataclass(frozen=True)
class CorpusHit:
    """One retrieved passage. ``gid`` is globally unique for cross-source fusion.

    ``matched_by`` (``both`` | ``semantic`` | ``keyword``) is set by the source that
    produced the hit — it knows which signal fired. ``dense_score``/``sparse_score`` are
    the source's own raw signals, kept for inspection/tests only; cross-source fusion is
    rank-based and does not read them. ``score`` is filled in by ``CorpusIndex`` with the
    final fused score."""

    gid: str  # f"{source_id}:{ref}"
    source_id: str
    ref: str
    text: str
    dense_score: float | None = None
    sparse_score: float | None = None
    score: float = 0.0
    matched_by: str = ""
    #: When the passage was written or last changed, for a ``since`` filter.
    at: datetime | None = None
    #: A name for the passage's origin when the ref alone is opaque (a conversation's title).
    title: str | None = None


@dataclass(frozen=True)
class CorpusDocument:
    """One item read in full, as ``platform_read`` returns it."""

    title: str
    text: str
    at: datetime | None = None


class SourceAdapter(ABC):
    """A knowledge source the corpus retrieves from and reports on.

    Concrete adapters wrap a store (a folder's chunks, memory, conversations). A
    chunked adapter overrides ``reindex`` to (re-)embed its rows; a wrapper over a
    store that embeds on write leaves the default no-op.
    """

    source_kind: str

    @abstractmethod
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
        """Hybrid recall from this source's store, returned **ranked** (best first) so
        the index can fuse it by position. ``query`` is the raw text (for stores that
        embed it themselves); ``query_vec``/``query_tokens`` are the pre-computed dense
        and sparse forms (for stores that score rows directly). A degraded query vector
        ⇒ sparse-only. Each hit carries its ``matched_by``. ``since`` drops anything older."""

    @abstractmethod
    async def status(self, owner_id: str) -> SourceStatus | list[SourceStatus]:
        """This source's row(s) for the ``/rag`` list — one status, or several for a
        registry of instances (e.g. one folder adapter managing many folders)."""

    async def read(self, owner_id: str, ref: str) -> CorpusDocument | None:
        """One item in full by its ref, or None when this source has no such item."""
        del owner_id, ref
        return None

    async def reindex(self, owner_id: str, *, current_model: str | None = None) -> int:
        """(Re-)embed this source's pending/stale chunks into ``current_model``'s
        space. Default no-op for adapters whose store embeds on its own write path
        (memory, conversations); chunked adapters override. Returns rows embedded."""
        del owner_id, current_model
        return 0
