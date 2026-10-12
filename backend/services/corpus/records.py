"""Corpus sources over what the platform itself wrote: thread work summaries, sub-agent
reports and routine run outputs.

Each is a short sealed text on an existing row, so each is a :class:`RecallCollection`
over that row ranked by BM25 alone. They carry no embeddings; adding them would mean a
vector column and an embed-on-write path for every one of them.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from sqlalchemy import Engine, func
from sqlmodel import Session, col, select

from core.db import in_session
from core.vault import Vault
from models.conversation import Conversation
from models.subagent import SubagentRecord
from models.task import ScheduledTask, TaskRun
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
    name = "work-summaries"

    @staticmethod
    def _owned(stmt, owner_id: str):
        return stmt.where(Conversation.owner_id == owner_id).where(
            col(Conversation.work_summary_enc).is_not(None)
        )

    def signature(self, session: Session, owner_id: str) -> Hashable:
        stmt = self._owned(
            select(Conversation.ephemeral, func.count(), func.max(Conversation.work_summary_at)),
            owner_id,
        ).group_by(col(Conversation.ephemeral))
        return frozenset(session.exec(stmt).all())

    def stamps(self, session: Session, owner_id: str) -> dict[str, Hashable]:
        stmt = self._owned(
            select(Conversation.id, Conversation.work_summary_at, Conversation.ephemeral),
            owner_id,
        )
        return {cid: (at, ephemeral) for cid, at, ephemeral in session.exec(stmt).all()}

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
    name = "agent-reports"

    @staticmethod
    def _owned(stmt, owner_id: str):
        return stmt.where(SubagentRecord.owner_id == owner_id).where(
            col(SubagentRecord.summary_enc).is_not(None)
        )

    def signature(self, session: Session, owner_id: str) -> Hashable:
        stmt = self._owned(select(func.count(), func.max(SubagentRecord.ended_at)), owner_id)
        return tuple(session.exec(stmt).one())

    def stamps(self, session: Session, owner_id: str) -> dict[str, Hashable]:
        stmt = self._owned(select(SubagentRecord.id, SubagentRecord.ended_at), owner_id)
        return dict(session.exec(stmt).all())

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


class RoutineRunCollection(Collection[RecordMeta]):
    name = "routine-runs"

    @staticmethod
    def _owned(stmt, owner_id: str):
        return (
            stmt.join(ScheduledTask, col(TaskRun.task_id) == col(ScheduledTask.id))
            .where(ScheduledTask.owner_id == owner_id)
            .where(col(TaskRun.summary_enc).is_not(None))
        )

    def signature(self, session: Session, owner_id: str) -> Hashable:
        stmt = self._owned(select(func.count(), func.max(TaskRun.finished_at)), owner_id)
        return tuple(session.exec(stmt).one())

    def stamps(self, session: Session, owner_id: str) -> dict[str, Hashable]:
        stmt = self._owned(select(TaskRun.id, TaskRun.finished_at), owner_id)
        return dict(session.exec(stmt).all())

    def load(
        self, session: Session, vault: Vault, owner_id: str, ids: list[str]
    ) -> list[Loaded[RecordMeta]]:
        rows = session.exec(
            select(TaskRun, ScheduledTask)
            .join(ScheduledTask, col(TaskRun.task_id) == col(ScheduledTask.id))
            .where(col(TaskRun.id).in_(ids))
        ).all()
        return [
            Loaded(
                Doc(
                    run.id,
                    run.finished_at or run.started_at,
                    RecordMeta(vault.decrypt_str(task.title_enc)),
                ),
                vault.decrypt_str(run.summary_enc),
            )
            for run, task in rows
            if run.summary_enc is not None
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
        self._engine = engine
        self._vault = vault
        self._collection = collection
        self._index = RecallIndex(engine, vault)
        self._label = label
        self._read_text = read_text
        self.source_kind = source_kind
        self.SOURCE_ID = source_id

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


def _run_text(session: Session, vault: Vault, owner_id: str, ref: str) -> str | None:
    run = session.get(TaskRun, ref)
    if run is None or run.summary_enc is None:
        return None
    task = session.get(ScheduledTask, run.task_id)
    if task is None or task.owner_id != owner_id:
        return None
    return vault.decrypt_str(run.summary_enc)


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
        RecordAdapter(
            engine,
            vault,
            RoutineRunCollection(),
            source_kind="routines",
            source_id="surf-routines",
            label="Routine outputs",
            read_text=_run_text,
        ),
    ]
