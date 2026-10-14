"""The memory-only recall index: every searchable row, ranked without decrypting it again.

Recall used to decrypt and score the owner's whole history on every query. This index
decrypts each row once, after unlock, and keeps what ranking needs in memory: an inverted
keyword index (BM25) and one unit-vector matrix per embedding model. Nothing it builds
touches disk, and a vault lock drops all of it, so the plaintext-free-at-rest posture of
the sealed columns is unchanged.

**Kept current by diffing, not by write hooks.** Each collection reports a cheap aggregate
signature and, when that moves, a per-row stamp from cleartext columns (an embedding model,
an ``updated_at``, a flag). Only rows whose stamp changed are decrypted again. A write
path that forgets to tell the index about a change cannot leave it stale.

Text is not kept: postings and vectors are enough to rank, and a caller decrypts only the
rows it returns.
"""

from __future__ import annotations

import asyncio
import math
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import Engine
from sqlmodel import Session

from core.db import in_session
from core.vault import Vault
from services import ranking

_K1 = 1.2
_B = 0.75


@dataclass(frozen=True)
class Doc[M]:
    """One indexed row as ranking sees it. ``meta`` is the collection's own cleartext
    payload (a conversation id, a chunk's source) for filtering and for finding the row
    again."""

    id: str
    at: datetime | None
    meta: M


@dataclass(frozen=True)
class Loaded[M]:
    """A row as a collection hands it to the index: the doc, its plaintext, and its
    vector (if it has one in a known model)."""

    doc: Doc[M]
    text: str
    vector: list[float] | None = None
    model: str | None = None


@dataclass(frozen=True)
class Query:
    tokens: set[str]
    vector: np.ndarray | None
    model: str | None


@dataclass(frozen=True)
class Ranked[M]:
    doc: Doc[M]
    score: float
    matched_by: str
    dense: float | None = None
    sparse: float | None = None


class Collection[M](ABC):
    """One kind of searchable row."""

    name: str

    @abstractmethod
    def signature(self, session: Session, owner_id: str) -> Hashable:
        """A cheap aggregate that changes whenever any row's stamp would."""

    @abstractmethod
    def stamps(self, session: Session, owner_id: str) -> dict[str, Hashable]:
        """Every row id with a cleartext stamp that changes when the row needs reloading."""

    @abstractmethod
    def load(
        self, session: Session, vault: Vault, owner_id: str, ids: list[str]
    ) -> list[Loaded[M]]:
        """Decrypt the given rows."""


class _Vectors:
    """One embedding model's unit vectors, appended and removed in place."""

    def __init__(self, dim: int) -> None:
        self.dim: int = dim
        self.matrix: NDArray[np.float32] = np.zeros((16, dim), dtype=np.float32)
        self.ids: list[str | None] = []
        self.row: dict[str, int] = {}
        self.free: list[int] = []

    def put(self, doc_id: str, vector: NDArray[np.float32]) -> None:
        self.drop(doc_id)
        if self.free:
            slot = self.free.pop()
            self.ids[slot] = doc_id
        else:
            slot = len(self.ids)
            if slot == len(self.matrix):
                grown = np.zeros((len(self.matrix) * 2, self.dim), dtype=np.float32)
                grown[:slot] = self.matrix[:slot]
                self.matrix = grown
            self.ids.append(doc_id)
        self.matrix[slot] = vector
        self.row[doc_id] = slot

    def drop(self, doc_id: str) -> None:
        slot = self.row.pop(doc_id, None)
        if slot is not None:
            self.matrix[slot] = 0.0
            self.ids[slot] = None
            self.free.append(slot)

    def scores(self, query: NDArray[np.float32]) -> list[tuple[str, float]]:
        if not self.row:
            return []
        values = self.matrix[: len(self.ids)] @ query
        return [
            (doc_id, float(cast("np.float32", values[slot])))
            for slot, doc_id in enumerate(self.ids)
            if doc_id is not None and values[slot] > 0
        ]


@dataclass
class _Shard[M]:
    signature: Hashable = None
    stamps: dict[str, Hashable] = field(default_factory=dict)
    docs: dict[str, Doc[M]] = field(default_factory=dict)
    terms: dict[str, Counter[str]] = field(default_factory=dict)
    postings: dict[str, dict[str, int]] = field(default_factory=dict)
    total_length: int = 0
    vectors: dict[str, _Vectors] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def remove(self, doc_id: str) -> None:
        _ = self.stamps.pop(doc_id, None)
        _ = self.docs.pop(doc_id, None)
        counts = self.terms.pop(doc_id, None)
        if counts is not None:
            self.total_length -= counts.total()
            for term in counts:
                posting = self.postings[term]
                _ = posting.pop(doc_id, None)
                if not posting:
                    del self.postings[term]
        for table in self.vectors.values():
            table.drop(doc_id)

    def add(self, item: Loaded[M]) -> None:
        doc_id = item.doc.id
        self.docs[doc_id] = item.doc
        counts = ranking.term_counts(item.text)
        if counts:
            self.terms[doc_id] = counts
            self.total_length += counts.total()
            for term, count in counts.items():
                self.postings.setdefault(term, {})[doc_id] = count
        if item.vector is not None and item.model is not None:
            vector = np.asarray(item.vector, dtype=np.float32)
            norm = float(np.linalg.norm(vector))
            if norm:
                table = self.vectors.get(item.model)
                if table is None or table.dim != len(vector):
                    table = self.vectors[item.model] = _Vectors(len(vector))
                table.put(doc_id, vector / norm)

    def bm25(self, tokens: set[str]) -> dict[str, float]:
        count = len(self.terms)
        if not count:
            return {}
        average = self.total_length / count
        scores: dict[str, float] = {}
        for term in tokens:
            posting = self.postings.get(term)
            if not posting:
                continue
            idf = math.log(1 + (count - len(posting) + 0.5) / (len(posting) + 0.5))
            for doc_id, tf in posting.items():
                length = self.terms[doc_id].total()
                norm = tf + _K1 * (1 - _B + _B * length / average)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * tf * (_K1 + 1) / norm
        return scores

    def dense(self, vector: np.ndarray | None, model: str | None) -> dict[str, float]:
        if vector is None or model is None:
            return {}
        table = self.vectors.get(model)
        if table is None or table.dim != len(vector):
            return {}
        norm = float(np.linalg.norm(vector))
        if not norm:
            return {}
        return dict(table.scores((vector / norm).astype(np.float32)))


class RecallIndex:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._shards: dict[tuple[str, str], object] = {}
        self._generation: int = 0
        vault.register_on_lock(self.clear)

    def clear(self) -> None:
        self._shards.clear()
        self._generation += 1

    async def search[M](
        self,
        collection: Collection[M],
        owner_id: str,
        query: Query,
        *,
        limit: int,
        keep: Callable[[Doc[M]], bool] | None = None,
    ) -> list[Ranked[M]]:
        """The ``limit`` best docs by RRF over BM25 and cosine. A doc ``keep`` rejects
        is skipped before ranking, so a filter never shortens the result."""
        shard = await self._sync(collection, owner_id)
        sparse = shard.bm25(query.tokens)
        dense = shard.dense(query.vector, query.model)
        if keep is not None:
            sparse = {d: s for d, s in sparse.items() if keep(shard.docs[d])}
            dense = {d: s for d, s in dense.items() if keep(shard.docs[d])}
        fused = ranking.rrf(dense, sparse)
        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [
            Ranked(
                shard.docs[d],
                score,
                ranking.matched_by(d, dense, sparse),
                dense.get(d),
                sparse.get(d),
            )
            for d, score in ranked
        ]

    async def docs[M](self, collection: Collection[M], owner_id: str) -> list[Doc[M]]:
        shard = await self._sync(collection, owner_id)
        return list(shard.docs.values())

    async def _sync[M](self, collection: Collection[M], owner_id: str) -> _Shard[M]:
        key = (collection.name, owner_id)
        stored = self._shards.get(key)
        if stored is None:
            shard = _Shard[M]()
            self._shards[key] = shard
        else:
            shard = cast("_Shard[M]", stored)
        async with shard.lock:
            generation = self._generation
            fresh = await in_session(
                self._engine, lambda session: self._refresh(collection, owner_id, shard, session)
            )
            if fresh is not None and generation == self._generation:
                signature, stamps, changed, loaded = fresh
                for doc_id in (set(shard.stamps) - set(stamps)) | set(changed):
                    shard.remove(doc_id)
                for item in loaded:
                    shard.add(item)
                for doc_id in changed:
                    shard.stamps[doc_id] = stamps[doc_id]
                shard.signature = signature
        return shard

    def _refresh[M](
        self, collection: Collection[M], owner_id: str, shard: _Shard[M], session: Session
    ) -> tuple[Hashable, dict[str, Hashable], list[str], list[Loaded[M]]] | None:
        signature = collection.signature(session, owner_id)
        if shard.signature is not None and signature == shard.signature:
            return None
        stamps = collection.stamps(session, owner_id)
        changed = [
            doc_id
            for doc_id, stamp in stamps.items()
            if doc_id not in shard.stamps or shard.stamps[doc_id] != stamp
        ]
        loaded = collection.load(session, self._vault, owner_id, changed) if changed else []
        return signature, stamps, changed, loaded


def rank_texts(texts: dict[str, str], tokens: set[str]) -> list[tuple[str, float]]:
    """BM25 over a handful of in-memory texts (a catalog), best first."""
    shard: _Shard[None] = _Shard()
    for key, text in texts.items():
        shard.add(Loaded(Doc(key, None, None), text))
    return sorted(shard.bm25(tokens).items(), key=lambda kv: kv[1], reverse=True)
