"""What each thread changed in the operator's own project folders, and putting it back.

A thread that writes a project's folder (with a lease, or because it was started in it)
edits the operator's working copy directly. The first time a turn changes a file, the file
is recorded as it was before that turn, so the operator can put it back: all the way to
before the thread's first change, or to before any one turn. The file tools record around
each write, and the shell around bringing a command's work out of the VM
(``services/vm/project.py``). Directories are not recorded; their files are.

A file is put back only if it still holds what the thread left: one the operator, or
another thread, has changed since is reported rather than overwritten.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import NotFoundError
from core.vault import Vault
from models.thread_change import ThreadChange
from services.sandbox.base import SandboxError, contained_path

from .backend import Mount

#: The largest file whose original is kept; a larger one is recorded as changed, without a
#: way back.
MAX_KEPT_BYTES = 4 * 1024 * 1024

type ChangeStatus = Literal["added", "modified", "deleted"]
type _FileKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class ChangeView:
    id: str
    project_id: str
    path: str
    status: ChangeStatus
    reverted: bool
    #: Whether the original was kept, so the file can be put back.
    revertible: bool


@dataclass(frozen=True, slots=True)
class ThreadChangeFile:
    project_id: str
    path: str


@dataclass
class RevertReport:
    reverted: list[str] = field(default_factory=list)
    #: (path, why it was left as it is).
    skipped: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Before:
    content: bytes | None
    too_large: bool = False


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def target_of(root: Path, rel: str) -> Path | None:
    try:
        return contained_path(root, rel)
    except SandboxError:
        return None


def _read(root: Path, rel: str, limit: int) -> _Before | None:
    """The file as it is now; None for a directory or a path outside ``root``."""
    target = target_of(root, rel)
    if target is None or target.is_dir():
        return None
    if not target.is_file():
        return _Before(None)
    if target.stat().st_size > limit:
        return _Before(None, too_large=True)
    return _Before(target.read_bytes())


def sha_now(root: Path, rel: str) -> str | None:
    target = target_of(root, rel)
    if target is None or not target.is_file():
        return None
    return sha(target.read_bytes())


def _order(row: ThreadChange) -> tuple[datetime, str]:
    """Chronological by when the row's turn first changed the file."""
    return (row.created_at, row.id)


def _live(rows: list[ThreadChange]) -> list[ThreadChange]:
    """The rows still to be put back; a file the operator put back shows its history."""
    return [row for row in rows if row.reverted_at is None] or rows


def latest_row(rows: list[ThreadChange]) -> ThreadChange:
    """The row that last touched the file, whose ``current_sha`` is what it holds."""
    return max(rows, key=lambda row: row.updated_at)


class ThreadChanges:
    def __init__(self, engine: Engine, vault: Vault, *, max_kept_bytes: int = MAX_KEPT_BYTES):
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._limit: int = max_kept_bytes

    async def run[T](self, work: Callable[[Session], T]) -> T:
        """Run ``work`` in a committed session on this store's database."""
        return await in_session(self._engine, work)

    def unseal(self, sealed: bytes) -> bytes:
        """The plaintext of a kept original."""
        return self._vault.decrypt_bytes(sealed)

    async def before(
        self,
        owner_id: str,
        conversation_id: str,
        project_id: str,
        root: Path,
        paths: Sequence[str],
        *,
        prompt_message_id: str | None = None,
    ) -> None:
        """Keep each file as it is now, the first time this turn changes it (or the
        first time since the operator put it back)."""
        limit = self._limit
        found = await asyncio.to_thread(lambda: {rel: _read(root, rel, limit) for rel in paths})

        def work(session: Session) -> None:
            rows = self._turn_rows(
                session, owner_id, conversation_id, project_id, prompt_message_id
            )
            for rel, before in found.items():
                if before is None:
                    continue
                row = rows.get(rel)
                if row is not None and row.reverted_at is None:
                    continue
                now = datetime.now(UTC)
                if row is None:
                    row = ThreadChange(
                        owner_id=owner_id,
                        conversation_id=conversation_id,
                        project_id=project_id,
                        prompt_message_id=prompt_message_id,
                        path_enc=self._vault.encrypt_str(rel),
                    )
                content = before.content
                row.original_enc = None if content is None else self._vault.encrypt_bytes(content)
                row.original_sha = None if content is None else sha(content)
                row.original_kept = not before.too_large
                row.current_sha = row.original_sha
                row.reverted_at = None
                row.created_at = now
                row.updated_at = now
                session.add(row)

        await in_session(self._engine, work)

    async def after(
        self,
        owner_id: str,
        conversation_id: str,
        project_id: str,
        root: Path,
        paths: Sequence[str],
        *,
        prompt_message_id: str | None = None,
    ) -> None:
        """Note what each file recorded for this turn holds now that it has changed it."""
        now = await asyncio.to_thread(lambda: {rel: sha_now(root, rel) for rel in paths})

        def work(session: Session) -> None:
            rows = self._turn_rows(
                session, owner_id, conversation_id, project_id, prompt_message_id
            )
            for rel, sha in now.items():
                row = rows.get(rel)
                if row is None or row.reverted_at is not None:
                    continue
                row.current_sha = sha
                row.updated_at = datetime.now(UTC)
                session.add(row)

        await in_session(self._engine, work)

    async def list(self, owner_id: str, conversation_id: str) -> list[ChangeView]:
        """The thread's changes, by project and path; files it changed back to how they
        were are left out."""

        def work(session: Session) -> list[ChangeView]:
            views: list[ChangeView] = []
            for (project_id, rel), rows in self.files(session, owner_id, conversation_id).items():
                live = _live(rows)
                first, last = live[0], latest_row(live)
                status = _status(first, last)
                if status is None:
                    continue
                views.append(
                    ChangeView(
                        id=first.id,
                        project_id=project_id,
                        path=rel,
                        status=status,
                        reverted=first.reverted_at is not None,
                        revertible=first.original_kept,
                    )
                )
            return sorted(views, key=lambda view: (view.project_id, view.path))

        return await in_session(self._engine, work)

    async def original(
        self, owner_id: str, conversation_id: str, change_id: str
    ) -> tuple[ThreadChangeFile, bytes | None]:
        """One change's file, and its content before the thread's first change."""

        def work(session: Session) -> tuple[ThreadChangeFile, bytes | None]:
            row = session.get(ThreadChange, change_id)
            if row is None or row.owner_id != owner_id or row.conversation_id != conversation_id:
                raise NotFoundError("change not found")
            rel = self._vault.decrypt_str(row.path_enc)
            rows = self.files(session, owner_id, conversation_id)[(row.project_id, rel)]
            sealed = _live(rows)[0].original_enc
            file = ThreadChangeFile(project_id=row.project_id, path=rel)
            return file, None if sealed is None else self._vault.decrypt_bytes(sealed)

        return await in_session(self._engine, work)

    async def revert(
        self,
        owner_id: str,
        conversation_id: str,
        roots: Mapping[str, Path],
        ids: Collection[str] | None = None,
    ) -> RevertReport:
        """Put each file back to how it was before the thread's first change (all of
        them, or those whose change is in ``ids``) where it still holds what the thread
        left."""

        def work(session: Session) -> RevertReport:
            report = RevertReport()
            for (project_id, rel), rows in self.files(session, owner_id, conversation_id).items():
                live = [row for row in rows if row.reverted_at is None]
                if not live or (ids is not None and not any(row.id in ids for row in live)):
                    continue
                reason = self._put_back(live[0], latest_row(live), roots.get(project_id), rel)
                if reason is None:
                    for row in live:
                        row.reverted_at = datetime.now(UTC)
                        session.add(row)
                    report.reverted.append(rel)
                else:
                    report.skipped.append((rel, reason))
            return report

        return await in_session(self._engine, work)

    async def delete_for_conversation(self, owner_id: str, conversation_id: str) -> None:
        def work(session: Session) -> None:
            _ = session.exec(
                delete(ThreadChange)
                .where(col(ThreadChange.owner_id) == owner_id)
                .where(col(ThreadChange.conversation_id) == conversation_id)
            )

        await in_session(self._engine, work)

    def _put_back(
        self, first: ThreadChange, last: ThreadChange, root: Path | None, rel: str
    ) -> str | None:
        """Restore one file to ``first``'s original, or say why it was left alone.
        Blocking IO."""
        if root is None:
            return "its project is no longer registered"
        target = target_of(root, rel)
        if target is None:
            return "its path leads outside the project"
        if not first.original_kept:
            return "it was too large to keep a copy of"
        if (sha(target.read_bytes()) if target.is_file() else None) != last.current_sha:
            return "it has changed since this conversation last wrote it"
        if first.original_enc is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_bytes(self._vault.decrypt_bytes(first.original_enc))
        return None

    def _all(self, session: Session, owner_id: str, conversation_id: str) -> list[ThreadChange]:
        return list(
            session.exec(
                select(ThreadChange)
                .where(ThreadChange.owner_id == owner_id)
                .where(ThreadChange.conversation_id == conversation_id)
            ).all()
        )

    def files(
        self, session: Session, owner_id: str, conversation_id: str
    ) -> dict[_FileKey, list[ThreadChange]]:
        """Each file's rows, one per turn that changed it, oldest first."""
        files: dict[_FileKey, list[ThreadChange]] = {}
        for row in sorted(self._all(session, owner_id, conversation_id), key=_order):
            key = (row.project_id, self._vault.decrypt_str(row.path_enc))
            files.setdefault(key, []).append(row)
        return files

    def _turn_rows(
        self,
        session: Session,
        owner_id: str,
        conversation_id: str,
        project_id: str,
        prompt_message_id: str | None,
    ) -> dict[str, ThreadChange]:
        return {
            self._vault.decrypt_str(row.path_enc): row
            for row in self._all(session, owner_id, conversation_id)
            if row.project_id == project_id and row.prompt_message_id == prompt_message_id
        }


class ChangeLog:
    """One thread's hand on the store, for the tools that change its projects' files."""

    def __init__(
        self,
        store: ThreadChanges,
        *,
        owner_id: str,
        conversation_id: str,
        prompt_message_id: str | None = None,
    ) -> None:
        self._store: ThreadChanges = store
        self._owner_id: str = owner_id
        self._conversation_id: str = conversation_id
        self._prompt_message_id: str | None = prompt_message_id

    @staticmethod
    def records(mount: Mount) -> bool:
        """Whether a change in this place is the operator's working copy changing."""
        return mount.place.kind == "projects" and mount.project_id is not None and mount.writable

    async def before(self, mount: Mount, paths: Sequence[str]) -> None:
        if paths and mount.project_id is not None and self.records(mount):
            await self._store.before(
                self._owner_id,
                self._conversation_id,
                mount.project_id,
                mount.root,
                paths,
                prompt_message_id=self._prompt_message_id,
            )

    async def after(self, mount: Mount, paths: Sequence[str]) -> None:
        if paths and mount.project_id is not None and self.records(mount):
            await self._store.after(
                self._owner_id,
                self._conversation_id,
                mount.project_id,
                mount.root,
                paths,
                prompt_message_id=self._prompt_message_id,
            )


def _status(first: ThreadChange, last: ThreadChange) -> ChangeStatus | None:
    """How the file differs now from before ``first``'s turn."""
    if first.original_sha == last.current_sha and first.original_kept:
        return None
    if first.original_sha is None and first.original_kept:
        return "added"
    if last.current_sha is None:
        return "deleted"
    return "modified"
