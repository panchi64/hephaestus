"""The knowledge corpus — one retrieval index over many pluggable sources.

``CorpusIndex`` is the capability: it fans a query out to registered
:class:`SourceAdapter` instances and RRF-fuses their hits into one ranking, and it
owns the ``/rag`` surface reads (source list, stats, folder lifecycle). The existing
rich stores (memory, conversations) enroll as wrapper adapters untouched; chunked
content (folders now, more later) lands in the generic ``corpus_chunk`` store via
:class:`CorpusChunkStore`.
"""

from __future__ import annotations

from services.corpus.adapter import CorpusDocument, CorpusHit, SourceAdapter, SourceStatus
from services.corpus.chunk_store import CorpusChunkStore
from services.corpus.folder import FolderAdapter
from services.corpus.index import CorpusIndex, CorpusStats
from services.corpus.records import RecordAdapter, record_adapters
from services.corpus.wrappers import ConversationAdapter, MemoryAdapter

__all__ = [
    "ConversationAdapter",
    "CorpusChunkStore",
    "CorpusDocument",
    "CorpusHit",
    "CorpusIndex",
    "CorpusStats",
    "FolderAdapter",
    "MemoryAdapter",
    "RecordAdapter",
    "SourceAdapter",
    "SourceStatus",
    "record_adapters",
]
