"""Workspace history — content-addressed snapshots of the agent's sandbox.

After a turn that changed files, the workspace's text files are captured as a
**snapshot**: a point-in-time tree the operator can browse (code) and diff against
the previous one. File bytes are content-addressed and **encrypted at rest** under
the vault, deduped across snapshots by content hash; the snapshot's manifest
(path → hash) and small change-stats stay in the clear (like an artifact's
filename) — the file *content* does not.

Keyed by ``conversation_id`` — not a foreign key, because a snapshot can also come
from a stateless run (keyed by its run id). Every record carries the ``owner_id``
seam.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class WorkspaceBlob(Table, table=True):
    __tablename__: ClassVar[str] = "workspace_blobs"
    # One row per distinct file content per owner — snapshots share blobs by hash.
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("owner_id", "sha256", name="uq_workspace_blob_owner_sha"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # Hex sha256 of the plaintext file bytes — the dedup key within an owner.
    sha256: str = Field(index=True)
    size: int
    # AEAD ciphertext of the file bytes (the source of truth).
    blob_enc: bytes
    created_at: datetime = Field(default_factory=utcnow)


class WorkspaceSnapshot(Table, table=True):
    __tablename__: ClassVar[str] = "workspace_snapshots"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    conversation_id: str = Field(index=True)
    run_id: str | None = None
    title: str | None = None
    # AEAD ciphertext of the JSON {relpath: sha256} tree at this point. Encrypted at
    # rest because the file paths/names reveal the operator's private workspace
    # structure; the bytes they point at are encrypted in WorkspaceBlob.
    manifest_enc: bytes
    # JSON {"added": n, "modified": n, "removed": n} vs. the previous snapshot —
    # bare counts, no content, so kept in the clear for cheap timeline listing.
    stats_json: str
    # How this version is previewed on the View stage. A `show(file=…)` stamps the
    # captured-bytes artifact that backs the preview + its coarse render kind
    # ("image" | "html" | "text" | "other"); a `show(serve=…)`/no explicit file
    # leaves both null, and the frontend auto-picks an entry HTML page from the tree.
    # Policy, not content — kept in the clear like `stats_json`.
    preview_artifact_id: str | None = None
    preview_kind: str | None = None
    # The operator's durable bookmark on this version — a keeper worth finding again
    # amid an otherwise disposable timeline. Policy, not content, kept in the clear
    # like `preview_kind`.
    keeper: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
