"""The uploads source — index extracted upload text into the corpus.

The in-app counterpart to :class:`~services.corpus.documents.DocumentsAdapter`: where
that indexes document bodies, this indexes the **text extracted from uploaded files**
(the `UP-2` output) so the agent retrieves an upload's contents through the one
``corpus.retrieve`` tool. Only the extracted text is indexed — never the raw bytes —
and it's chunked and sealed into the shared ``corpus_chunk`` store exactly as documents
and folders are.

Each upload is its own ``source_id`` (the upload id) under the shared
``source_kind="uploads"``, so re-indexing a single upload (after the operator corrects
its extracted text) touches only that upload's chunks. Indexing runs on the same
lock-aware :class:`~core.worker.WriteBehindWorker` the documents source uses, and like
that source an index job **clears the upload's existing chunks before inserting** — a
correction that shortens the text must not strand orphan chunks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import override

import numpy as np
from sqlalchemy import Engine
from sqlmodel import Session

from core.db import in_session
from core.worker import WriteBehindWorker
from models.upload import Upload
from services.chunking import chunk_text
from services.corpus.adapter import CorpusHit, SourceAdapter, SourceStatus
from services.corpus.chunk_store import CorpusChunkStore


@dataclass(frozen=True)
class UploadIndexJob:
    """A queued (re)index of one upload's extracted text, drained off the request path.

    ``text`` is the current extracted text to index, or ``None`` for a removal
    (delete): either way the worker first clears the upload's existing chunks, then —
    when text is present — chunks, seals, and embeds it."""

    owner_id: str
    upload_id: str
    text: str | None


@dataclass(frozen=True)
class UploadExcludeJob:
    """A queued restamp of one upload's chunks' ``kb_excluded`` — the retroactive
    knowledge-base toggle. Routed through the same worker as (re)index/remove so it can
    never race a queued reindex of the same upload."""

    owner_id: str
    upload_id: str
    kb_excluded: bool


# What the uploads worker drains: an index/remove or a KB-exclusion restamp.
UploadJob = UploadIndexJob | UploadExcludeJob


class UploadsAdapter(SourceAdapter):
    source_kind: str = "uploads"
    SOURCE_ID: str = "surf-uploads"

    def __init__(
        self, engine: Engine, chunk_store: CorpusChunkStore, unlocked: asyncio.Event | None
    ) -> None:
        self._engine: Engine = engine
        self._chunks: CorpusChunkStore = chunk_store
        self._worker: WriteBehindWorker[UploadJob] = WriteBehindWorker(
            self._handle, name="corpus-uploads", unlocked=unlocked
        )

    async def start(self) -> None:
        await self._worker.start()

    async def stop(self) -> None:
        await self._worker.stop()

    # --- upload lifecycle (called by the UploadStore after extraction) --------

    def index_upload(self, owner_id: str, upload_id: str, text: str) -> None:
        """Queue a (re)index of one upload's extracted text. Idempotent: the job clears
        the upload's prior chunks before inserting the current ones, so first-extraction
        and a later correction share one path."""
        self._worker.submit(UploadIndexJob(owner_id, upload_id, text))

    def remove_upload(self, owner_id: str, upload_id: str) -> None:
        """Queue removal of an upload's chunks (delete). Routed through the worker so it
        can't race a still-queued index of the same upload."""
        self._worker.submit(UploadIndexJob(owner_id, upload_id, None))

    def set_excluded(self, owner_id: str, upload_id: str, value: bool) -> None:
        """Queue a restamp of the upload's chunks' ``kb_excluded`` — the retroactive
        knowledge-base toggle. Same worker as index/remove, so it serializes behind any
        queued reindex of this upload and can't be undone by one."""
        self._worker.submit(UploadExcludeJob(owner_id, upload_id, value))

    # --- adapter contract -------------------------------------------------

    @override
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
        return await self._chunks.retrieve(
            owner_id,
            self.source_kind,
            query_vec,
            query_model,
            query_tokens,
            limit=limit,
            since=since,
        )

    @override
    async def status(self, owner_id: str) -> SourceStatus:
        return SourceStatus(
            source_id=self.SOURCE_ID,
            kind="surface",
            label="Uploads",
            doc_count=await self._chunks.count_items(owner_id, self.source_kind),
            status="indexed",
            last_indexed_at=None,
            href="/uploads",
        )

    @override
    async def reindex(self, owner_id: str, *, current_model: str | None = None) -> int:
        return await self._chunks.reembed(
            owner_id, source_kind=self.source_kind, current_model=current_model
        )

    # --- indexing ---------------------------------------------------------

    async def _handle(self, job: UploadJob) -> None:
        """The worker handler. An exclusion restamp is a clear-column flip; an index job
        clears the upload's existing chunks, then (when text is present) chunks, seals,
        and embeds it. Runs only while the vault is unlocked, since indexing seals text +
        vectors."""
        if isinstance(job, UploadExcludeJob):
            await self._chunks.set_excluded(job.owner_id, job.upload_id, job.kb_excluded)
            return
        await self._chunks.delete_source(job.owner_id, job.upload_id)
        if job.text is None:
            return  # removal — nothing to re-insert
        chunks = chunk_text(job.text)
        if not chunks:
            return
        # Stamp the upload's *current* exclusion onto the rebuilt chunks (read live, so a
        # reindex of an already-excluded file doesn't quietly re-admit it to the corpus).
        excluded = await self._is_excluded(job.owner_id, job.upload_id)
        _ = await self._chunks.upsert(
            job.owner_id,
            self.source_kind,
            job.upload_id,
            job.upload_id,
            chunks,
            kb_excluded=excluded,
        )
        _ = await self._chunks.reembed(job.owner_id, job.upload_id)

    async def _is_excluded(self, owner_id: str, upload_id: str) -> bool:
        """The upload row's authoritative ``kb_excluded`` (False if it's gone)."""

        def work(session: Session) -> bool:
            upload = session.get(Upload, upload_id)
            return bool(upload and upload.owner_id == owner_id and upload.kb_excluded)

        return await in_session(self._engine, work)
