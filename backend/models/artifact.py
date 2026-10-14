"""Published artifacts — the files an agent surfaces for preview.

When the agent produces something visual in its sandbox (an HTML report, a chart,
a code snippet), it *publishes* the file: the bytes are captured here, decoupled
from the sandbox's lifecycle, so the operator can preview them even after the
session is reaped. The bytes are **encrypted at rest** under the vault (they are
the operator's content) — and so are the two metadata fields that carry the
operator's own words, the title and the filename. A filename is not a routing
detail: ``q3-layoffs-draft.html`` says as much about the operator as the document
does, so it is sealed like every peer entity's equivalent (`XC-SEC-3`).

What stays in the clear is only what the UI needs to *route* to a preview without
a key: conversation, content type, kind, and size. That is the line
``Conversation.model`` already sits on — structural metadata, not content.

Keyed by ``conversation_id`` — not a foreign key, because an artifact can also
come from a stateless run (keyed by its run id), which is not a conversation row.
Every record carries the ``owner_id`` seam.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class Artifact(Table, table=True):
    __tablename__ = "artifacts"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    conversation_id: str = Field(index=True)
    run_id: str | None = None
    # AEAD ciphertext of the operator-facing title and the original filename. Both are
    # user content (see the module docstring), so both are sealed from their first write.
    title_enc: str | None = None
    filename_enc: str | None = None
    # The pre-encryption cleartext. Kept **only** for rows written before these fields
    # were sealed: reads prefer the `_enc` column and fall back here, and the startup
    # backfill seals each remaining value and nulls this out (services/sealing.py).
    # Nothing writes them any more, so they are null on every new row and drain to null
    # on every old one — a migration can't do the sealing, having no key before unlock.
    title: str | None = None
    filename: str | None = None
    content_type: str
    # Coarse rendering hint for the UI: "html" | "image" | "text" | "other".
    kind: str
    size: int
    # AEAD ciphertext of the file bytes (the source of truth).
    blob_enc: bytes
    created_at: datetime = Field(default_factory=utcnow, index=True)
