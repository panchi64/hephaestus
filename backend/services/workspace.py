"""One answer to "where does this run's file work happen".

The file tools, `code_execute` or the shell, attachment staging and the skills bundler all
act on one resolved value, so the agent never edits a file it cannot run:

    workspace "sandbox"  → the conversation's own workspace, at ``/work`` in the VM
    workspace "checkout" → the project's own folder, on the host, on its current branch
    workspace "worktree" → the project's git worktree, on the host, by absolute path

Which of the two a run gets is the conversation's ``workspace_kind``
(``services/workspace_kind.py``).

:class:`RunWorkspace` carries both halves — the host directory to act on, and
:meth:`RunWorkspace.display`, the path *string the model is told*. Those differ in a
sandbox (host `<data>/sandbox/work/<key>/x` is `/work/x` to the model) and are the same in
a worktree, which is exactly the kind of detail that goes wrong when four modules each
build it themselves.

**Attachments and skills stage under ``.odysseus/`` in a worktree.** They have to land
inside it or the file tools can't reach them, and they must not show up as the agent's
work — so :func:`prepare_worktree_workspace` makes that directory ignore itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from services.projects.checkout import CheckoutHolders
from services.projects.repo import run_git
from services.projects.store import ProjectStore
from services.projects.worktree import WorktreeBusyError, WorktreeManager
from services.sandbox import (
    LiveWork,
    SandboxError,
    SandboxSession,
    SandboxSessionManager,
)
from services.sandbox.base import contained_path
from services.workspace_kind import WorkspaceKind

logger = logging.getLogger(__name__)

#: Where a conversation's sandbox workspace appears inside the VM. The model is
#: told this, never the host path — the host path means nothing in the guest.
SANDBOX_MOUNT = "/work"

#: Odysseus' own scratch inside a worktree: staged attachments and opened skill bundles.
#: Excluded from git, so the diff the operator reviews is the agent's work and nothing else.
WORKTREE_SCRATCH = ".odysseus"


class WorkspaceFiles(Protocol):
    """The two-method slice staging needs (``services/sandbox/staging.py``'s
    ``_Stageable``). Named structurally so a sandbox session satisfies it as-is and a
    plain host directory needs only the small adapter below."""

    def read_file(self, relpath: str) -> bytes: ...

    def write_file(self, relpath: str, content: bytes) -> None: ...


class HostFiles:
    """`WorkspaceFiles` over a plain directory — the worktree's side of staging.

    Containment is the sandbox's own check, reused rather than re-derived: a staged
    filename is operator content and must not be able to write outside the tree.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def read_file(self, relpath: str) -> bytes:
        target = contained_path(self._root, relpath)
        if not target.is_file():
            raise SandboxError(f"no such file in the workspace: {relpath!r}")
        return target.read_bytes()

    def write_file(self, relpath: str, content: bytes) -> None:
        target = contained_path(self._root, relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


@dataclass(frozen=True)
class RunWorkspace:
    """This run's workspace: the host directory, and how a path in it is named aloud."""

    root: Path
    kind: WorkspaceKind
    files: WorkspaceFiles
    #: The branch a worktree run is working on; None in a sandbox.
    branch: str | None = None

    @property
    def stage_prefix(self) -> str:
        """Where staged files go, relative to the root. A project folder keeps them out of
        the operator's diff; a sandbox workspace is ours entirely and needs no prefix."""
        return "" if self.kind == "sandbox" else f"{WORKTREE_SCRATCH}/"

    def display(self, relpath: str) -> str:
        """The path string the model is given for ``relpath``.

        The one place the sandbox's guest path is translated, so a marker, a tool result
        and a staged skill can never name the same file three different ways.
        """
        if self.kind == "sandbox":
            return f"{SANDBOX_MOUNT}/{relpath.lstrip('/')}"
        return str(self.root / relpath)


async def sandbox_workspace(
    sessions: SandboxSessionManager, workspace_key: str, *, holder: LiveWork | None = None
) -> RunWorkspace:
    """The conversation's sandbox workspace. Raises `SandboxError` when it can't be
    opened (the VM is down, a locked vault, an unreadable seal) — the caller degrades.

    ``holder`` is the run this workspace is for, and passing it is what keeps the
    live-session cap from displacing a session the run is still working in between two
    of its tool calls. The files would survive that — a reap leaves them alone — but the
    live process and system state around them would not.

    A key naming a *delegation* resolves to a fork of the workspace it delegates from,
    rather than to a session of its own. Read here rather than left to the caller for the
    reason the worktree half reads it: a delegated run resolves its workspace on every
    file-tool call, and a plain `acquire` on a delegated key would quietly hand back an
    empty workspace — a sub-agent working in a copy of nothing, and a merge at the end
    with no fork to land."""
    owner, delegation = split_workspace_key(workspace_key)
    session = (
        await _delegated_session(sessions, owner, workspace_key, holder=holder)
        if delegation is not None
        else await sessions.acquire(workspace_key, holder=holder)
    )
    return RunWorkspace(root=session.ensure_workspace(), kind="sandbox", files=session)


async def _delegated_session(
    sessions: SandboxSessionManager,
    parent_key: str,
    child_key: str,
    *,
    holder: LiveWork | None,
) -> SandboxSession:
    """A delegated agent's workspace — forked on first use, reopened on every later one.

    ``fork`` is the *event* of taking the copy, and refuses a key it has already forked;
    this is the question a delegated run asks on every file-tool call, and they are not the
    same question. While delegation blocked its parent's turn the two collapsed into one —
    the copy was taken and handed straight to the child inside a single call — but a
    sub-agent runs as its own Run and resolves its workspace the way every run does:
    repeatedly, and from scratch. The counterpart to ``WorktreeManager.open_child``, keyed
    the same way and there for the same reason.

    Claiming the reused fork matters more here than it does for an ordinary session:
    displacing a fork *deletes* it, nothing about a fork being sealed, so a copy nobody has
    claimed is a sub-agent's work the live-session cap may throw away mid-run.
    """
    taken = sessions.existing(child_key)
    if taken is not None and taken.ephemeral:
        taken.touch()
        taken.hold(holder)
        return taken
    # Either nothing yet, or something that is not a fork sitting under this key. `fork`
    # takes the copy in the first case and refuses in the second, which is the honest
    # answer — adopting a non-fork would mean merging a whole conversation's workspace
    # into another's when the sub-agent ends.
    return await sessions.fork(parent_key, child_key, holder=holder)


#: What separates a workspace's owner from one delegation of it in a workspace key.
#:
#: The convention is not new — a forked sandbox has always been keyed ``<parent>/<child>``
#: — but it used to be honoured by one workspace kind and ignored by the other, which is
#: why a delegated run could be given its own workspace and never its own checkout.
DELEGATION_SEP = "/"


def split_workspace_key(workspace_key: str) -> tuple[str, str | None]:
    """A workspace key as *whose* workspace, and *which delegation* of it.

    ``"c123"`` → ``("c123", None)``: the conversation's own.
    ``"c123/w7"`` → ``("c123", "w7")``: a delegated child of it.

    One reading of the key, shared by both kinds, so a sub-agent cannot end up with a
    workspace forked from its parent and a checkout that is its parent's own.
    """
    owner, separator, delegation = workspace_key.partition(DELEGATION_SEP)
    return owner, (delegation or None) if separator else None


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

    ``delegation_id`` asks for a *delegated child* of that checkout instead: its own
    worktree on its own branch, cut from the holder's. The holder is still the
    conversation named above — a fork is one conversation working in two places, not a
    second thread taking the checkout — which is what lets a sub-agent work beside the
    thread that launched it rather than being refused as a rival for it.
    """
    project = await projects.get(owner_id, project_id)
    root = Path(project.root_path)
    if delegation_id is not None:
        state = await worktrees.open_child(
            project_id=project_id,
            root=root,
            conversation_id=conversation_id,
            delegation_id=delegation_id,
        )
        prepare_worktree_workspace(state.path)
        return RunWorkspace(
            root=state.path,
            kind="worktree",
            files=HostFiles(state.path),
            branch=state.branch,
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
    )


def prepare_worktree_workspace(path: Path) -> None:
    """Keep Odysseus' own scratch out of the operator's diff.

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
            ignore.write_text("# Odysseus' own scratch — never the operator's work.\n*\n")
    except OSError:
        logger.debug("workspace: could not exclude %s from git", WORKTREE_SCRATCH, exc_info=True)


async def resolve_workspace(
    *,
    workspace_kind: str,
    project_id: str | None,
    conversation_id: str | None,
    workspace_key: str,
    owner_id: str,
    sessions: SandboxSessionManager | None,
    projects: ProjectStore | None,
    worktrees: WorktreeManager | None,
    checkouts: CheckoutHolders | None = None,
    holder: LiveWork | None = None,
) -> RunWorkspace | None:
    """This run's workspace, or None when there isn't one.

    Takes explicit handles rather than a `RunContext` because it is called from both
    sides of a turn: from inside a run (through `tools/workspace.py`) and at composition
    time, before the run exists, to stage attachments. One resolution, two entry points.

    Returns None rather than raising, for the two shapes of "no workspace" the callers
    already handle: a project workspace without the pieces it needs, and a sandbox that
    will not open. A busy worktree or checkout is *not* one of those — that is a real
    conflict the operator has to see, so `WorktreeBusyError` propagates.
    """
    if workspace_kind == "checkout":
        if projects is None or checkouts is None or not project_id or not conversation_id:
            return None
        owner_conversation, _ = split_workspace_key(workspace_key)
        try:
            project = await projects.get(owner_id, project_id)
        except Exception:
            logger.warning("workspace: could not open the project folder", exc_info=True)
            return None
        root = Path(project.root_path)
        if not root.is_dir():
            return None
        await checkouts.acquire(project_id, owner_conversation or conversation_id, holder)
        prepare_worktree_workspace(root)
        code, out, _ = await run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = out.strip() if code == 0 and out.strip() else None
        return RunWorkspace(root=root, kind="checkout", files=HostFiles(root), branch=branch)
    if workspace_kind == "worktree":
        # Never fall back to the sandbox. A worktree run that quietly got a sandbox
        # workspace would edit files its shell tools are refused access to, and nothing
        # would say why — the failure the one-workspace rule exists to prevent. No
        # worktree means no workspace, and the tool layer says so in words.
        if projects is None or worktrees is None or not project_id or not conversation_id:
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
