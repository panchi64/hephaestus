"""Putting a thread's project files back to before some turns: the dry run, its token, and
the write.

A rewind is previewed first. The token names the planned files and what each holds on disk,
so a change to any of them between the preview and the confirm is refused.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import StaleRewindError
from models.thread_change import ThreadChange

from .changes import ThreadChangeFile, ThreadChanges, latest_row, sha, sha_now, target_of

logger = logging.getLogger(__name__)

#: A file a rewind writes: where, what (None deletes), and the rows that then count as put back.
type _Action = tuple[ThreadChangeFile, Path, bytes | None, list[str]]


@dataclass(frozen=True, slots=True)
class RewindConflict:
    project_id: str
    path: str
    reason: str


@dataclass
class RewindPlan:
    """What rewinding some turns' file changes would do, with nothing yet done."""

    restore: list[ThreadChangeFile] = field(default_factory=list)
    delete: list[ThreadChangeFile] = field(default_factory=list)
    #: Files left alone because they no longer hold what the turns left.
    conflicts: list[RewindConflict] = field(default_factory=list)
    #: Files whose original was too large to keep.
    unrestorable: list[ThreadChangeFile] = field(default_factory=list)
    #: Files a rewind could not write; empty in a preview.
    failed: list[RewindConflict] = field(default_factory=list)
    #: Names the planned files and what each holds on disk now; a change to any of them
    #: since the preview makes the token stale. It does not cover which turns were named.
    preview_token: str = ""


class Rewinder:
    def __init__(self, store: ThreadChanges) -> None:
        self._store: ThreadChanges = store

    async def plan(
        self,
        owner_id: str,
        conversation_id: str,
        prompt_ids: Collection[str],
        roots: Mapping[str, Path],
    ) -> RewindPlan:
        """What putting every file back to before the turns ``prompt_ids`` would do.
        Writes nothing."""

        def work(session: Session) -> RewindPlan:
            return self._plan(session, owner_id, conversation_id, prompt_ids, roots)[0]

        return await in_session(self._store.engine, work)

    async def apply(
        self,
        owner_id: str,
        conversation_id: str,
        prompt_ids: Collection[str],
        roots: Mapping[str, Path],
        preview_token: str,
    ) -> RewindPlan:
        """Carry out the plan :meth:`plan` previewed and return what it did. Raises
        :class:`StaleRewindError` when the planned files, or what they hold on disk, no
        longer match the token.

        The files are written between two sessions rather than inside one, because a
        session that loses the database lock is run again whole and would then plan
        against files already restored. A file that cannot be written is reported in
        ``failed`` and the rest go on."""

        def plan_work(session: Session) -> tuple[RewindPlan, list[_Action]]:
            plan, actions = self._plan(session, owner_id, conversation_id, prompt_ids, roots)
            if plan.preview_token != preview_token:
                raise StaleRewindError("the files changed since the rewind was previewed")
            return plan, actions

        plan, actions = await in_session(self._store.engine, plan_work)

        def write() -> list[_Action]:
            done: list[_Action] = []
            for action in actions:
                file, target, original, _ = action
                try:
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        _ = target.write_bytes(original)
                except OSError as exc:
                    plan.failed.append(RewindConflict(file.project_id, file.path, str(exc)))
                    (plan.delete if original is None else plan.restore).remove(file)
                else:
                    done.append(action)
            return done

        written = await asyncio.to_thread(write)
        if plan.failed:
            logger.warning(
                "rewind of conversation %s could not write %s",
                conversation_id,
                [failure.path for failure in plan.failed],
            )
        ids = {row_id for _, _, _, row_ids in written for row_id in row_ids}

        def mark_work(session: Session) -> None:
            now = datetime.now(UTC)
            for row in session.exec(select(ThreadChange).where(col(ThreadChange.id).in_(ids))):
                row.reverted_at = now
                session.add(row)

        if ids:
            await in_session(self._store.engine, mark_work)
        return plan

    def _plan(
        self,
        session: Session,
        owner_id: str,
        conversation_id: str,
        prompt_ids: Collection[str],
        roots: Mapping[str, Path],
    ) -> tuple[RewindPlan, list[_Action]]:
        """The plan, and for each file it would write: where, what to write (None deletes)
        and the rows that then count as put back. Blocking IO."""
        plan = RewindPlan()
        actions: list[_Action] = []
        entries: list[list[str | None]] = []
        wanted = set(prompt_ids)
        for (project_id, rel), rows in self._store.files(
            session, owner_id, conversation_id
        ).items():
            chosen = [
                row for row in rows if row.prompt_message_id in wanted and row.reverted_at is None
            ]
            if not chosen:
                continue
            first, last = chosen[0], latest_row(chosen)
            file = ThreadChangeFile(project_id=project_id, path=rel)
            root = roots.get(project_id)
            target = None if root is None else target_of(root, rel)
            if root is None or target is None:
                reason = (
                    "its project is no longer registered"
                    if root is None
                    else "its path leads outside the project"
                )
                plan.conflicts.append(RewindConflict(project_id, rel, reason))
                entries.append(["conflict", project_id, rel, None, reason])
                continue
            disk = sha_now(root, rel)
            if not first.original_kept:
                plan.unrestorable.append(file)
                entries.append(["unrestorable", project_id, rel, disk, None])
                continue
            if disk != last.current_sha:
                reason = "it has changed since this conversation last wrote it"
                plan.conflicts.append(RewindConflict(project_id, rel, reason))
                entries.append(["conflict", project_id, rel, disk, first.original_sha])
                continue
            if disk == first.original_sha:
                continue
            original = (
                None
                if first.original_enc is None
                else self._store.vault.decrypt_bytes(first.original_enc)
            )
            (plan.delete if original is None else plan.restore).append(file)
            entries.append(
                [
                    "delete" if original is None else "restore",
                    project_id,
                    rel,
                    disk,
                    first.original_sha,
                ]
            )
            actions.append((file, target, original, [row.id for row in chosen]))
        entries.sort(key=lambda entry: [part or "" for part in entry])
        plan.preview_token = sha(json.dumps(entries).encode())
        return plan, actions
