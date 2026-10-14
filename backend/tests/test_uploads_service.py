"""The uploads capability: extraction lifecycle, dedup, correction, indexing, sealing."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlmodel import Session, select

from core.db import in_session, init_db, make_engine
from core.exceptions import NotFoundError
from core.vault import Vault, VaultLocked
from models.upload import Upload, UploadStatus
from services.corpus.chunk_store import CorpusChunkStore
from services.corpus.uploads import UploadsAdapter
from services.upload_extraction import BasicExtractor
from services.uploads import UploadStore

from .test_memory import FakeEmbedder
from .test_uploads_extraction import FakeVisionOCR, FlakyVisionOCR, NoVisionOCR, image_pdf, text_pdf

OWNER = "operator"


async def _store(ocr=None, *, extractor=None):
    """A started UploadStore + its corpus adapter over a throwaway in-memory DB."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    chunk_store = CorpusChunkStore(engine, vault, FakeEmbedder())
    adapter = UploadsAdapter(engine, chunk_store, vault.unlocked_event)
    await adapter.start()
    store = UploadStore(engine, vault, adapter, extractor or BasicExtractor(ocr or NoVisionOCR()))
    await store.start()
    return engine, vault, chunk_store, adapter, store


class _LockingExtractor:
    """Raises VaultLocked as if the vault locked mid-extraction (e.g. decrypting a
    vision endpoint's key) — a transient condition, not a bad file."""

    async def extract(self, owner_id, raw, mime, filename):
        raise VaultLocked("vault locked mid-extraction")


async def _drain(store, adapter):
    await store._worker.join()  # extraction completes (and submits the index job)
    await adapter._worker.join()  # the index job is drained


async def _teardown(store, adapter):
    await store.stop()
    await adapter.stop()


async def _retrieve(adapter, token):
    return await adapter.retrieve(OWNER, token, None, None, {token}, limit=5)


# --- extraction lifecycle + indexing (UP-2) --------------------------------


async def test_text_upload_is_extracted_indexed_and_labelled_basic():
    engine, _vault, _chunks, adapter, store = await _store()
    view, created = await store.create(
        OWNER, "n.txt", "text/plain", b"the operator likes zebra facts"
    )
    assert created is True
    await _drain(store, adapter)

    got = await store.get(OWNER, view.id)
    assert got.status == "done"
    assert got.extracted_text == "the operator likes zebra facts"
    assert got.extractor == "basic"
    hits = await _retrieve(adapter, "zebra")
    assert hits and hits[0].source_id == view.id
    await _teardown(store, adapter)


async def test_native_pdf_extracted():
    engine, _vault, _chunks, adapter, store = await _store()
    view, _ = await store.create(OWNER, "d.pdf", "application/pdf", text_pdf("pdf marker quokka"))
    await _drain(store, adapter)
    got = await store.get(OWNER, view.id)
    assert got.extracted_text is not None
    assert got.status == "done" and "pdf marker quokka" in got.extracted_text
    assert got.vision is False
    await _teardown(store, adapter)


async def test_scanned_pdf_uses_vision():
    engine, _vault, _chunks, adapter, store = await _store(FakeVisionOCR("scanned page words"))
    view, _ = await store.create(OWNER, "scan.pdf", "application/pdf", image_pdf())
    await _drain(store, adapter)
    got = await store.get(OWNER, view.id)
    assert got.status == "done" and got.vision is True
    assert got.extracted_text == "scanned page words"
    await _teardown(store, adapter)


async def test_scanned_pdf_without_vision_errors_with_reason():
    engine, _vault, _chunks, adapter, store = await _store(NoVisionOCR())
    view, _ = await store.create(OWNER, "scan.pdf", "application/pdf", image_pdf())
    await _drain(store, adapter)
    got = await store.get(OWNER, view.id)
    assert got.status == "error" and got.note is not None and "vision model" in got.note
    assert not got.extracted_text
    await _teardown(store, adapter)


async def test_retry_recovers_after_vision_configured():
    flaky = FlakyVisionOCR()
    engine, _vault, _chunks, adapter, store = await _store(flaky)
    view, _ = await store.create(OWNER, "scan.pdf", "application/pdf", image_pdf())
    await _drain(store, adapter)
    assert (await store.get(OWNER, view.id)).status == "error"

    flaky.enabled = True  # operator configures a vision model, then retries
    await store.retry(OWNER, view.id)
    await _drain(store, adapter)
    got = await store.get(OWNER, view.id)
    assert got.status == "done" and got.vision is True
    await _teardown(store, adapter)


# --- dedup (UP-1) ----------------------------------------------------------


async def test_duplicate_bytes_are_recognized():
    engine, _vault, _chunks, adapter, store = await _store()
    v1, c1 = await store.create(OWNER, "a.txt", "text/plain", b"identical bytes")
    v2, c2 = await store.create(OWNER, "b.txt", "text/plain", b"identical bytes")
    await _drain(store, adapter)
    assert c1 is True and c2 is False
    assert v1.id == v2.id
    assert await store.count(OWNER) == 1
    await _teardown(store, adapter)


# --- correction (UP-2) -----------------------------------------------------


async def test_correct_text_reindexes_and_marks_manual():
    engine, _vault, _chunks, adapter, store = await _store()
    view, _ = await store.create(OWNER, "n.txt", "text/plain", b"alpha original marker")
    await _drain(store, adapter)

    await store.correct_text(OWNER, view.id, "beta corrected marker")
    await adapter._worker.join()
    got = await store.get(OWNER, view.id)
    assert got.extracted_text == "beta corrected marker" and got.extractor == "manual"
    assert not await _retrieve(adapter, "alpha")  # stale text dropped
    assert await _retrieve(adapter, "beta")  # corrected text indexed
    await _teardown(store, adapter)


# --- delete + content + guard ----------------------------------------------


async def test_delete_drops_chunks():
    engine, _vault, chunk_store, adapter, store = await _store()
    view, _ = await store.create(OWNER, "n.txt", "text/plain", b"deletable zebra note")
    await _drain(store, adapter)
    await store.delete(OWNER, view.id)
    await adapter._worker.join()
    assert await chunk_store.count_items(OWNER, "uploads") == 0
    await _teardown(store, adapter)


async def test_content_roundtrips_original_bytes():
    engine, _vault, _chunks, adapter, store = await _store()
    view, _ = await store.create(OWNER, "f.bin", "application/octet-stream", b"\x00\x01raw")
    blob = await store.content(OWNER, view.id)
    assert blob.content == b"\x00\x01raw" and blob.filename == "f.bin"
    await _teardown(store, adapter)


async def test_owner_guard_raises_not_found():
    engine, _vault, _chunks, adapter, store = await _store()
    view, _ = await store.create(OWNER, "n.txt", "text/plain", b"x")
    with pytest.raises(NotFoundError):
        await store.get("intruder", view.id)
    await _teardown(store, adapter)


async def test_blob_and_filename_sealed_at_rest():
    engine, vault, _chunks, adapter, store = await _store()
    await store.create(OWNER, "secret.txt", "text/plain", b"the wifi password is hunter2")
    await _drain(store, adapter)

    with Session(engine) as session:
        row = session.exec(select(Upload)).one()
    assert b"hunter2" not in row.blob_enc
    assert "secret.txt" not in row.filename_enc
    assert vault.decrypt_bytes(row.blob_enc) == b"the wifi password is hunter2"
    await _teardown(store, adapter)


# --- crash recovery + lock-park (review fixes) -----------------------------


async def test_start_requeues_stranded_extractions():
    """A restart loses the in-memory job queue, so start() must re-queue unfinished
    uploads and reset any stranded ``extracting`` row — otherwise it sticks forever."""
    engine, vault, _chunks, adapter, store = await _store()
    view, _ = await store.create(OWNER, "n.txt", "text/plain", b"recoverable text body")
    await _drain(store, adapter)
    await store.stop()

    # Simulate a crash mid-extraction: the row is stuck and its job is gone.
    def strand(session: Session) -> None:
        row = session.get(Upload, view.id)
        assert row is not None
        row.status = UploadStatus.EXTRACTING
        session.add(row)

    await in_session(engine, strand)

    # A fresh store over the same engine recovers it on start.
    store2 = UploadStore(engine, vault, adapter, BasicExtractor(NoVisionOCR()))
    await store2.start()
    await _drain(store2, adapter)
    assert (await store2.get(OWNER, view.id)).status == "done"
    await store2.stop()
    await adapter.stop()


async def test_vault_lock_during_extraction_does_not_become_error():
    """A VaultLocked mid-extraction must park/retry, never be recorded as a permanent
    error — the lock-aware worker's guarantee."""
    engine, _vault, _chunks, adapter, store = await _store(extractor=_LockingExtractor())
    view, _ = await store.create(OWNER, "n.txt", "text/plain", b"content")
    await _drain(store, adapter)  # the worker retries then drops; status is untouched
    assert (await store.get(OWNER, view.id)).status != "error"
    await _teardown(store, adapter)
