"""One answer to "which folder is this run's own place".

Attachment staging, the skills bundler, the project's agents and commands and the
thread's home all read one resolved value:

    "sandbox"  → the conversation's scratch, at ``/work`` in the VM
    "checkout" → the project the thread is filed under, on the host, on its current branch
    "worktree" → the thread's own git worktree of that project, on the host

Which one a run gets follows from its binding: a worktree if the thread opened one, else
the project it is filed under, else its scratch.

:class:`RunWorkspace` carries both halves — the host directory to act on, and
:meth:`RunWorkspace.display`, the path *string the model is told*. Those differ in a
sandbox (host `<data>/sandbox/work/<key>/x` is `/work/x` to the model) and are the same in
a worktree, which is exactly the kind of detail that goes wrong when four modules each
build it themselves.

**Attachments and skills stage under ``.hephaestus/`` in a worktree.** They have to land
inside it or the file tools can't reach them, and they must not show up as the agent's
work — so :func:`prepare_worktree_workspace` makes that directory ignore itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from services.home.paths import HOME
from services.projects.repo import run_git
from services.projects.store import ProjectStore
from services.projects.worktree import WorktreeBusyError, WorktreeManager
from services.sandbox import (
    LiveWork,
    SandboxError,
    SandboxSessionManager,
)
from services.sandbox.base import contained_path

logger = logging.getLogger(__name__)

#: What a run's own place is (see the module note).
type PlaceKind = Literal["sandbox", "checkout", "worktree"]


def place_kind(project_id: str | None, worktree: bool) -> PlaceKind:
    """What a thread's own place is, from its binding."""
    if project_id is None:
        return "sandbox"
    return "worktree" if worktree else "checkout"

#: Where a conversation's sandbox workspace appears inside the VM. The model is
#: told this, never the host path — the host path means nothing in the guest.
SANDBOX_MOUNT = "/work"

#: Hephaestus' own scratch inside a worktree: staged attachments and opened skill bundles.
#: Excluded from git, so the diff the operator reviews is the agent's work and nothing else.
WORKTREE_SCRATCH = ".hephaestus"


class WorkspaceFiles(Protocol):
    """The two-method slice staging needs (``services/sandbox/staging.py``'s
    ``_Stageable``). Named structurally so a sandbox session satisfies it as-is and a
    plain host directory needs only the small adapter below."""

    def read_file(self, relpath: str) -> bytes: ...

    def write_file(self, relpath: str, content: bytes) -> None: ...


class WorkspaceSession(WorkspaceFiles, Protocol):
    """The slice of a sandbox session that resolving a workspace reads."""

    def ensure_workspace(self) -> Path: ...


class WorkspaceSessions(Protocol):
    """The slice of the sandbox manager that resolving a workspace opens sessions through."""

    async def acquire(self, key: str, *, holder: LiveWork | None = None) -> WorkspaceSession: ...


class HostFiles:
    """`WorkspaceFiles` over a plain directory — the worktree's side of staging.

    Containment is the sandbox's own check, reused rather than re-derived: a staged
    filename is operator content and must not be able to write outside the tree.
    """

    def __init__(self, root: Path) -> None:
        self._root: Path = root

    def read_file(self, relpath: str) -> bytes:
        target = contained_path(self._root, relpath)
        if not target.is_file():
            raise SandboxError(f"no such file in the workspace: {relpath!r}")
        return target.read_bytes()

    def write_file(self, relpath: str, content: bytes) -> None:
        target = contained_path(self._root, relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_bytes(content)


@dataclass(frozen=True)
class RunWorkspace:
    """This run's workspace: the host directory, and how a path in it is named aloud."""

    root: Path
    kind: PlaceKind
    files: WorkspaceFiles
    #: The branch a worktree run is working on; None in a sandbox.
    branch: str | None = None
    #: Where the agent's home holds this folder (``~/projects/<slug>`` or
    #: ``~/worktrees/<slug>``); None in a sandbox.
    home_path: str | None = None

    @property
    def stage_prefix(self) -> str:
        """Where staged files go, relative to the root. A project folder keeps them out of
        the operator's diff; a sandbox workspace is ours entirely and needs no prefix."""
        return "" if self.kind == "sandbox" else f"{WORKTREE_SCRATCH}/"

    def display(self, relpath: str) -> str:
        """The path string the model is given for ``relpath``.

        The one place a workspace path is put in the home's terms, so a marker, a tool
        result and a staged skill can never name the same file three different ways.
        """
        rel = relpath.lstrip("/")
        if self.kind == "sandbox":
            return f"{SANDBOX_MOUNT}/{rel}"
        if self.home_path is not None:
            return f"{self.home_path}/{rel}"
        return str(self.root / relpath)


async def sandbox_workspace(
    sessions: WorkspaceSessions, workspace_key: str, *, holder: LiveWork | None = None
) -> RunWorkspace:
    """The conversation's sandbox workspace. Raises `SandboxError` when it can't be
    opened (the VM is down, a locked vault, an unreadable seal) — the caller degrades.

    ``holder`` is the run this workspace is for, and passing it is what keeps the
    live-session cap from displacing a session the run is still working in between two
    of its tool calls. The files would survive that — a reap leaves them alone — but the
    live process and system state around them would not.

    A key naming a delegation is a sub-agent in a worktree of its own, which shares the
    scratch of the thread it delegates from."""
    session = await sessions.acquire(scratch_key(workspace_key), holder=holder)
    return RunWorkspace(root=session.ensure_workspace(), kind="sandbox", files=session)


#: What separates a workspace's owner from one delegation of it in a workspace key.
DELEGATION_SEP = "/"


def split_workspace_key(workspace_key: str) -> tuple[str, str | None]:
    """A workspace key as *whose* workspace, and *which delegation* of it.

    ``"c123"`` → ``("c123", None)``: the conversation's own.
    ``"c123/w7"`` → ``("c123", "w7")``: a sub-agent of it working in worktree ``w7``.
    """
    owner, separator, delegation = workspace_key.partition(DELEGATION_SEP)
    return owner, (delegation or None) if separator else None


def scratch_key(workspace_key: str) -> str:
    """The key of the scratch a workspace key works in: a delegation shares its owner's."""
    return split_workspace_key(workspace_key)[0]


async def worktree_workspace(
    *,
    projects: ProjectStore,
    worktrees: WorktreeManager,
    owner_id: str,
    project_id: str,
    conversation_id: str,
    delegation_id: str | None = None,
) -> RunWorkspace:
    """The project's git worktree, with this conversation's branch checked out.

    Idempotent per conversation, and refused (`WorktreeBusyError`) while another code
    conversation holds the project — one checkout, one thread at a time.

    ``delegation_id`` names a sub-agent's own worktree instead, cut when it was spawned
    and named under the conversation above.
    """
    project = await projects.get(owner_id, project_id)
    root = Path(project.root_path)
    home_path = await project_home_path(projects, owner_id, project_id, group="worktrees")
    if delegation_id is not None:
        state = worktrees.reopen_child(
            project_id=project_id,
            conversation_id=conversation_id,
            delegation_id=delegation_id,
        )
        prepare_worktree_workspace(state.path)
        return RunWorkspace(
            root=state.path,
            kind="worktree",
            files=HostFiles(state.path),
            branch=state.branch,
            home_path=home_path,
        )
    state = await worktrees.acquire(
        project_id=project_id,
        root=root,
        base_ref=project.base_ref,
        conversation_id=conversation_id,
    )
    prepare_worktree_workspace(state.path)
    return RunWorkspace(
        root=state.path,
        kind="worktree",
        files=HostFiles(state.path),
        branch=state.branch,
        home_path=home_path,
    )


async def project_home_path(
    projects: ProjectStore,
    owner_id: str,
    project_id: str,
    *,
    group: Literal["projects", "worktrees"] = "projects",
) -> str | None:
    """Where the agent's home holds this project, or a thread's worktree of it, or None
    when it has no slug."""
    slug = (await projects.slugs(owner_id)).get(project_id)
    return f"{HOME}/{group}/{slug}" if slug else None


def prepare_worktree_workspace(path: Path) -> None:
    """Keep Hephaestus' own scratch out of the operator's diff.

    The scratch directory ignores *itself*, with a `.gitignore` containing `*` — which
    covers that `.gitignore` too, so the whole directory disappears from `git status` and
    from `git add -A`. Deliberately not `.git/info/exclude`: for a linked worktree that
    file resolves to the **common** git directory, meaning the operator's own repository
    metadata, and this feature's entire premise is that we do not write there.

    Best-effort: failing to write it is untidy (the agent might commit its own scratch),
    never a reason to refuse the turn.
    """
    try:
        scratch = path / WORKTREE_SCRATCH
        scratch.mkdir(parents=True, exist_ok=True)
        ignore = scratch / ".gitignore"
        if not ignore.is_file():
            _ = ignore.write_text("# Hephaestus' own scratch — never the operator's work.\n*\n")
    except OSError:
        logger.debug("workspace: could not exclude %s from git", WORKTREE_SCRATCH, exc_info=True)


async def resolve_workspace(
    *,
    project_id: str | None,
    worktree: bool,
    conversation_id: str | None,
    workspace_key: str,
    owner_id: str,
    sessions: SandboxSessionManager | None,
    projects: ProjectStore | None,
    worktrees: WorktreeManager | None,
    holder: LiveWork | None = None,
) -> RunWorkspace | None:
    """This run's own place, or None when there isn't one: its worktree, else the project
    it is filed under, else its scratch.

    Takes explicit handles rather than a `RunContext` because it is called from both
    sides of a turn: from inside a run (through `tools/workspace.py`) and at composition
    time, before the run exists, to stage attachments. One resolution, two entry points.

    Returns None rather than raising, for the two shapes of "no workspace" the callers
    already handle: a project without the pieces it needs, and a sandbox that will not
    open. A busy worktree is *not* one of those — that is a real conflict the operator has
    to see, so `WorktreeBusyError` propagates.
    """
    if project_id and not worktree:
        if projects is None:
            return None
        try:
            project = await projects.get(owner_id, project_id)
        except Exception:
            logger.warning("workspace: could not open the project folder", exc_info=True)
            return None
        root = Path(project.root_path)
        if not root.is_dir():
            return None
        prepare_worktree_workspace(root)
        code, out, _ = await run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = out.strip() if code == 0 and out.strip() else None
        return RunWorkspace(
            root=root,
            kind="checkout",
            files=HostFiles(root),
            branch=branch,
            home_path=await project_home_path(projects, owner_id, project_id),
        )
    if project_id and worktree:
        # Never fall back to the sandbox. A worktree run that quietly got a sandbox
        # workspace would edit files its shell tools are refused access to, and nothing
        # would say why — the failure the one-workspace rule exists to prevent. No
        # worktree means no workspace, and the tool layer says so in words.
        if projects is None or worktrees is None or not conversation_id:
            return None
        # Which checkout, read off the workspace key rather than off the conversation.
        # For every ordinary run the two say the same thing (a run's key *is* its
        # conversation), so this changes nothing for them; a delegated run is the case
        # where they differ, and it is the whole reason the key is a field of its own.
        owner_conversation, delegation_id = split_workspace_key(workspace_key)
        try:
            return await worktree_workspace(
                projects=projects,
                worktrees=worktrees,
                owner_id=owner_id,
                project_id=project_id,
                conversation_id=owner_conversation or conversation_id,
                delegation_id=delegation_id,
            )
        except WorktreeBusyError:
            raise
        except Exception:
            # A missing project or a git failure degrades to "no workspace" the same way
            # an unavailable sandbox does; the tool layer says so in words.
            logger.warning("workspace: could not open the project worktree", exc_info=True)
            return None
    if sessions is None:
        return None
    try:
        return await sandbox_workspace(sessions, workspace_key, holder=holder)
    except SandboxError:
        logger.debug("workspace: no sandbox workspace available", exc_info=True)
        return None
