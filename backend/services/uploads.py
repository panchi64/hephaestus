"""Uploads — accept files, extract their text, and make both retrievable.

The capability behind `UP-1`/`UP-2`. An upload is encrypted at rest (its bytes,
filename, and extracted text); the operator owns it, so reads come back decrypted.
What the store keeps true:

- **Duplicates are recognized** (`UP-1`). A file's content ``sha256`` is the dedup
  key: re-uploading identical bytes returns the existing upload instead of storing a
  second copy. ``create`` reports which happened so the route can say so.
- **Text is extracted off the request path** (`UP-2`). A new upload is born ``queued``;
  a lock-aware :class:`~core.worker.WriteBehindWorker` drains it — decrypt the bytes,
  run the :class:`~services.upload_extraction.UploadExtractor` (native PDF text, vision
  OCR for scanned pages), seal the result — moving it ``extracting`` → ``done``/``error``.
  The worker seals content, so it parks while the vault is locked.
- **Extracted text is corpus content and correctable.** After a successful extraction
  (and after any operator correction) the store hands the text to the
  :class:`~services.corpus.uploads.UploadsAdapter` to (re)index, so the agent retrieves
  it through ``corpus.retrieve``. Deleting an upload drops its chunks.

Like the document store, *search* is not a method here — it flows through the corpus's
hybrid recall, not a hand-rolled scan over decrypted text.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from core.db import get_owned, in_session
from core.exceptions import NotFoundError
from core.vault import Vault, VaultLocked
from core.worker import WriteBehindWorker
from models.upload import Upload, UploadStatus
from services.corpus.uploads import UploadsAdapter
from services.upload_extraction import UploadExtractor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractJob:
    """A queued extraction of one upload, re-reading the row fresh on each attempt so a
    retry (after a park or a manual retry) always sees current state."""

    owner_id: str
    upload_id: str


@dataclass(frozen=True)
class UploadView:
    """A decrypted upload for the detail view — includes the full extracted text."""

    id: str
    filename: str
    mime: str
    size_bytes: int
    status: str
    vision: bool
    extractor: str | None
    extracted_text: str | None
    note: str | None
    kb_excluded: bool
    favorite: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class UploadSummaryView:
    """A library-list row: built entirely from clear columns, so listing the library
    never decrypts a single upload's text."""

    id: str
    filename: str
    mime: str
    size_bytes: int
    status: str
    vision: bool
    extractor: str | None
    has_text: bool
    note: str | None
    kb_excluded: bool
    favorite: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class UploadHead:
    """Clear metadata for serving decisions — mime, content digest, byte size — with no
    decryption. Lets the image/thumbnail routes set a content-addressed ETag and answer a
    conditional request (304) without ever unsealing the file bytes."""

    mime: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class UploadBlob:
    """A decrypted upload's original bytes, ready to serve for download/export."""

    filename: str
    mime: str
    content: bytes


class UploadStore:
    def __init__(
        self,
        engine: Engine,
        vault: Vault,
        adapter: UploadsAdapter,
        extractor: UploadExtractor,
    ) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._adapter: UploadsAdapter = adapter
        self._extractor: UploadExtractor = extractor
        # Extraction seals content (the extracted text) and resolves a vision model
        # whose key is sealed, so it must park while the vault is locked.
        self._worker: WriteBehindWorker[ExtractJob] = WriteBehindWorker(
            self._run_extraction, name="upload-extract", unlocked=vault.unlocked_event
        )

    async def start(self) -> None:
        await self._worker.start()
        await self._requeue_pending()

    async def stop(self) -> None:
        await self._worker.stop()

    async def flush(self) -> None:
        """Wait until the extraction queue has drained."""
        await self._worker.join()

    async def _requeue_pending(self) -> None:
        """Crash recovery for the extraction lifecycle: the job queue is in-memory, so a
        restart loses any pending work and can strand a row mid-``extracting``. On start,
        reset ``extracting`` rows back to ``queued`` and re-submit every unfinished
        upload — the extraction handler is idempotent, so a partly-run job re-runs
        cleanly. Status is clear metadata, so this reads/writes nothing sealed and is
        safe before the vault is unlocked (the worker itself parks until then)."""

        def work(session: Session) -> list[ExtractJob]:
            rows = session.exec(
                select(Upload).where(
                    col(Upload.status).in_([UploadStatus.QUEUED, UploadStatus.EXTRACTING])
                )
            ).all()
            jobs: list[ExtractJob] = []
            for row in rows:
                if row.status == UploadStatus.EXTRACTING:
                    row.status = UploadStatus.QUEUED
                    session.add(row)
                jobs.append(ExtractJob(row.owner_id, row.id))
            return jobs

        for job in await in_session(self._engine, work):
            self._worker.submit(job)

    # --- write path -------------------------------------------------------

    async def create(
        self, owner_id: str, filename: str, mime: str, content: bytes
    ) -> tuple[UploadView, bool]:
        """Store a file and queue its text extraction. Recognizes duplicates (`UP-1`):
        identical bytes return the existing upload untouched. Returns the upload and
        whether it was newly created (False ⇒ a duplicate of an existing one)."""
        digest = hashlib.sha256(content).hexdigest()
        existing = await self._find_by_hash(owner_id, digest)
        if existing is not None:
            return existing, False

        upload = Upload(
            owner_id=owner_id,
            filename_enc=self._vault.encrypt_str(filename),
            mime=mime,
            size_bytes=len(content),
            sha256=digest,
            blob_enc=self._vault.encrypt_bytes(content),
            status=UploadStatus.QUEUED,
        )

        def work(session: Session) -> UploadView:
            session.add(upload)
            session.flush()
            return self._to_view(upload, filename, None)

        try:
            view = await in_session(self._engine, work)
        except IntegrityError:
            # A concurrent upload of identical bytes won the owner+sha256 unique
            # constraint race — recognize it as the duplicate it is (UP-1) rather than
            # erroring. The dedup pre-check above handles the common case; this closes
            # the check-then-insert window for simultaneous identical uploads.
            existing = await self._find_by_hash(owner_id, digest)
            if existing is not None:
                return existing, False
            raise
        self._worker.submit(ExtractJob(owner_id, view.id))
        return view, True

    async def correct_text(self, owner_id: str, upload_id: str, text: str) -> UploadView:
        """Replace an upload's extracted text with an operator correction (`UP-2`) and
        re-index it. Clears any extraction note and marks the upload done."""
        await self._require(owner_id, upload_id)
        text_enc = self._vault.encrypt_str(text)

        def work(session: Session) -> UploadView:
            upload = session.get(Upload, upload_id)
            assert upload is not None
            upload.extracted_text_enc = text_enc
            upload.has_text = bool(text)
            upload.status = UploadStatus.DONE
            upload.extractor = "manual"  # operator-edited; not a fallback candidate
            upload.note = None
            upload.updated_at = datetime.now(UTC)
            session.add(upload)
            session.flush()
            return self._to_view(upload, self._vault.decrypt_str(upload.filename_enc), text)

        view = await in_session(self._engine, work)
        self._adapter.index_upload(owner_id, upload_id, text)
        return view

    async def set_kb_excluded(self, owner_id: str, upload_id: str, value: bool) -> UploadView:
        """Toggle whether this upload is part of the knowledge base — retroactively. Flips
        the authoritative flag on the row and restamps its corpus chunks, so it's filtered
        out of (``True``) or back into (``False``) every ``corpus.retrieve``. The bytes,
        extracted text, and the chunks themselves stay put, so it's a cheap, reversible
        scope change — not a delete."""

        def work(session: Session) -> UploadView:
            # Ownership check folded into the one write (no separate _require round-trip).
            upload = session.get(Upload, upload_id)
            if upload is None or upload.owner_id != owner_id:
                raise NotFoundError(f"upload {upload_id!r} not found")
            upload.kb_excluded = value
            upload.updated_at = datetime.now(UTC)
            session.add(upload)
            session.flush()
            return self._view_from_row(upload)

        view = await in_session(self._engine, work)
        self._adapter.set_excluded(owner_id, upload_id, value)
        return view

    async def set_favorite(self, owner_id: str, upload_id: str, value: bool) -> UploadView:
        """Toggle the operator's star on an image. A clear-column flip, like
        ``set_kb_excluded`` but with no corpus restamp — favorite is a UI affordance, not a
        knowledge-base property, so it touches the row alone."""

        def work(session: Session) -> UploadView:
            upload = session.get(Upload, upload_id)
            if upload is None or upload.owner_id != owner_id:
                raise NotFoundError(f"upload {upload_id!r} not found")
            upload.favorite = value
            upload.updated_at = datetime.now(UTC)
            session.add(upload)
            session.flush()
            return self._view_from_row(upload)

        return await in_session(self._engine, work)

    async def retry(self, owner_id: str, upload_id: str) -> UploadView:
        """Re-queue extraction for an upload (e.g. after configuring a vision model for a
        scanned PDF). Resets it to queued and clears the prior note."""
        await self._require(owner_id, upload_id)

        def work(session: Session) -> UploadView:
            upload = session.get(Upload, upload_id)
            assert upload is not None
            upload.status = UploadStatus.QUEUED
            upload.note = None
            upload.updated_at = datetime.now(UTC)
            session.add(upload)
            session.flush()
            return self._view_from_row(upload)

        view = await in_session(self._engine, work)
        self._worker.submit(ExtractJob(owner_id, upload_id))
        return view

    async def delete(self, owner_id: str, upload_id: str) -> None:
        """Hard-delete an upload (bytes + extracted text) and drop its corpus chunks."""
        await self._require(owner_id, upload_id)

        def work(session: Session) -> None:
            upload = session.get(Upload, upload_id)
            if upload is not None:
                session.delete(upload)

        await in_session(self._engine, work)
        self._adapter.remove_upload(owner_id, upload_id)

    # --- read path --------------------------------------------------------

    async def list_uploads(self, owner_id: str) -> list[UploadSummaryView]:
        """The library, newest first — summaries only (no full extracted text)."""

        def work(session: Session) -> list[UploadSummaryView]:
            rows = session.exec(
                select(Upload)
                .where(Upload.owner_id == owner_id)
                .order_by(col(Upload.created_at).desc())
            ).all()
            return [self._to_summary(row) for row in rows]

        return await in_session(self._engine, work)

    async def get(self, owner_id: str, upload_id: str) -> UploadView:
        def work(session: Session) -> UploadView:
            upload = session.get(Upload, upload_id)
            if upload is None or upload.owner_id != owner_id:
                raise NotFoundError(f"upload {upload_id!r} not found")
            return self._view_from_row(upload)

        return await in_session(self._engine, work)

    async def get_many(self, owner_id: str, ids: list[str]) -> dict[str, UploadView]:
        """The owner's uploads among ``ids``, decrypted, keyed by id — one query for the
        whole set. Ids that aren't the owner's (or don't exist) are simply absent, so the
        caller drops them the same way it would drop a ``NotFoundError`` from ``get``.

        A turn's attachments are resolved through this rather than through ``get`` in a
        loop: each ``in_session`` is a thread hop, and a four-file turn was paying four of
        them for what is one ``IN`` query."""
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return {}

        def work(session: Session) -> dict[str, UploadView]:
            rows = session.exec(
                select(Upload).where(
                    Upload.owner_id == owner_id,
                    col(Upload.id).in_(wanted),
                )
            ).all()
            return {row.id: self._view_from_row(row) for row in rows}

        return await in_session(self._engine, work)

    async def contents(self, owner_id: str, ids: list[str]) -> dict[str, UploadBlob]:
        """The decrypted bytes of the owner's uploads among ``ids``, keyed by id.

        The batch form of :meth:`content`, for the one caller that needs several at once
        (a turn attaching multiple images). Unlike :meth:`get_many` this materializes every
        blob, so it is only for a set the caller has already narrowed."""
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return {}

        def work(session: Session) -> dict[str, UploadBlob]:
            rows = session.exec(
                select(Upload).where(
                    Upload.owner_id == owner_id,
                    col(Upload.id).in_(wanted),
                )
            ).all()
            return {
                row.id: UploadBlob(
                    filename=self._vault.decrypt_str(row.filename_enc),
                    mime=row.mime,
                    content=self._vault.decrypt_bytes(row.blob_enc),
                )
                for row in rows
            }

        return await in_session(self._engine, work)

    async def content(self, owner_id: str, upload_id: str) -> UploadBlob:
        """The original file bytes, decrypted — for download/export."""

        def work(session: Session) -> UploadBlob:
            upload = session.get(Upload, upload_id)
            if upload is None or upload.owner_id != owner_id:
                raise NotFoundError(f"upload {upload_id!r} not found")
            return UploadBlob(
                filename=self._vault.decrypt_str(upload.filename_enc),
                mime=upload.mime,
                content=self._vault.decrypt_bytes(upload.blob_enc),
            )

        return await in_session(self._engine, work)

    async def head(self, owner_id: str, upload_id: str) -> UploadHead | None:
        """Clear serving metadata (mime, content digest, byte size) for an upload, or None
        if it isn't this owner's. Decrypts nothing — the image/thumbnail routes use it to
        ETag a response and answer a conditional request without unsealing the bytes."""

        def work(session: Session) -> UploadHead | None:
            upload = session.get(Upload, upload_id)
            if upload is None or upload.owner_id != owner_id:
                return None
            return UploadHead(mime=upload.mime, sha256=upload.sha256, size_bytes=upload.size_bytes)

        return await in_session(self._engine, work)

    async def image_ids(self, owner_id: str, ids: list[str]) -> list[str]:
        """Of ``ids``, those that name this owner's image files (``image/*``). A cheap
        clear-column filter that decrypts nothing — the delete-choice flow uses it to keep
        the keep/purge prompt to images, and to purge only images."""
        wanted = list(dict.fromkeys(ids))  # de-dupe, preserve order
        if not wanted:
            return []

        def work(session: Session) -> list[str]:
            rows = session.exec(
                select(Upload.id).where(
                    Upload.owner_id == owner_id,
                    col(Upload.id).in_(wanted),
                    col(Upload.mime).startswith("image/"),
                )
            ).all()
            return list(rows)

        return await in_session(self._engine, work)

    async def count(self, owner_id: str) -> int:
        """How many uploads the owner has (a readout; never decrypts)."""

        def work(session: Session) -> int:
            return session.exec(
                select(func.count()).select_from(Upload).where(Upload.owner_id == owner_id)
            ).one()

        return await in_session(self._engine, work)

    # --- extraction (worker handler) --------------------------------------

    async def _run_extraction(self, job: ExtractJob) -> None:
        """Drain one extraction: decrypt the bytes, extract text, seal and persist the
        outcome, and index any text. Content problems (a scanned page with no vision
        model) end as an ``error`` status with a reason; a hard parse failure does too.
        A vault lock anywhere in the pass parks the worker rather than recording a
        spurious error — the initial decrypt and the final seal sit outside the try, and
        a lock *inside* extraction (e.g. decrypting a vision endpoint's key) re-raises so
        the lock-aware worker parks and retries on unlock."""
        loaded = await self._load_for_extraction(job)
        if loaded is None:
            return  # deleted before extraction ran — nothing to do
        filename, mime, raw = loaded
        await self._set_status(job, UploadStatus.EXTRACTING)
        try:
            result = await self._extractor.extract(job.owner_id, raw, mime, filename)
        except VaultLocked:
            raise  # not a failure — let the worker park and retry once unlocked
        except Exception:  # a bad file mustn't kill the worker
            logger.exception("extraction failed for upload %s", job.upload_id)
            await self._finish(job, status=UploadStatus.ERROR, note="could not read the file")
            return

        # Done when we got text, or when the file simply had none to give; error only
        # when there was content to extract but a reason it couldn't be (e.g. no vision
        # model). The extractor/vision provenance rides through on every outcome.
        status = UploadStatus.DONE if (result.text or not result.note) else UploadStatus.ERROR
        await self._finish(
            job,
            status=status,
            text=result.text or None,
            vision=result.vision,
            note=result.note,
            extractor=result.extractor,
        )
        if result.text:
            self._adapter.index_upload(job.owner_id, job.upload_id, result.text)

    async def _load_for_extraction(self, job: ExtractJob) -> tuple[str, str, bytes] | None:
        def work(session: Session) -> tuple[str, str, bytes] | None:
            upload = session.get(Upload, job.upload_id)
            if upload is None or upload.owner_id != job.owner_id:
                return None
            return (
                self._vault.decrypt_str(upload.filename_enc),
                upload.mime,
                self._vault.decrypt_bytes(upload.blob_enc),
            )

        return await in_session(self._engine, work)

    async def _set_status(self, job: ExtractJob, status: str) -> None:
        def work(session: Session) -> None:
            upload = session.get(Upload, job.upload_id)
            if upload is None:
                return
            upload.status = status
            upload.updated_at = datetime.now(UTC)
            session.add(upload)

        await in_session(self._engine, work)

    async def _finish(
        self,
        job: ExtractJob,
        *,
        status: str,
        text: str | None = None,
        vision: bool = False,
        note: str | None = None,
        extractor: str | None = None,
    ) -> None:
        text_enc = self._vault.encrypt_str(text) if text is not None else None

        def work(session: Session) -> None:
            upload = session.get(Upload, job.upload_id)
            if upload is None:
                return
            upload.status = status
            upload.vision = vision
            upload.note = note
            if text is not None:
                upload.extracted_text_enc = text_enc
                upload.has_text = True
            if extractor is not None:
                upload.extractor = extractor
            upload.updated_at = datetime.now(UTC)
            session.add(upload)

        await in_session(self._engine, work)

    # --- internals --------------------------------------------------------

    async def _find_by_hash(self, owner_id: str, digest: str) -> UploadView | None:
        def work(session: Session) -> UploadView | None:
            upload = session.exec(
                select(Upload).where(Upload.owner_id == owner_id, Upload.sha256 == digest)
            ).first()
            return self._view_from_row(upload) if upload is not None else None

        return await in_session(self._engine, work)

    async def owned_ids(self, owner_id: str, ids: list[str]) -> set[str]:
        """Which of ``ids`` name uploads this owner has — the batch form of :meth:`owns`,
        decrypting nothing. The chat route validates a turn's whole attachment list with
        one query rather than one per id."""
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return set()

        def work(session: Session) -> set[str]:
            rows = session.exec(
                select(Upload.id).where(
                    Upload.owner_id == owner_id,
                    col(Upload.id).in_(wanted),
                )
            ).all()
            return set(rows)

        return await in_session(self._engine, work)

    async def _require(self, owner_id: str, upload_id: str) -> None:
        _ = await get_owned(self._engine, Upload, upload_id, owner_id, what="upload")

    def _view_from_row(self, upload: Upload) -> UploadView:
        text = (
            self._vault.decrypt_str(upload.extracted_text_enc)
            if upload.extracted_text_enc is not None
            else None
        )
        return self._to_view(upload, self._vault.decrypt_str(upload.filename_enc), text)

    def _to_summary(self, upload: Upload) -> UploadSummaryView:
        # Filename is sealed, so it's the one thing decrypted here; the rest are clear
        # columns. The full extracted text is never touched for a list row (the cheap
        # `has_text` flag stands in for it).
        return UploadSummaryView(
            id=upload.id,
            filename=self._vault.decrypt_str(upload.filename_enc),
            mime=upload.mime,
            size_bytes=upload.size_bytes,
            status=upload.status,
            vision=upload.vision,
            extractor=upload.extractor,
            has_text=upload.has_text,
            note=upload.note,
            kb_excluded=upload.kb_excluded,
            favorite=upload.favorite,
            created_at=upload.created_at,
            updated_at=upload.updated_at,
        )

    @staticmethod
    def _to_view(upload: Upload, filename: str, text: str | None) -> UploadView:
        return UploadView(
            id=upload.id,
            filename=filename,
            mime=upload.mime,
            size_bytes=upload.size_bytes,
            status=upload.status,
            vision=upload.vision,
            extractor=upload.extractor,
            extracted_text=text,
            note=upload.note,
            kb_excluded=upload.kb_excluded,
            favorite=upload.favorite,
            created_at=upload.created_at,
            updated_at=upload.updated_at,
        )
