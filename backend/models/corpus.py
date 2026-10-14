"""Knowledge-corpus schema — the generic chunk store + the folder source registry.

The corpus is **one retrieval index** fed by many *sources* (the rich stores —
memory, conversations — plug in as adapters untouched; chunked reference content
— host folders and uploads — lands here).

Two tables:

- ``CorpusChunk`` — the generic chunk store. One source item (a file) becomes
  *many* chunks (a token-window slice of its text). Each chunk carries the same
  at-rest posture as every other recall vector: the text and its embedding are
  **encrypted at rest** under the vault (an embedding is invertible enough to leak
  its text, so it is sealed too — which is why recall is brute-force-in-Python over
  the decrypted working set rather than an in-DB plaintext ANN
  index). Structural metadata (owner, source, ordinal, content hash, embedding
  provenance, timestamps) stays in the clear so the DB can segregate stale
  embeddings (`EMB-2`) and dedup on re-index.
- ``CorpusSource`` — the operator-added **folder** registry (path + crawl status).
  The path is **sealed** like the chunks it produces: a path into the operator's own
  filesystem names their projects, clients and habits, so it is user content
  (`XC-SEC-3`). The crawl status stays in the clear so the ``/rag`` list can order
  and segregate rows without a key.

The chunk's embedding ``model`` + ``dim`` are recorded (`EMB-2`): when the operator
changes the embedding model, existing vectors are a different space, so retrieve
falls back to keyword for them until a rebuild re-embeds them.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field, UniqueConstraint

from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class CorpusChunk(Table, table=True):
    __tablename__: ClassVar[str] = "corpus_chunk"
    # Idempotent (re)index: the same content never inserts twice for the same *item*.
    # ``external_ref`` is in the key, not content alone: a source can hold many items (a
    # folder crawl holds a whole tree), and two byte-identical files under it — a repeated
    # LICENSE, two empty ``__init__.py``, one config copied into two environments — are
    # two items that must both be indexable. Keyed on content alone the second one can
    # never be stored, so it is invisible to search and every hit on the shared text is
    # cited against the first file's path.
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint(
            "owner_id",
            "source_id",
            "external_ref",
            "content_hash",
            name="uq_corpus_chunk_ref_content",
        ),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # Which adapter produced this chunk (e.g. "folder") and which source instance.
    source_kind: str = Field(index=True)
    source_id: str = Field(index=True)
    # A human-traceable pointer back to the origin, "<path>#<char-offset>".
    external_ref: str
    # sha256 of the chunk text — the dedup/idempotency key (a re-crawl of unchanged
    # content hashes to the same value, so the unique constraint skips the re-insert).
    content_hash: str = Field(index=True)
    # The chunk's position within its source item (0-based), for stable ordering.
    ordinal: int = 0
    # AEAD ciphertext of the chunk text (the source of truth for retrieval).
    text_enc: str
    # AEAD ciphertext of the embedding (a JSON float array); None until embedded
    # (a chunk is inserted with a null vector, then the backfill seals it).
    embedding_enc: str | None = None
    # Embedding provenance for EMB-2: which model/space produced the vector.
    embedding_model: str | None = None
    embedding_dim: int | None = None
    # Denormalized from the chunk's source (e.g. an upload's `kb_excluded`) so recall
    # filters in the DB without a per-source join: a chunk with this set is dropped
    # from every retrieve. Indexed for the `where kb_excluded = false` recall scan.
    # The source row stays authoritative; a toggle restamps its chunks here.
    kb_excluded: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class CorpusSource(Table, table=True):
    __tablename__: ClassVar[str] = "corpus_source"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # The project this belongs to, or null for **unfiled** — visible in every scope,
    # not orphaned. See models/project.py for the one scope rule.
    project_id: str | None = Field(default=None, index=True)
    # Only "folder" today — the operator-added host path. Surfaces are virtual
    # adapters (not rows): they manage their own content on their own pages.
    kind: str = Field(default="folder")
    # AEAD ciphertext of the host path the crawler walks. A path into the operator's own
    # filesystem names their projects, clients and habits, so it is user content and is
    # sealed (XC-SEC-3).
    path_enc: str | None = None
    # The pre-encryption cleartext path, kept only for rows written before the path was
    # sealed — reads fall back to it and the startup backfill drains it to null
    # (services/sealing.py). Same two-phase shape as `Conversation.title`.
    path: str | None = None
    # indexed | indexing | stale | error — the crawl state the /rag list renders.
    status: str = Field(default="indexing")
    # A short reason code for the last failure (e.g. "PATH NOT FOUND"), else None.
    error_hint: str | None = None
    last_indexed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
