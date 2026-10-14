"""Uploads schema — a stored file plus the text extracted from it.

The operator uploads a file; the system keeps the original bytes and makes the
file's *contents* usable (`UP-1`/`UP-2`). For PDFs that means pulling the embedded
text, falling back to a vision model for image-only/scanned pages; the extracted
text is retained per upload, correctable afterward, and indexed into the knowledge
corpus so the agent can retrieve it.

At-rest posture mirrors documents and artifacts: the content the upload *is* — its
raw ``blob`` and the ``filename`` and ``extracted_text`` derived from it — is
**encrypted at rest** under the vault. Structural metadata stays in the clear so the
DB can list, dedup, and route without unsealing: ``owner_id``, ``mime``,
``size_bytes``, the content ``sha256`` (a one-way digest, the duplicate-recognition
key for `UP-1`), the extraction ``status``, the ``vision`` flag, and timestamps.

Extraction runs off the request path, so a row is born ``queued`` and moves through
``extracting`` → ``done``/``error`` as the worker drains it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class UploadStatus(StrEnum):
    """Where an upload is in the extraction pipeline. A plain string column carries
    it (no DB enum, so SQLite + Alembic stay simple), with this enum as the in-code
    vocabulary — matching how the corpus's ``status`` and a document version's
    ``origin`` are plain strings."""

    QUEUED = "queued"
    EXTRACTING = "extracting"
    DONE = "done"
    ERROR = "error"


class Upload(Table, table=True):
    __tablename__: ClassVar[str] = "uploads"
    # Duplicate recognition (`UP-1`) is enforced, not best-effort: identical bytes for
    # one owner collide on this constraint, so a create race can't sneak two copies in.
    # The unique index also serves the dedup lookup (owner_id + sha256), so sha256
    # needs no separate index.
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("owner_id", "sha256", name="uq_uploads_owner_sha256"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # AEAD ciphertext of the original filename (operator content).
    filename_enc: str
    mime: str
    size_bytes: int
    # sha256 of the raw bytes — a one-way digest, so it stays in the clear and keys
    # duplicate recognition (`UP-1`); covered by the owner+sha256 unique index above.
    sha256: str
    # AEAD ciphertext of the original file bytes (the source of truth).
    blob_enc: bytes
    # queued | extracting | done | error — the extraction lifecycle (off the request
    # path); UploadStatus is the in-code vocabulary.
    status: str = Field(default=UploadStatus.QUEUED, index=True)
    # AEAD ciphertext of the extracted text, once available (`UP-2`); null until the
    # worker finishes, and correctable by the operator afterward.
    extracted_text_enc: str | None = None
    # Whether the extracted text is non-empty — a cheap, clear flag so the library list
    # never has to decrypt the full text just to know there's content to show.
    has_text: bool = Field(default=False)
    # True when a vision model produced (some of) the text — an image-only/scanned PDF.
    vision: bool = Field(default=False)
    # Whether the operator has scoped this file out of the knowledge base. Clear
    # metadata (not content): when true the upload's corpus chunks are filtered out
    # of every `corpus.retrieve`, so it's no longer referenced from any chat — while
    # the bytes/extracted text and the chunks themselves stay sealed, so flipping it
    # back is instant. Default false: a fresh upload joins the corpus seamlessly.
    kb_excluded: bool = Field(default=False)
    # The operator's star on this image — clear metadata (a UI flag, not content),
    # mirroring kb_excluded. Lets a listing surface a starred subset without decrypting
    # anything; default false. Meaningful only for image uploads, but cheap and harmless
    # on any row.
    favorite: bool = Field(default=False)
    # Which extractor produced the current text: "basic" (the built-in fallback) or
    # "mineru" (high-fidelity), or "manual" once the operator corrects it. Clear
    # metadata, null until extracted — lets the UI flag fallback extractions as
    # candidates to re-run through MinerU for higher quality.
    extractor: str | None = None
    # A short, clear note about extraction (never operator content): the failure reason
    # when status is "error", or a degradation note on an otherwise-successful
    # extraction (e.g. pages beyond the page cap). Failure is signalled by `status`,
    # not by this being set — so it is *not* named `error`, which a reader could mistake
    # for "this upload failed" when it merely carries a done-state note.
    note: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)
