"""The worktree branch surface — review what a worktree conversation changed, then land it.

A worktree thread works on `heph/<conversation-id>` in a worktree beside the project, never
in the operator's own checkout. These three endpoints are how that work gets back:

- ``GET /worktrees/{conversation_id}`` — the diffstat, the patch, the **per-file rows with
  their risk verdict**, and how far the branch has drifted from the project's base ref.
- ``POST .../merge`` — **the one operation that writes the operator's tree**. It is not
  approval-gated in the agent sense because it cannot be: the agent never calls it. The
  operator pressing MERGE *is* the approval, which is why this is a route and not a tool.
- ``POST .../discard`` — throw the branch away.

Keyed by conversation rather than by branch name, because that is what the operator is
looking at when they decide. camelCase out, matching the projects surface beside it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from core.exceptions import NotFoundError
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.projects import ProjectView, WorktreeError, branch_for

router = APIRouter(prefix="/worktrees", tags=["projects"])


class FileChangeOut(CamelModel):
    """One file in the diff, carrying the verdict the merge gate renders.

    ``category`` and ``risk`` are decided in :mod:`services.projects.diffstat`, and so is
    the order these arrive in. The screen shows them; it must not derive them — a client
    that decided for itself whether ``uv.lock`` is a dependency change would be a second
    answer to a question this codebase already answers once.
    """

    path: str
    #: Where a rename came from; null otherwise.
    old_path: str | None = None
    #: ``added`` · ``modified`` · ``deleted`` · ``renamed`` · ``copied`` ·
    #: ``type-changed`` · ``unmerged``.
    status: str
    insertions: int
    deletions: int
    #: Git could not count lines, so the two counts above are zero and mean nothing.
    binary: bool
    #: ``dependency`` · ``infrastructure`` · ``config`` · ``code`` · ``test`` ·
    #: ``asset`` · ``docs``.
    category: str
    #: ``high`` · ``elevated`` · ``normal``.
    risk: str
    #: The one-phrase reason for the band, authored server-side and shown verbatim.
    reason: str


class BranchOut(CamelModel):
    conversation_id: str
    project_id: str
    branch: str
    base_ref: str
    files_changed: int
    insertions: int
    deletions: int
    patch: str
    #: Whether this conversation currently holds the project's single checkout. False
    #: means another worktree thread has it — the branch still exists and is still
    #: mergeable, it just isn't the one checked out right now.
    active: bool
    #: The per-file rows, pre-ordered by how much the operator should look at each. Empty
    #: for a thread with no branch yet, exactly like the counts above.
    files: list[FileChangeOut] = []
    #: Commits this branch has that the base does not.
    ahead: int = 0
    #: Commits the base has that this branch does not — staleness. A branch reviewed
    #: against a base that has moved on is the failure mode this number exists to name.
    behind: int = 0
    #: The branch tip's committer date, ISO 8601 with offset; null when there is no
    #: branch. A string rather than a datetime because it is git's own field, passed
    #: through rather than re-derived.
    last_commit_at: str | None = None


class MergedOut(CamelModel):
    merged: bool
    detail: str


async def _resolve(request: Request, conversation_id: str) -> tuple[ProjectView, Path]:
    """The thread's project and its root path, or a 4xx explaining which half is missing."""
    binding = await deps.store(request).binding(conversation_id)
    if not binding.worktree or not binding.project_id:
        raise HTTPException(status_code=404, detail="not a worktree conversation")
    try:
        project = await deps.projects(request).get(OPERATOR_ID, binding.project_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return project, Path(project.root_path)


@router.get("/{conversation_id}", response_model=BranchOut)
async def read_branch(request: Request, conversation_id: str) -> BranchOut:
    project, root = await _resolve(request, conversation_id)
    worktrees = deps.worktrees(request)
    try:
        diff = await worktrees.diff(
            root,
            base_ref=project.base_ref,
            conversation_id=conversation_id,
            project_id=project.id,
        )
    except WorktreeError:
        # No branch yet (a worktree thread that hasn't touched a file), or a project
        # directory that has moved out from under us. An empty diff is the honest answer
        # for both, and far better than a 500 on every render of the chat header.
        diff = None
    return BranchOut(
        conversation_id=conversation_id,
        project_id=project.id,
        branch=branch_for(conversation_id),
        base_ref=project.base_ref,
        files_changed=diff.files_changed if diff else 0,
        insertions=diff.insertions if diff else 0,
        deletions=diff.deletions if diff else 0,
        patch=diff.patch if diff else "",
        active=worktrees.holder(project.id) == conversation_id,
        files=[
            FileChangeOut(
                path=change.path,
                old_path=change.old_path,
                status=change.status,
                insertions=change.insertions,
                deletions=change.deletions,
                binary=change.binary,
                category=change.category,
                risk=change.risk,
                reason=change.reason,
            )
            for change in (diff.files if diff else [])
        ],
        ahead=diff.ahead if diff else 0,
        behind=diff.behind if diff else 0,
        last_commit_at=diff.last_commit_at if diff else None,
    )


@router.post("/{conversation_id}/merge", response_model=MergedOut)
async def merge_branch(request: Request, conversation_id: str) -> MergedOut:
    project, root = await _resolve(request, conversation_id)
    try:
        detail = await deps.worktrees(request).merge(
            root,
            base_ref=project.base_ref,
            conversation_id=conversation_id,
            project_id=project.id,
        )
    except WorktreeError as exc:
        # A conflict, or a working tree standing on the wrong branch. Both are git's own
        # message or ours about git, and both are the operator's to resolve — handing the
        # text back verbatim beats paraphrasing it into something less actionable.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Merged, the thread goes on working in the operator's own folder.
    await deps.store(request).close_worktree(conversation_id)
    return MergedOut(merged=True, detail=detail.strip())


@router.post("/{conversation_id}/discard", status_code=204)
async def discard_branch(request: Request, conversation_id: str) -> None:
    project, root = await _resolve(request, conversation_id)
    await deps.worktrees(request).discard(
        root, project_id=project.id, conversation_id=conversation_id
    )
    await deps.store(request).close_worktree(conversation_id)
