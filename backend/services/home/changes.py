"""What each thread changed in the operator's own project folders, and putting it back.

A thread that writes a project's folder (with a lease, or because it was started in it)
edits the operator's working copy directly. Each file it changes is recorded as it was
before the thread's first change, so the operator can put it back. The file tools record
around each write, and the shell around bringing a command's work out of the VM
(``services/vm/project.py``). Directories are not recorded; their files are.

A file is put back only if it still holds what the thread left: one the operator, or
another thread, has changed since is reported rather than overwritten.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Collection, Mapping, Sequence
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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _target(root: Path, rel: str) -> Path | None:
    try:
        return contained_path(root, rel)
    except SandboxError:
        return None


def _read(root: Path, rel: str, limit: int) -> _Before | None:
    """The file as it is now; None for a directory or a path outside ``root``."""
    target = _target(root, rel)
    if target is None or target.is_dir():
        return None
    if not target.is_file():
        return _Before(None)
    if target.stat().st_size > limit:
        return _Before(None, too_large=True)
    return _Before(target.read_bytes())


def _sha_now(root: Path, rel: str) -> str | None:
    target = _target(root, rel)
    if target is None or not target.is_file():
        return None
    return _sha(target.read_bytes())


class ThreadChanges:
    def __init__(self, engine: Engine, vault: Vault, *, max_kept_bytes: int = MAX_KEPT_BYTES):
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._limit: int = max_kept_bytes

    async def before(
        self,
        owner_id: str,
        conversation_id: str,
        project_id: str,
        root: Path,
        paths: Sequence[str],
    ) -> None:
        """Keep each file as it is now, the first time this thread changes it (or the
        first time since the operator put it back)."""
        limit = self._limit
        found = await asyncio.to_thread(lambda: {rel: _read(root, rel, limit) for rel in paths})

        def work(session: Session) -> None:
            rows = self._rows(session, owner_id, conversation_id, project_id)
            for rel, before in found.items():
                if before is None:
                    continue
                row = rows.get(rel)
                if row is not None and row.reverted_at is None:
                    continue
                if row is None:
                    row = ThreadChange(
                        owner_id=owner_id,
                        conversation_id=conversation_id,
                        project_id=project_id,
                        path_enc=self._vault.encrypt_str(rel),
                    )
                content = before.content
                row.original_enc = None if content is None else self._vault.encrypt_bytes(content)
                row.original_sha = None if content is None else _sha(content)
                row.original_kept = not before.too_large
                row.current_sha = row.original_sha
                row.reverted_at = None
                row.updated_at = datetime.now(UTC)
                session.add(row)

        await in_session(self._engine, work)

    async def after(
        self,
        owner_id: str,
        conversation_id: str,
        project_id: str,
        root: Path,
        paths: Sequence[str],
    ) -> None:
        """Note what each recorded file holds now that the thread has changed it."""
        now = await asyncio.to_thread(lambda: {rel: _sha_now(root, rel) for rel in paths})

        def work(session: Session) -> None:
            rows = self._rows(session, owner_id, conversation_id, project_id)
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
            for row in self._all(session, owner_id, conversation_id):
                status = _status(row)
                if status is None:
                    continue
                views.append(
                    ChangeView(
                        id=row.id,
                        project_id=row.project_id,
                        path=self._vault.decrypt_str(row.path_enc),
                        status=status,
                        reverted=row.reverted_at is not None,
                        revertible=row.original_kept,
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
            file = ThreadChangeFile(
                project_id=row.project_id, path=self._vault.decrypt_str(row.path_enc)
            )
            sealed = row.original_enc
            return file, None if sealed is None else self._vault.decrypt_bytes(sealed)

        return await in_session(self._engine, work)

    async def revert(
        self,
        owner_id: str,
        conversation_id: str,
        roots: Mapping[str, Path],
        ids: Collection[str] | None = None,
    ) -> RevertReport:
        """Put each change back (all of them, or those in ``ids``) where the file still
        holds what the thread left."""

        def work(session: Session) -> RevertReport:
            report = RevertReport()
            for row in self._all(session, owner_id, conversation_id):
                if row.reverted_at is not None or (ids is not None and row.id not in ids):
                    continue
                rel = self._vault.decrypt_str(row.path_enc)
                reason = self._put_back(row, roots.get(row.project_id), rel)
                if reason is None:
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

    def _put_back(self, row: ThreadChange, root: Path | None, rel: str) -> str | None:
        """Restore one file, or say why it was left alone. Blocking IO."""
        if root is None:
            return "its project is no longer registered"
        target = _target(root, rel)
        if target is None:
            return "its path leads outside the project"
        if not row.original_kept:
            return "it was too large to keep a copy of"
        if (_sha(target.read_bytes()) if target.is_file() else None) != row.current_sha:
            return "it has changed since this conversation last wrote it"
        if row.original_enc is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_bytes(self._vault.decrypt_bytes(row.original_enc))
        return None

    def _all(self, session: Session, owner_id: str, conversation_id: str) -> list[ThreadChange]:
        return list(
            session.exec(
                select(ThreadChange)
                .where(ThreadChange.owner_id == owner_id)
                .where(ThreadChange.conversation_id == conversation_id)
            ).all()
        )

    def _rows(
        self, session: Session, owner_id: str, conversation_id: str, project_id: str
    ) -> dict[str, ThreadChange]:
        return {
            self._vault.decrypt_str(row.path_enc): row
            for row in self._all(session, owner_id, conversation_id)
            if row.project_id == project_id
        }


class ChangeLog:
    """One thread's hand on the store, for the tools that change its projects' files."""

    def __init__(self, store: ThreadChanges, *, owner_id: str, conversation_id: str) -> None:
        self._store: ThreadChanges = store
        self._owner_id: str = owner_id
        self._conversation_id: str = conversation_id

    @staticmethod
    def records(mount: Mount) -> bool:
        """Whether a change in this place is the operator's working copy changing."""
        return mount.place.kind == "projects" and mount.project_id is not None and mount.writable

    async def before(self, mount: Mount, paths: Sequence[str]) -> None:
        if paths and mount.project_id is not None and self.records(mount):
            await self._store.before(
                self._owner_id, self._conversation_id, mount.project_id, mount.root, paths
            )

    async def after(self, mount: Mount, paths: Sequence[str]) -> None:
        if paths and mount.project_id is not None and self.records(mount):
            await self._store.after(
                self._owner_id, self._conversation_id, mount.project_id, mount.root, paths
            )


def _status(row: ThreadChange) -> ChangeStatus | None:
    if row.original_sha == row.current_sha and row.original_kept:
        return None
    if row.original_sha is None and row.original_kept:
        return "added"
    if row.current_sha is None:
        return "deleted"
    return "modified"
