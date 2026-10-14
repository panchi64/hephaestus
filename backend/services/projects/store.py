"""The project catalog: CRUD, the active selection, and the git probe.

Three things here are worth knowing before changing anything.

**Null means unfiled, and unfiled is visible everywhere.** `visible_project_ids` is the
one place the scope rule lives, and every filtered query in the app reads it rather than
writing its own `WHERE`. The rule is:

    visible   = unfiled  ∪  the active project
    invisible = every other project

That is what makes the scope safe to introduce over an existing database — every row
that already exists is unfiled, so activating a project *adds* to what the operator sees
and never subtracts. It is also why lists and recall use the same helper: a project chat
that could not reach the operator's general memory would be a worse assistant, and a
chat that could reach *another* project's notes would be a leak.

**The path is sealed; the git facts are not.** A directory path names the operator's
clients and employers, so `root_path_enc` is vault-sealed like any other content. Whether
the directory is a repo, and how many changes are uncommitted, are facts about the
world that we re-probe on every read anyway.

**`probe` never mutates.** It answers "what is this directory" so the UI can ask the
operator whether to `git init` — running one is a separate, explicitly confirmed act
(`services/projects/worktree.py`). A read that quietly created a repository in someone's
directory would be exactly the kind of surprise this feature exists to avoid.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import ColumnElement, Engine, or_, update
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
from services.settings_store import ACTIVE_PROJECT_KEY, SettingsStore

#: Every entity that carries a `project_id`. Written out because there is no shared base
#: class — each model declares `owner_id` inline too — and because deleting a project has
#: to unfile *all* of them or the ones it misses vanish from every listing forever.
#: `tests/test_projects.py` pins this against the live schema, so a fourth scoped entity
#: fails there rather than silently losing its rows on the next project delete.
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

    Every scoped query goes through this rather than writing its own ``WHERE``, because
    the rule has a trap in it: SQL ``IN`` never matches NULL, so the obvious
    ``column.in_(visible)`` silently drops every **unfiled** row — which is every row
    that existed before projects did. A query written that way looks correct, passes a
    test that only uses filed rows, and blanks the operator's history in production.

    ``visible=None`` means the caller asked for everything (the ALL PROJECTS scope) and
    gets no clause at all.
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
    # None when the path is not a repo — distinct from 0, which means a clean tree.
    uncommitted_changes: int | None = None
    current_branch: str | None = None


@dataclass(frozen=True)
class ProjectView:
    id: str
    name: str
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
        *args,
        cwd=str(cwd),
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
        # owner → {project id: directory}, in most-recently-opened order — the whole
        # answer `workspace_roots` gives, memoised. See it for why, and `_forget` for
        # what clears it. Dropped on lock, so decrypted paths never outlive the key that
        # protects them: a locked vault answers exactly as it did before this cache, by
        # failing to decrypt rather than by serving what it read while unlocked.
        self._roots: dict[str, dict[str, Path]] = {}
        vault.register_on_lock(self._roots.clear)

    async def _view(self, row: Project) -> ProjectView:
        root = Path(self._vault.decrypt_str(row.root_path_enc))
        return ProjectView(
            id=row.id,
            name=row.name,
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
        # Probes shell out, so fan them out rather than paying serially per project.
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
            row = Project(
                owner_id=owner_id,
                name=name.strip() or resolved.name,
                root_path_enc=sealed,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

        created = await in_session(self._db, work)
        self._forget(owner_id)
        return await self._view(created)

    @staticmethod
    def _resolve_root(root_path: str) -> Path:
        """The absolute, symlink-resolved directory a caller named, or a refusal.

        Split out of :meth:`create` because :meth:`ensure_for_path` has to compare against
        the *same* normalisation the stored rows were written with — two spellings of one
        directory (a symlink, a trailing slash, ``~``) would otherwise file as two
        projects and cut two worktrees from the same repository.
        """
        root = Path(root_path).expanduser()
        if not root.is_absolute():
            # A browser cannot produce an absolute host path, so the operator either
            # typed one or used the native picker. A relative path here would resolve
            # against the *server's* cwd, which is never what they meant.
            raise InvalidInputError("project path must be absolute")
        if not root.is_dir():
            raise InvalidInputError(f"no such directory: {root}")
        return root.resolve()

    async def ensure_for_path(self, owner_id: str, root_path: str) -> ProjectView:
        """The project for this directory, creating one the first time it is used.

        A project is the storage and the git machinery a worktree thread needs; it was never
        meant to be a thing the operator files paperwork for before they can start
        working. This is the difference: point at any directory and it *is* a project,
        named after itself, with whatever git facts it already has.

        Matching is on the **decrypted** path rather than on the ciphertext, which is not
        an optimisation to skip: sealing is randomised, so the same directory encrypts to
        a different string every time and a ciphertext comparison would create a second
        row for a directory that already has one. The backup importer resolves the
        ``root_path_enc`` natural key the same way, for the same reason.

        Never revives an archived project silently — an archived row still matches, and
        returning it archived would leave the operator with a session in a project their
        listing does not show. It is unarchived, because pointing at the directory again
        is exactly the act of saying they work here after all.

        Creating a row is the *only* thing this does to the directory. Whether it is a
        repository stays a fact we read (:func:`probe_repo`, on every view) and never one
        we establish — ``git init`` remains the separate, explicitly confirmed act it has
        always been, so the returned view is what tells the caller a repository is still
        needed.
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

        Cheaper than :meth:`list` on purpose — no git probe, which shells out once per
        project and would put N subprocesses behind every refresh of the thread list and
        behind every click on a file path in an answer.

        Insertion order is load-bearing for the second of those: opening a path the model
        named means searching the operator's directories for it, and the one they worked
        in most recently is the one they meant.

        **Memoised, because this is on the polling path.** The conversation rail re-reads
        it on every refresh — which is every couple of seconds while anything is running
        — and each read was a full table scan plus one AEAD decrypt per project, to
        rebuild a map that only changes when a project is written. The memo is dropped by
        every write that goes through this store (:meth:`_forget`) and by a vault lock, so
        the only way to serve a stale answer is to write ``project`` rows around the store
        entirely — which is the backup importer, already outside every cache in the app.
        """
        cached = self._roots.get(owner_id)
        if cached is not None:
            return dict(cached)  # a copy: a caller's mutation must not edit the memo

        def work(session: Session) -> list[Project]:
            query = select(Project).where(Project.owner_id == owner_id)
            return list(session.exec(query.order_by(col(Project.last_opened_at).desc())).all())

        rows = await in_session(self._db, work)
        roots = {row.id: Path(self._vault.decrypt_str(row.root_path_enc)) for row in rows}
        self._roots[owner_id] = roots
        return dict(roots)

    def _forget(self, owner_id: str) -> None:
        """Drop this owner's memoised roots. Called by every write that can change what
        :meth:`workspace_roots` answers — which includes :meth:`activate`, because the
        map's *order* is part of the answer and touching ``last_opened_at`` reorders it."""
        _ = self._roots.pop(owner_id, None)

    async def workspace_names(self, owner_id: str) -> dict[str, str]:
        """Each project id mapped to the **basename** of its directory.

        What the sidebar groups worktree threads under. Deliberately not the path: a
        conversation listing is the one screen that is always on display, and the full
        path names the operator's clients across the whole width of the rail. The
        basename is the part that identifies the work.
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

        Unfiling is the whole point. The scope rule is `visible = unfiled ∪ active`, and a
        deleted id can never be active again — so rows left pointing at it would drop out
        of every listing permanently, with no way back from the UI. The operator deleted a
        *label*, not a year of conversations. Returning them to unfiled makes them visible
        everywhere again, which is exactly where they were before projects existed.
        """
        row = await self._row(owner_id, project_id)

        def work(session: Session) -> None:
            for model_cls in SCOPED_MODELS:
                _ = session.exec(
                    update(model_cls)
                    .where(col(model_cls.project_id) == row.id)
                    .values(project_id=None)
                )
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
        """The decrypted host root. The one accessor the worktree layer needs, kept here
        so nothing else has to know the column is sealed."""
        row = await self._row(owner_id, project_id)
        return Path(self._vault.decrypt_str(row.root_path_enc))
