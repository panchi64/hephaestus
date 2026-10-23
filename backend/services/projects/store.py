"""The project catalog: CRUD, the active selection, and the git probe.

**Null means unfiled, and unfiled is visible everywhere.** `visible_project_ids` is the
one place the scope rule lives; every filtered query reads it:

    visible   = unfiled  ∪  the active project
    invisible = every other project

Existing rows are all unfiled, so activating a project only adds to what the operator
sees. A project chat must reach the general memory, and must not reach another project's
notes.

**The path is sealed; the git facts are not.** A directory path names the operator's
clients, so `root_path_enc` is vault-sealed. Repo status is re-probed on every read.

**`probe` never mutates.** `git init` is a separate, explicitly confirmed act
(`services/projects/worktree.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import ColumnElement, Engine, delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import InvalidInputError, NotFoundError
from core.vault import Vault
from models._fields import utcnow
from models.conversation import Conversation
from models.corpus import CorpusSource
from models.project import Project
from models.subagent import SubagentRecord
from models.task import ScheduledTask
from models.thread_change import ThreadChange
from models.thread_worktree import ThreadWorktree
from services.sandbox.gitenv import GIT_ARG_PINS, git_config_pins
from services.settings_store import ACTIVE_PROJECT_KEY, SettingsStore

from .slugs import slug_for

#: How many times a registration picks a slug before a clash is reported.
_SLUG_ATTEMPTS = 3

#: Every entity that carries a `project_id`; there is no shared base class, and deleting a
#: project must unfile all of them. `tests/test_projects.py` pins this against the schema.
SCOPED_MODELS: tuple[type[Conversation | ScheduledTask | CorpusSource | SubagentRecord], ...] = (
    Conversation,
    ScheduledTask,
    CorpusSource,
    SubagentRecord,
)


def visible_project_ids(active: str | None) -> tuple[str | None, ...]:
    """The project ids a query may return rows for: unfiled, plus the active project."""
    return (None,) if active is None else (None, active)


def project_clause(
    column: str | None, visible: tuple[str | None, ...] | None
) -> ColumnElement[bool] | None:
    """The scope rule as one SQL clause, or ``None`` for no filtering at all.

    SQL ``IN`` never matches NULL, so ``column.in_(visible)`` would silently drop every
    unfiled row, which includes all history that predates projects. Scoped queries use this
    instead of their own ``WHERE``.

    ``visible=None`` is the ALL PROJECTS scope and gets no clause.
    """
    if visible is None:
        return None
    filed = [pid for pid in visible if pid is not None]
    field = col(column)
    unfiled = field.is_(None)
    return unfiled if not filed else or_(unfiled, field.in_(filed))


@dataclass(frozen=True)
class RepoProbe:
    """What a directory actually is, right now. Re-read on every listing."""

    exists: bool
    is_git_repo: bool
    # None when the path is not a repo; 0 is a clean tree.
    uncommitted_changes: int | None = None
    current_branch: str | None = None


@dataclass(frozen=True)
class ProjectView:
    id: str
    name: str
    #: The project's folder in the agent's home, as `~/projects/<slug>`.
    slug: str
    root_path: str
    git_initialized: bool
    base_ref: str
    archived: bool
    created_at: datetime
    last_opened_at: datetime
    probe: RepoProbe


async def _run_git(cwd: Path, *args: str) -> tuple[int, str]:
    """One `git` invocation with a fixed argv and no shell. Returns (code, stdout)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *GIT_ARG_PINS,
        *args,
        cwd=str(cwd),
        env=git_config_pins(os.environ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def probe_repo(root: Path) -> RepoProbe:
    if not root.is_dir():
        return RepoProbe(exists=False, is_git_repo=False)
    code, _ = await _run_git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return RepoProbe(exists=True, is_git_repo=False)
    _, status = await _run_git(root, "status", "--porcelain")
    _, branch = await _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    changed = len([line for line in status.splitlines() if line.strip()])
    return RepoProbe(
        exists=True,
        is_git_repo=True,
        uncommitted_changes=changed,
        current_branch=branch.strip() or None,
    )


class ProjectStore:
    """Owner-scoped project catalog. Vault-sealed paths; the active selection is a
    preference, not session state, so it survives a reload and a lock."""

    def __init__(self, db_engine: Engine, vault: Vault, settings: SettingsStore) -> None:
        self._db: Engine = db_engine
        self._vault: Vault = vault
        self._settings: SettingsStore = settings
        # owner -> {project id: directory}, most recently opened first: the memoised
        # `workspace_roots` answer, cleared by `_forget` and on lock so decrypted paths
        # never outlive the key.
        self._roots: dict[str, dict[str, Path]] = {}
        vault.register_on_lock(self._roots.clear)
        # owner -> {project id: slug}, cleared by the same writes.
        self._slugs: dict[str, dict[str, str]] = {}

    @property
    def engine(self) -> Engine:
        return self._db

    @property
    def vault(self) -> Vault:
        """The vault that seals project paths."""
        return self._vault

    async def _view(self, row: Project) -> ProjectView:
        root = Path(self._vault.decrypt_str(row.root_path_enc))
        return ProjectView(
            id=row.id,
            name=row.name,
            slug=row.slug,
            root_path=str(root),
            git_initialized=row.git_initialized,
            base_ref=row.base_ref,
            archived=row.archived,
            created_at=row.created_at,
            last_opened_at=row.last_opened_at,
            probe=await probe_repo(root),
        )

    async def list(self, owner_id: str, *, include_archived: bool = False) -> list[ProjectView]:
        def work(session: Session) -> list[Project]:
            query = select(Project).where(Project.owner_id == owner_id)
            if not include_archived:
                query = query.where(col(Project.archived).is_(False))
            return list(session.exec(query.order_by(col(Project.last_opened_at).desc())).all())

        rows = await in_session(self._db, work)
        # Probes shell out, so run them concurrently.
        return list(await asyncio.gather(*(self._view(row) for row in rows)))

    async def get(self, owner_id: str, project_id: str) -> ProjectView:
        return await self._view(await self._row(owner_id, project_id))

    async def _row(self, owner_id: str, project_id: str) -> Project:
        def work(session: Session) -> Project | None:
            row = session.get(Project, project_id)
            return row if row is not None and row.owner_id == owner_id else None

        row = await in_session(self._db, work)
        if row is None:
            raise NotFoundError("project not found")
        return row

    async def create(self, owner_id: str, name: str, root_path: str) -> ProjectView:
        resolved = self._resolve_root(root_path)
        sealed = self._vault.encrypt_str(str(resolved))

        def work(session: Session) -> Project:
            taken = session.exec(select(Project.slug).where(Project.owner_id == owner_id)).all()
            label = name.strip() or resolved.name
            row = Project(
                owner_id=owner_id,
                name=label,
                slug=slug_for(label, set(taken)),
                root_path_enc=sealed,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

        # Two registrations can read the same taken slugs; the unique constraint refuses
        # the second, which then picks again.
        for _ in range(_SLUG_ATTEMPTS - 1):
            with contextlib.suppress(IntegrityError):
                created = await in_session(self._db, work)
                break
        else:
            created = await in_session(self._db, work)
        self._forget(owner_id)
        return await self._view(created)

    @staticmethod
    def _resolve_root(root_path: str) -> Path:
        """The absolute, symlink-resolved directory a caller named, or a refusal.

        Shared with :meth:`ensure_for_path` so two spellings of one directory (symlink,
        trailing slash, ``~``) do not file as two projects.
        """
        root = Path(root_path).expanduser()
        if not root.is_absolute():
            # A relative path would resolve against the server's cwd.
            raise InvalidInputError("project path must be absolute")
        if not root.is_dir():
            raise InvalidInputError(f"no such directory: {root}")
        return root.resolve()

    async def ensure_for_path(self, owner_id: str, root_path: str) -> ProjectView:
        """The project for this directory, creating one the first time it is used.

        Any directory is a project, named after itself, with whatever git facts it has.

        Matching is on the decrypted path: sealing is randomised, so comparing ciphertext
        would create a second row for the same directory. The backup importer resolves the
        ``root_path_enc`` natural key the same way.

        An archived match is unarchived, since pointing at the directory again says the
        operator works there and returning it archived would leave a session in a project
        their listing hides.

        Creating a row is all this does to the directory; whether it is a repository is read
        (:func:`probe_repo`), and ``git init`` stays a separate confirmed act.
        """
        resolved = self._resolve_root(root_path)

        def find(session: Session) -> Project | None:
            rows = session.exec(select(Project).where(Project.owner_id == owner_id)).all()
            target = str(resolved)
            return next(
                (r for r in rows if self._vault.decrypt_str(r.root_path_enc) == target), None
            )

        existing = await in_session(self._db, find)
        if existing is not None:
            if not existing.archived:
                return await self._view(existing)
            return await self.update(owner_id, existing.id, archived=False)

        return await self.create(owner_id, resolved.name, str(resolved))

    async def workspace_roots(self, owner_id: str) -> dict[str, Path]:
        """Each project id mapped to its directory, most recently opened first.

        Cheaper than :meth:`list`: no git probe, which would put one subprocess per project
        behind every thread-list refresh and every file-path click.

        Insertion order is load-bearing: opening a path the model named searches these
        directories in order, and the most recently used is the likeliest.

        **Memoised, because the conversation rail polls it** every couple of seconds. The
        memo is dropped by every write through this store (:meth:`_forget`) and by a vault
        lock; only writes that bypass the store (the backup importer) can leave it stale.
        """
        cached = self._roots.get(owner_id)
        if cached is not None:
            return dict(cached)  # a copy, so a caller's mutation cannot edit the memo

        def work(session: Session) -> list[Project]:
            query = select(Project).where(Project.owner_id == owner_id)
            return list(session.exec(query.order_by(col(Project.last_opened_at).desc())).all())

        rows = await in_session(self._db, work)
        roots = {row.id: Path(self._vault.decrypt_str(row.root_path_enc)) for row in rows}
        self._roots[owner_id] = roots
        return dict(roots)

    async def slugs(self, owner_id: str) -> dict[str, str]:
        """Each live project's id mapped to its folder name in the agent's home,
        ``~/projects/<slug>``. Archived projects are not in the home."""
        cached = self._slugs.get(owner_id)
        if cached is not None:
            return dict(cached)

        def work(session: Session) -> list[tuple[str, str]]:
            query = (
                select(Project.id, Project.slug)
                .where(Project.owner_id == owner_id)
                .where(col(Project.archived).is_(False))
            )
            return list(session.exec(query).all())

        slugs = dict(await in_session(self._db, work))
        self._slugs[owner_id] = slugs
        return dict(slugs)

    def _forget(self, owner_id: str) -> None:
        """Drop this owner's memoised roots and slugs; called by every write that can change
        them, including :meth:`activate`, whose ``last_opened_at`` touch reorders the roots."""
        _ = self._roots.pop(owner_id, None)
        _ = self._slugs.pop(owner_id, None)

    async def workspace_names(self, owner_id: str) -> dict[str, str]:
        """Each project id mapped to the **basename** of its directory.

        What the sidebar groups worktree threads under; not the full path, which would
        display the operator's clients across the always-visible rail.
        """
        return {pid: root.name for pid, root in (await self.workspace_roots(owner_id)).items()}

    async def update(
        self,
        owner_id: str,
        project_id: str,
        *,
        name: str | None = None,
        base_ref: str | None = None,
        archived: bool | None = None,
        git_initialized: bool | None = None,
    ) -> ProjectView:
        row = await self._row(owner_id, project_id)

        def work(session: Session) -> Project:
            stored = session.get(Project, row.id)
            assert stored is not None  # _row already proved ownership in this process
            if name is not None:
                stored.name = name
            if base_ref is not None:
                stored.base_ref = base_ref
            if archived is not None:
                stored.archived = archived
            if git_initialized is not None:
                stored.git_initialized = git_initialized
            session.add(stored)
            session.commit()
            session.refresh(stored)
            return stored

        updated = await in_session(self._db, work)
        self._forget(owner_id)
        return await self._view(updated)

    async def delete(self, owner_id: str, project_id: str) -> None:
        """Remove the project and **unfile everything that was in it**.

        A deleted id can never be active again, so rows left pointing at it would vanish
        from every listing; the operator deleted a label, not their conversations.
        """
        row = await self._row(owner_id, project_id)

        def work(session: Session) -> None:
            for model_cls in SCOPED_MODELS:
                _ = session.exec(
                    update(model_cls)
                    .where(col(model_cls.project_id) == row.id)
                    .values(project_id=None)
                )
            # Neither a worktree nor a recorded change of a deleted project is usable.
            for gone in (ThreadWorktree, ThreadChange):
                _ = session.exec(delete(gone).where(col(gone.project_id) == row.id))
            stored = session.get(Project, row.id)
            if stored is not None:
                session.delete(stored)
            session.commit()

        await in_session(self._db, work)
        self._forget(owner_id)
        # Deleting the active project leaves nothing active rather than a dangling id.
        if await self.active_id(owner_id) == project_id:
            await self.activate(owner_id, None)

    async def active_id(self, owner_id: str) -> str | None:
        value = await self._settings.get(owner_id, ACTIVE_PROJECT_KEY)
        return value or None

    async def activate(self, owner_id: str, project_id: str | None) -> None:
        if project_id is not None:
            _ = await self._row(owner_id, project_id)  # 404 rather than a dangling selection

            def touch(session: Session) -> None:
                stored = session.get(Project, project_id)
                if stored is not None:
                    stored.last_opened_at = utcnow()
                    session.add(stored)
                    session.commit()

            await in_session(self._db, touch)
            self._forget(owner_id)  # `last_opened_at` moved: the map's order changed
        await self._settings.set(owner_id, ACTIVE_PROJECT_KEY, project_id or "")

    async def root_path(self, owner_id: str, project_id: str) -> Path:
        """The decrypted host root, so nothing else needs to know the column is sealed."""
        row = await self._row(owner_id, project_id)
        return Path(self._vault.decrypt_str(row.root_path_enc))
