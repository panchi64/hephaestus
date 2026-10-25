"""Corpus sources over what the platform itself wrote: thread work summaries and sub-agent
reports. A feature adds its own through :class:`RecordAdapter`.

Each is a short sealed text on an existing row, so each is a :class:`RecallCollection`
over that row ranked by BM25 alone. They carry no embeddings; adding them would mean a
vector column and an embed-on-write path for every one of them.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime
from typing import override

import numpy as np
from sqlalchemy import ColumnElement, Engine, func
from sqlmodel import Session, col, select

from core.db import in_session
from core.vault import Vault
from models.conversation import Conversation
from models.subagent import SubagentRecord
from services.conversation_search import snippet
from services.corpus.adapter import CorpusDocument, CorpusHit, SourceAdapter, SourceStatus
from services.recall_index import Collection, Doc, Loaded, Query, RecallIndex
from services.sealing import open_sealed

#: A record's full text by id, or None when it is not the owner's.
type ReadText = Callable[[Session, Vault, str, str], str | None]


@dataclass(frozen=True)
class RecordMeta:
    title: str
    #: Hidden from search (an ephemeral thread's summary), still readable by ref.
    hidden: bool = False


class SummaryCollection(Collection[RecordMeta]):
    name: str = "work-summaries"

    @staticmethod
    def _owned(owner_id: str) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
        return (
            col(Conversation.owner_id) == owner_id,
            col(Conversation.work_summary_enc).is_not(None),
        )

    @override
    def signature(self, session: Session, owner_id: str) -> Hashable:
        stmt = (
            select(Conversation.ephemeral, func.count(), func.max(Conversation.work_summary_at))
            .where(*self._owned(owner_id))
            .group_by(col(Conversation.ephemeral))
        )
        return frozenset(session.exec(stmt).all())

    @override
    def stamps(self, session: Session, owner_id: str) -> dict[str, Hashable]:
        stmt = select(Conversation.id, Conversation.work_summary_at, Conversation.ephemeral).where(
            *self._owned(owner_id)
        )
        return {cid: (at, ephemeral) for cid, at, ephemeral in session.exec(stmt).all()}

    @override
    def load(
        self, session: Session, vault: Vault, owner_id: str, ids: list[str]
    ) -> list[Loaded[RecordMeta]]:
        rows = session.exec(select(Conversation).where(col(Conversation.id).in_(ids))).all()
        return [
            Loaded(
                Doc(
                    row.id,
                    row.work_summary_at,
                    RecordMeta(
                        open_sealed(vault, row.title_enc, row.title) or "(untitled conversation)",
                        row.ephemeral,
                    ),
                ),
                vault.decrypt_str(row.work_summary_enc),
            )
            for row in rows
            if row.work_summary_enc is not None
        ]


class ReportCollection(Collection[RecordMeta]):
    name: str = "agent-reports"

    @staticmethod
    def _owned(owner_id: str) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
        return (
            col(SubagentRecord.owner_id) == owner_id,
            col(SubagentRecord.summary_enc).is_not(None),
        )

    @override
    def signature(self, session: Session, owner_id: str) -> Hashable:
        stmt = select(func.count(), func.max(SubagentRecord.ended_at)).where(*self._owned(owner_id))
        return tuple(session.exec(stmt).one())

    @override
    def stamps(self, session: Session, owner_id: str) -> dict[str, Hashable]:
        stmt = select(SubagentRecord.id, SubagentRecord.ended_at).where(*self._owned(owner_id))
        return dict(session.exec(stmt).all())

    @override
    def load(
        self, session: Session, vault: Vault, owner_id: str, ids: list[str]
    ) -> list[Loaded[RecordMeta]]:
        rows = session.exec(select(SubagentRecord).where(col(SubagentRecord.id).in_(ids))).all()
        return [
            Loaded(
                Doc(row.id, row.ended_at, RecordMeta(f"{row.handle} ({row.spec_name})")),
                f"{vault.decrypt_str(row.task_enc)}\n\n{vault.decrypt_str(row.summary_enc)}",
            )
            for row in rows
            if row.summary_enc is not None
        ]


class RecordAdapter(SourceAdapter):
    """A corpus source over one :class:`RecordMeta` collection."""

    def __init__(
        self,
        engine: Engine,
        vault: Vault,
        collection: Collection[RecordMeta],
        *,
        source_kind: str,
        source_id: str,
        label: str,
        read_text: ReadText,
    ) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._collection: Collection[RecordMeta] = collection
        self._index: RecallIndex = RecallIndex(engine, vault)
        self._label: str = label
        self._read_text: ReadText = read_text
        self.source_kind: str = source_kind
        self.SOURCE_ID: str = source_id

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
        ranked = await self._index.search(
            self._collection,
            owner_id,
            Query(query_tokens, None, None),
            limit=limit,
            keep=lambda doc: (
                not doc.meta.hidden and (since is None or (doc.at is not None and doc.at >= since))
            ),
        )
        texts = await in_session(
            self._engine,
            lambda session: {
                hit.doc.id: self._read_text(session, self._vault, owner_id, hit.doc.id)
                for hit in ranked
            },
        )
        return [
            CorpusHit(
                gid=f"{self.SOURCE_ID}:{hit.doc.id}",
                source_id=self.SOURCE_ID,
                ref=hit.doc.id,
                text=snippet(texts[hit.doc.id] or "", query_tokens),
                score=hit.score,
                matched_by=hit.matched_by,
                at=hit.doc.at,
                title=hit.doc.meta.title,
            )
            for hit in ranked
            if texts.get(hit.doc.id)
        ]

    @override
    async def read(self, owner_id: str, ref: str) -> CorpusDocument | None:
        docs = {doc.id: doc for doc in await self._index.docs(self._collection, owner_id)}
        doc = docs.get(ref)
        if doc is None:
            return None
        text = await in_session(
            self._engine, lambda session: self._read_text(session, self._vault, owner_id, ref)
        )
        if text is None:
            return None
        return CorpusDocument(title=doc.meta.title, text=text, at=doc.at)

    @override
    async def status(self, owner_id: str) -> SourceStatus:
        docs = await self._index.docs(self._collection, owner_id)
        return SourceStatus(
            source_id=self.SOURCE_ID,
            kind="surface",
            label=self._label,
            doc_count=len(docs),
            status="indexed",
            last_indexed_at=None,
        )


def _summary_text(session: Session, vault: Vault, owner_id: str, ref: str) -> str | None:
    row = session.get(Conversation, ref)
    if row is None or row.owner_id != owner_id or row.work_summary_enc is None:
        return None
    return vault.decrypt_str(row.work_summary_enc)


def _report_text(session: Session, vault: Vault, owner_id: str, ref: str) -> str | None:
    row = session.get(SubagentRecord, ref)
    if row is None or row.owner_id != owner_id or row.summary_enc is None:
        return None
    return (
        f"Task: {vault.decrypt_str(row.task_enc)}\n\nReport:\n{vault.decrypt_str(row.summary_enc)}"
    )


def record_adapters(engine: Engine, vault: Vault) -> list[RecordAdapter]:
    return [
        RecordAdapter(
            engine,
            vault,
            SummaryCollection(),
            source_kind="summaries",
            source_id="surf-summaries",
            label="Work summaries",
            read_text=_summary_text,
        ),
        RecordAdapter(
            engine,
            vault,
            ReportCollection(),
            source_kind="reports",
            source_id="surf-reports",
            label="Agent reports",
            read_text=_report_text,
        ),
    ]
