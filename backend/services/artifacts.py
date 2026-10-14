"""Artifact store — capture and serve a View version's preview bytes.

The bytes behind a static View **preview**. When the agent ``show``s a file, its
bytes are captured here (encrypted at rest under the vault) and the version that
``services/workspace_history`` records points at the artifact as its preview;
serving returns the decrypted bytes with their content type for the UI to render
(``<img>`` / sandboxed iframe / inline). A pure blob+metadata store — the version,
its history, and its diffs live in the workspace-history store, not here.

Content-type inference is shared (``guess_content_type``) so the tool and routes
agree; ``kind`` is a coarse rendering hint the UI keys on.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine
from sqlmodel import Session

from core.db import in_session
from core.exceptions import NotFoundError
from core.vault import Vault
from models.artifact import Artifact
from services.sealing import open_sealed


def guess_content_type(filename: str) -> str:
    """A best-effort content type from the name; unknown falls back to text."""
    return mimetypes.guess_type(filename)[0] or "text/plain"


def _kind(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type in ("text/html", "application/xhtml+xml"):
        return "html"
    if content_type.startswith("text/") or content_type == "application/json":
        return "text"
    return "other"


@dataclass(frozen=True)
class ArtifactView:
    """Artifact metadata for listing and the publish event (no bytes)."""

    id: str
    conversation_id: str
    title: str
    filename: str
    content_type: str
    kind: str
    size: int
    created_at: datetime


@dataclass(frozen=True)
class ArtifactBlob:
    """A decrypted artifact, ready to serve."""

    filename: str
    content_type: str
    content: bytes


class ArtifactStore:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault

    async def publish(
        self,
        owner_id: str,
        conversation_id: str,
        *,
        filename: str,
        content: bytes,
        content_type: str | None = None,
        title: str | None = None,
        run_id: str | None = None,
    ) -> ArtifactView:
        """Capture a file's bytes as a previewable artifact (encrypted at rest)."""
        ctype = content_type or guess_content_type(filename)
        artifact = Artifact(
            owner_id=owner_id,
            conversation_id=conversation_id,
            run_id=run_id,
            title_enc=self._vault.encrypt_str(title or filename),
            filename_enc=self._vault.encrypt_str(filename),
            content_type=ctype,
            kind=_kind(ctype),
            size=len(content),
            blob_enc=self._vault.encrypt_bytes(content),
        )

        def work(session: Session) -> ArtifactView:
            session.add(artifact)
            session.flush()
            return self._to_view(artifact)

        return await in_session(self._engine, work)

    async def content(self, owner_id: str, artifact_id: str) -> ArtifactBlob:
        def work(session: Session) -> ArtifactBlob:
            row = self._require(session, owner_id, artifact_id)
            return ArtifactBlob(
                filename=self._filename(row),
                content_type=row.content_type,
                content=self._vault.decrypt_bytes(row.blob_enc),
            )

        return await in_session(self._engine, work)

    @staticmethod
    def _require(session: Session, owner_id: str, artifact_id: str) -> Artifact:
        row = session.get(Artifact, artifact_id)
        if row is None or row.owner_id != owner_id:
            raise NotFoundError(f"artifact {artifact_id!r} not found")
        return row

    def _filename(self, row: Artifact) -> str:
        # `or ""` covers only the impossible row where both columns are null; every
        # real row has one or the other, whichever side of the backfill it is on.
        return open_sealed(self._vault, row.filename_enc, row.filename) or ""

    def _to_view(self, row: Artifact) -> ArtifactView:
        return ArtifactView(
            id=row.id,
            conversation_id=row.conversation_id,
            title=open_sealed(self._vault, row.title_enc, row.title) or "",
            filename=self._filename(row),
            content_type=row.content_type,
            kind=row.kind,
            size=row.size,
            created_at=row.created_at,
        )
