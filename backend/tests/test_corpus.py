"""The corpus capability: chunk-store seal, folder crawl/idempotency, fused retrieve."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import init_db, make_engine
from core.exceptions import NotFoundError
from core.vault import Vault
from models.corpus import CorpusChunk
from services import ranking
from services.corpus.chunk_store import CorpusChunkStore, content_hash
from services.corpus.folder import FolderAdapter
from services.corpus.index import CorpusIndex
from services.corpus.wrappers import MemoryAdapter
from services.embeddings import Embedder
from services.memory import MemoryStore
from services.registry import ModelRegistry

from .test_memory import DegradedEmbedder, FakeEmbedder

OWNER = "operator"


def _index(embedder: Embedder, chunk_store: CorpusChunkStore, folder: FolderAdapter) -> CorpusIndex:
    """A CorpusIndex without a real registry (stats aren't under test here)."""
    return CorpusIndex(embedder, ModelRegistry.__new__(ModelRegistry), chunk_store, folder)


async def _engine_vault() -> tuple[Engine, Vault]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return engine, vault


# --- chunk store: encryption at rest --------------------------------------


async def test_chunk_text_and_vector_sealed_at_rest():
    embedder = FakeEmbedder()
    engine, vault = await _engine_vault()
    store = CorpusChunkStore(engine, vault, embedder)
    from services.chunking import chunk_text

    _ = await store.upsert(OWNER, "folder", "src-1", "/notes/a.txt", chunk_text("the cat is here"))
    _ = await store.reembed(OWNER, "src-1", current_model="fake-embed")

    with Session(engine) as session:
        row = session.exec(select(CorpusChunk)).one()
    assert "cat" not in row.text_enc  # text is sealed
    assert row.embedding_enc is not None and "1.0" not in row.embedding_enc  # vector sealed
    # And it round-trips through the vault.
    assert vault.decrypt_str(row.text_enc) == "the cat is here"


# --- folder adapter: crawl, idempotency, missing path ----------------------


async def _folder(
    embedder: Embedder,
) -> tuple[Engine, Vault, CorpusChunkStore, FolderAdapter]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    chunk_store = CorpusChunkStore(engine, vault, embedder)
    adapter = FolderAdapter(engine, chunk_store, vault.unlocked_event)
    return engine, vault, chunk_store, adapter


async def test_folder_crawls_chunks_and_embeds(tmp_path: Path):
    _ = (tmp_path / "a.txt").write_text("I have a pet cat at home")
    _ = (tmp_path / "b.md").write_text("My commute uses a car")
    _ = (tmp_path / "skip.bin").write_text("ignored binary")

    _engine, _vault, chunk_store, adapter = await _folder(FakeEmbedder())
    await adapter.start()
    source = await adapter.add_folder(OWNER, str(tmp_path))
    await adapter.flush()  # drain the queued crawl
    await adapter.stop()

    assert await chunk_store.count(OWNER, source.id) == 2  # two text files, binary skipped
    statuses = await adapter.status(OWNER)
    assert statuses[0].status == "indexed"


async def test_folder_reindex_is_idempotent(tmp_path: Path):
    _ = (tmp_path / "a.txt").write_text("a stable note about a dog")
    _engine, _vault, chunk_store, adapter = await _folder(FakeEmbedder())
    await adapter.start()
    source = await adapter.add_folder(OWNER, str(tmp_path))
    await adapter.flush()
    first = await chunk_store.count(OWNER, source.id)

    _ = await adapter.rebuild(OWNER, source.id)  # re-crawl identical content
    await adapter.flush()
    await adapter.stop()
    # No dup rows — content hash dedups the unchanged file.
    assert await chunk_store.count(OWNER, source.id) == first


async def test_missing_path_marks_error_status(tmp_path: Path):
    _engine, _vault, _chunk_store, adapter = await _folder(FakeEmbedder())
    await adapter.start()
    source = await adapter.add_folder(OWNER, str(tmp_path / "does-not-exist"))
    await adapter.flush()
    await adapter.stop()

    statuses = await adapter.status(OWNER)
    row = next(s for s in statuses if s.source_id == source.id)
    assert row.status == "error"
    assert row.error_hint == "PATH NOT FOUND"


# --- index: fan-out + RRF across sources -----------------------------------


async def test_retrieve_fuses_folder_and_memory(tmp_path: Path):
    _ = (tmp_path / "doc.txt").write_text("the feline sleeps all day")
    embedder = FakeEmbedder()
    engine, vault, chunk_store, folder = await _folder(embedder)
    await folder.start()
    src = await folder.add_folder(OWNER, str(tmp_path))
    await folder.flush()

    memory = MemoryStore(engine, vault, embedder)
    _ = await memory.remember(OWNER, "I drive a car to work")

    index = CorpusIndex(embedder, ModelRegistry.__new__(ModelRegistry), chunk_store, folder)
    index.register(folder)
    index.register(MemoryAdapter(memory))

    # "cat" embeds into the same concept slot as "feline" (the fake concept space),
    # so the folder hit surfaces by meaning across sources.
    hits = await index.retrieve(OWNER, "cat", limit=5)
    await folder.stop()
    assert hits, "the corpus should retrieve across folder + memory"
    assert any(h.source_id == src.id for h in hits)
    assert all(h.matched_by in ("semantic", "keyword", "both") for h in hits)


async def test_retrieve_degrades_to_sparse_without_embeddings(tmp_path: Path):
    _ = (tmp_path / "doc.txt").write_text("the wifi password is hunter2")
    _engine, _vault, chunk_store, folder = await _folder(DegradedEmbedder())
    await folder.start()
    _ = await folder.add_folder(OWNER, str(tmp_path))
    await folder.flush()

    index = CorpusIndex(
        DegradedEmbedder(), ModelRegistry.__new__(ModelRegistry), chunk_store, folder
    )
    index.register(folder)

    hits = await index.retrieve(OWNER, "wifi password", limit=5)
    await folder.stop()
    assert hits and hits[0].matched_by == "keyword"


def test_content_hash_is_stable():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


def test_rrf_fuses_cross_source_ranks():
    # The fusion primitive the index reuses is rank-based, so two sources' scores
    # combine without normalization — a sanity check the index relies on.
    fused = ranking.rrf({"a:1": 0.9, "b:1": 0.1}, {"b:1": 5.0})
    assert fused["b:1"] > 0  # surfaced by sparse despite a weak dense score
    assert np.isfinite(fused["a:1"])


# --- regression: cross-source fusion is rank-based, not raw-score ----------


async def test_strong_memory_hit_not_buried_under_folder_hits(tmp_path: Path):
    """A perfect semantic memory match must survive next to many folder hits whose
    raw cosines dwarf memory's (much smaller) fused score. The old fusion merged both
    sources into one score map and ranked by value, sinking the memory hit below every
    folder cosine; rank-based fusion gives each source's top hit equal standing."""
    embedder = FakeEmbedder()
    # Five folder docs that each match "cat" by both meaning and keyword (cosine ~1).
    for i in range(5):
        _ = (tmp_path / f"doc{i}.txt").write_text("the cat naps in the sun")
    engine, vault, chunk_store, folder = await _folder(embedder)
    await folder.start()
    _ = await folder.add_folder(OWNER, str(tmp_path))
    await folder.flush()

    # One memory that matches "cat" only by meaning (no literal "cat" token).
    memory = MemoryStore(engine, vault, embedder)
    _ = await memory.remember(OWNER, "a fluffy feline that purrs")

    index = _index(embedder, chunk_store, folder)
    index.register(folder)
    index.register(MemoryAdapter(memory))

    hits = await index.retrieve(OWNER, "cat", limit=3)
    # The memory hit lands in the top results rather than being buried by raw cosine.
    assert any(h.source_id == MemoryAdapter.SOURCE_ID for h in hits)


# --- regression: per-source reindex routing --------------------------------


async def test_reindex_unknown_source_raises():
    embedder = FakeEmbedder()
    _engine, _vault, chunk_store, folder = await _folder(embedder)
    index = _index(embedder, chunk_store, folder)
    index.register(folder)
    with pytest.raises(NotFoundError):
        _ = await index.reindex(OWNER, "no-such-source")


async def test_reindex_surface_routes_to_its_adapter():
    embedder = FakeEmbedder()
    engine, vault, chunk_store, folder = await _folder(embedder)
    memory = MemoryStore(engine, vault, embedder)
    index = _index(embedder, chunk_store, folder)
    index.register(folder)
    index.register(MemoryAdapter(memory))
    # A surface id routes to that adapter's reindex (memory re-embed) — no raise, no
    # folder fallback. Empty store ⇒ zero rows healed.
    assert await index.reindex(OWNER, MemoryAdapter.SOURCE_ID) == 0


# --- regression: EMB-2 segregation + heal for corpus chunks ----------------


async def test_chunks_segregate_then_reembed_heals_on_model_change():
    from services.chunking import chunk_text

    engine, vault, _chunk_store, _folder_adapter = await _folder(FakeEmbedder())
    store = CorpusChunkStore(engine, vault, FakeEmbedder(model="model-a"))
    _ = await store.upsert(OWNER, "folder", "src-1", "/n/a.txt", chunk_text("a fluffy feline"))
    _ = await store.reembed(OWNER, "src-1", current_model="model-a")

    # The operator switched embedding models; query vectors now live in model-b's space.
    batch = await FakeEmbedder(model="model-b").embed(OWNER, ["cat"])
    qvec, qtokens = np.asarray(batch.vectors[0]), {"cat"}

    store_b = CorpusChunkStore(engine, vault, FakeEmbedder(model="model-b"))
    before = await store_b.retrieve(OWNER, "folder", qvec, "model-b", qtokens, limit=5)
    assert all(h.matched_by != "semantic" for h in before)  # segregated: no dense match

    healed = await store_b.reembed(OWNER, "src-1", current_model="model-b")
    assert healed == 1
    after = await store_b.retrieve(OWNER, "folder", qvec, "model-b", qtokens, limit=5)
    assert any(h.dense_score is not None for h in after)  # meaning-recall restored


# --- regression: lock-aware indexing parks while the vault is locked --------


async def test_folder_index_parks_while_vault_locked(tmp_path: Path):
    _ = (tmp_path / "a.txt").write_text("a note about a dog")
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    chunk_store = CorpusChunkStore(engine, vault, FakeEmbedder())
    adapter = FolderAdapter(engine, chunk_store, vault.unlocked_event)

    # Register while unlocked — the source's own host path is sealed now (`XC-SEC-3`), so
    # registering needs the key. That's not a new constraint in practice: the only caller
    # is the /corpus route, and the auth gate in front of it implies an unlocked vault.
    # Then lock, and only then start the worker: the queued crawl is what must park.
    await vault.setup("pw")
    source = await adapter.add_folder(OWNER, str(tmp_path))  # sealed row + queued job
    vault.lock()

    await adapter.start()
    await asyncio.sleep(0.05)  # give the worker a chance — it should park, not index
    assert await chunk_store.count(OWNER, source.id) == 0  # nothing sealed while locked

    assert await vault.unlock("pw")  # unlock ⇒ the parked worker resumes
    await asyncio.wait_for(adapter.flush(), timeout=5)
    await adapter.stop()
    assert await chunk_store.count(OWNER, source.id) == 1


# --- regression: symlinked file escaping the tree is skipped ----------------


async def test_symlinked_file_outside_tree_is_skipped(tmp_path: Path):
    _ = (tmp_path / "real.txt").write_text("inside the tree about a cat")
    outside = Path(tempfile.mkdtemp()) / "secret.txt"
    _ = outside.write_text("outside secret about a dog")
    os.symlink(outside, tmp_path / "link.txt")  # escapes the indexed tree

    engine, vault, chunk_store, adapter = await _folder(FakeEmbedder())
    await adapter.start()
    source = await adapter.add_folder(OWNER, str(tmp_path))
    await adapter.flush()
    await adapter.stop()

    assert await chunk_store.count(OWNER, source.id) == 1  # only real.txt, not the symlink
    with Session(engine) as session:
        texts = [vault.decrypt_str(row.text_enc) for row in session.exec(select(CorpusChunk)).all()]
    assert any("inside the tree" in t for t in texts)  # the real file was indexed
    assert not any("secret" in t for t in texts)  # external content never ingested


# --- regression: a re-crawl reconciles, it does not only insert -------------


def _indexed(engine: Engine, vault: Vault) -> list[tuple[str, str]]:
    """Every chunk currently in the store as (external_ref, decrypted text). A list, not
    a dict: an edited file's old and new slices can share a ref (the offset only moves if
    the edit shifts the text), and collapsing them would hide the very duplication these
    tests are about."""
    with Session(engine) as session:
        rows = session.exec(select(CorpusChunk)).all()
    return [(row.external_ref, vault.decrypt_str(row.text_enc)) for row in rows]


async def test_recrawl_prunes_a_file_deleted_on_disk(tmp_path: Path):
    """A file removed from disk must stop being retrievable. The crawl is insert-only, so
    with no prune pass its chunks outlive it: RAG keeps quoting a file the operator
    deleted (a secrets file stays readable long after `rm`), and the folder's DOCS count
    still claims it."""
    _ = (tmp_path / "keep.txt").write_text("a stable note about a dog")
    secrets = tmp_path / "secrets.txt"
    _ = secrets.write_text("the launch code is hunter2")

    engine, vault, chunk_store, adapter = await _folder(FakeEmbedder())
    await adapter.start()
    source = await adapter.add_folder(OWNER, str(tmp_path))
    await adapter.flush()
    assert await chunk_store.count_items(OWNER, "folder", source.id) == 2

    secrets.unlink()
    _ = await adapter.rebuild(OWNER, source.id)
    await adapter.flush()
    await adapter.stop()

    indexed = _indexed(engine, vault)
    assert not any("hunter2" in text for _ref, text in indexed)
    assert not any(ref.startswith(str(secrets)) for ref, _text in indexed)
    assert any("about a dog" in text for _ref, text in indexed)  # the survivor is untouched
    assert await chunk_store.count_items(OWNER, "folder", source.id) == 1


async def test_recrawl_replaces_an_edited_files_chunks(tmp_path: Path):
    """An edit supersedes the old text. Insert-only, the store ends up holding both
    versions of the same passage — so recall can answer with the stale one and the
    operator has no way to tell which they got."""
    note = tmp_path / "wifi.txt"
    _ = note.write_text("the wifi password is hunter2")

    engine, vault, chunk_store, adapter = await _folder(FakeEmbedder())
    await adapter.start()
    source = await adapter.add_folder(OWNER, str(tmp_path))
    await adapter.flush()
    assert await chunk_store.count(OWNER, source.id) == 1

    _ = note.write_text("the wifi password is correcthorse")
    _ = await adapter.rebuild(OWNER, source.id)
    await adapter.flush()
    await adapter.stop()

    indexed = _indexed(engine, vault)
    assert len(indexed) == 1, "the superseded version must not sit alongside the new one"
    assert "correcthorse" in indexed[0][1]
    # Still one file, so the DOCS count is unchanged — a rewrite is not a new document.
    assert await chunk_store.count_items(OWNER, "folder", source.id) == 1


async def test_recrawl_keeps_a_file_it_could_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A file the crawler can see but cannot open is not a deleted file. Pruning on a
    permission blip would silently empty the index for content that still exists."""
    _ = (tmp_path / "notes.txt").write_text("the standup is at nine")

    _engine, _vault, chunk_store, adapter = await _folder(FakeEmbedder())
    await adapter.start()
    source = await adapter.add_folder(OWNER, str(tmp_path))
    await adapter.flush()
    assert await chunk_store.count(OWNER, source.id) == 1

    def _unreadable(_path: str) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(FolderAdapter, "_read", staticmethod(_unreadable))
    _ = await adapter.rebuild(OWNER, source.id)
    await adapter.flush()
    await adapter.stop()

    assert await chunk_store.count(OWNER, source.id) == 1


# --- regression: dedup is per file, not per source -------------------------


async def test_two_identical_files_are_both_indexed(tmp_path: Path):
    """Byte-identical files are still two files. Dedup keyed on content alone across the
    whole source (a repeated LICENSE, two copies of one config, two empty `__init__.py`)
    keeps only whichever path the crawl reached first: the other is invisible to search,
    and every hit on the shared text is cited against the wrong path."""
    (tmp_path / "env-a").mkdir()
    (tmp_path / "env-b").mkdir()
    shared = "retries = 3\nendpoint = 'https://example.invalid'"
    _ = (tmp_path / "env-a" / "config.toml").write_text(shared)
    _ = (tmp_path / "env-b" / "config.toml").write_text(shared)

    engine, vault, chunk_store, adapter = await _folder(FakeEmbedder())
    await adapter.start()
    source = await adapter.add_folder(OWNER, str(tmp_path))
    await adapter.flush()
    await adapter.stop()

    refs = {ref for ref, _text in _indexed(engine, vault)}
    assert len(refs) == 2, f"both copies should be indexed under their own paths: {refs}"
    assert any("env-a" in ref for ref in refs)
    assert any("env-b" in ref for ref in refs)
    assert await chunk_store.count_items(OWNER, "folder", source.id) == 2


async def test_dedup_scope_escapes_wildcards_in_a_path():
    """The per-item scope becomes a LIKE prefix, so it has to escape wildcards. Paths are
    full of `_`, which LIKE reads as "any one character", and `my-notes.txt` alongside
    `my_notes.txt` is an ordinary pair to have. Unescaped, the second one's dedup lookup
    matches the first one's rows and its content is silently never stored. Driven through
    the store directly, since the crawl's file order is the filesystem's to choose."""
    embedder = FakeEmbedder()
    engine, vault = await _engine_vault()
    store = CorpusChunkStore(engine, vault, embedder)
    from services.chunking import chunk_text

    chunks = chunk_text("the same two lines in both notes")
    assert await store.upsert(OWNER, "folder", "src-1", "/notes/my-notes.txt", chunks) == 1
    assert await store.upsert(OWNER, "folder", "src-1", "/notes/my_notes.txt", chunks) == 1
    assert await store.count_items(OWNER, "folder", "src-1") == 2


async def test_identical_files_still_dedup_within_themselves_across_recrawls(tmp_path: Path):
    """The per-file key must not cost idempotency: a second crawl of the same two
    identical files still inserts nothing."""
    _ = (tmp_path / "a.txt").write_text("the same words in both files")
    _ = (tmp_path / "b.txt").write_text("the same words in both files")

    _engine, _vault, chunk_store, adapter = await _folder(FakeEmbedder())
    await adapter.start()
    source = await adapter.add_folder(OWNER, str(tmp_path))
    await adapter.flush()
    first = await chunk_store.count(OWNER, source.id)

    _ = await adapter.rebuild(OWNER, source.id)
    await adapter.flush()
    await adapter.stop()

    assert first == 2
    assert await chunk_store.count(OWNER, source.id) == first
