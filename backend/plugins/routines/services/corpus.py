"""Routine run outputs as a corpus source, so platform search can find what a routine said."""

from __future__ import annotations

from collections.abc import Hashable
from typing import override

from sqlalchemy import ColumnElement, Engine, func
from sqlmodel import Session, col, select

from core.vault import Vault
from services.corpus.records import RecordAdapter, RecordMeta
from services.recall_index import Collection, Doc, Loaded

from ..models import ScheduledTask, TaskRun

SOURCE_KIND = "routines"
SURFACE_ID = "surf-routines"


class RoutineRunCollection(Collection[RecordMeta]):
    name: str = "routine-runs"

    @staticmethod
    def _owned(owner_id: str) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
        return (
            col(ScheduledTask.owner_id) == owner_id,
            col(TaskRun.summary_enc).is_not(None),
        )

    @override
    def signature(self, session: Session, owner_id: str) -> Hashable:
        stmt = (
            select(func.count(), func.max(TaskRun.finished_at))
            .join(ScheduledTask, col(TaskRun.task_id) == col(ScheduledTask.id))
            .where(*self._owned(owner_id))
        )
        return tuple(session.exec(stmt).one())

    @override
    def stamps(self, session: Session, owner_id: str) -> dict[str, Hashable]:
        stmt = (
            select(TaskRun.id, TaskRun.finished_at)
            .join(ScheduledTask, col(TaskRun.task_id) == col(ScheduledTask.id))
            .where(*self._owned(owner_id))
        )
        return dict(session.exec(stmt).all())

    @override
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


def _run_text(session: Session, vault: Vault, owner_id: str, ref: str) -> str | None:
    run = session.get(TaskRun, ref)
    if run is None or run.summary_enc is None:
        return None
    task = session.get(ScheduledTask, run.task_id)
    if task is None or task.owner_id != owner_id:
        return None
    return vault.decrypt_str(run.summary_enc)


def routine_runs_adapter(engine: Engine, vault: Vault) -> RecordAdapter:
    return RecordAdapter(
        engine,
        vault,
        RoutineRunCollection(),
        source_kind=SOURCE_KIND,
        source_id=SURFACE_ID,
        label="Routine outputs",
        read_text=_run_text,
    )
