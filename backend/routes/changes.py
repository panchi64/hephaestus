"""What a thread changed in the operator's own project folders, and putting it back.

- ``GET /conversations/{id}/changes`` — the files, by project, and whether the thread
  works in a worktree (whose branch ``/worktrees/{id}`` reviews and merges).
- ``GET /conversations/{id}/changes/{change_id}/diff`` — one file, before against now.
- ``POST /conversations/{id}/changes/revert`` — put files back, every one or those named.
- ``GET /conversations/{id}/changes/rewind?message_id=`` — a dry run of putting every file
  back to before the turn holding that message and the turns after it.
- ``POST /conversations/{id}/changes/rewind`` — do it, once the preview token still matches.

camelCase out, matching the projects surface beside it.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from core.exceptions import NotFoundError
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.home.changes import ChangeStatus, RewindPlan, ThreadChangeFile, ThreadChanges
from services.sandbox.base import SandboxError, contained_path

router = APIRouter(prefix="/conversations", tags=["projects"])


class ChangedFileOut(CamelModel):
    id: str
    path: str
    status: ChangeStatus
    reverted: bool
    #: Whether the original was kept, so the file can be put back.
    revertible: bool


class ProjectChangesOut(CamelModel):
    project_id: str
    #: The project's name, and its folder in the agent's home.
    name: str
    home_path: str
    files: list[ChangedFileOut]


class ChangesOut(CamelModel):
    projects: list[ProjectChangesOut]
    #: Whether the thread works in a worktree of its project.
    worktree: bool


class ChangeDiffOut(CamelModel):
    path: str
    #: A unified diff of the file before the thread's first change against it now; null
    #: for a file that is not text.
    patch: str | None


class RevertIn(CamelModel):
    #: The changes to put back; absent puts back every one.
    ids: list[str] | None = None


class SkippedOut(CamelModel):
    path: str
    reason: str


class RevertOut(CamelModel):
    reverted: list[str]
    skipped: list[SkippedOut]


class RewindIn(CamelModel):
    message_id: str
    preview_token: str


class RewindFileOut(CamelModel):
    project_id: str
    path: str


class RewindConflictOut(CamelModel):
    project_id: str
    path: str
    reason: str


class RewindOut(CamelModel):
    #: Files that would be (or were) put back, and files that would be deleted because the
    #: turns created them.
    restore: list[RewindFileOut]
    delete: list[RewindFileOut]
    #: Files left alone because they changed since the turns last wrote them.
    conflicts: list[RewindConflictOut]
    #: Files whose original was too large to keep.
    unrestorable: list[RewindFileOut]
    #: Names the plan and each file's current content; the confirming call returns it.
    preview_token: str


def _changes(request: Request) -> ThreadChanges:
    return deps.capabilities(request).get(ThreadChanges)


async def _roots(request: Request) -> dict[str, Path]:
    return await deps.projects(request).workspace_roots(OPERATOR_ID)


@router.get("/{conversation_id}/changes", response_model=ChangesOut)
async def list_changes(request: Request, conversation_id: str) -> ChangesOut:
    if not await deps.store(request).exists(conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    changes = await _changes(request).list(OPERATOR_ID, conversation_id)
    projects = deps.projects(request)
    names = {view.id: view.name for view in await projects.list(OPERATOR_ID, include_archived=True)}
    slugs = await projects.slugs(OPERATOR_ID)
    grouped: dict[str, list[ChangedFileOut]] = {}
    for change in changes:
        grouped.setdefault(change.project_id, []).append(
            ChangedFileOut(
                id=change.id,
                path=change.path,
                status=change.status,
                reverted=change.reverted,
                revertible=change.revertible,
            )
        )
    binding = await deps.store(request).binding(conversation_id)
    return ChangesOut(
        projects=[
            ProjectChangesOut(
                project_id=project_id,
                name=names.get(project_id, "a deleted project"),
                home_path=f"~/projects/{slugs[project_id]}" if project_id in slugs else "",
                files=files,
            )
            for project_id, files in grouped.items()
        ],
        worktree=binding.worktree,
    )


@router.get("/{conversation_id}/changes/{change_id}/diff", response_model=ChangeDiffOut)
async def change_diff(request: Request, conversation_id: str, change_id: str) -> ChangeDiffOut:
    try:
        file, before = await _changes(request).original(OPERATOR_ID, conversation_id, change_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    root = (await _roots(request)).get(file.project_id)
    after: bytes | None = None
    if root is not None:
        try:
            target = contained_path(root, file.path)
        except SandboxError:
            target = None
        if target is not None and target.is_file():
            after = target.read_bytes()
    return ChangeDiffOut(path=file.path, patch=_patch(file.path, before, after))


@router.post("/{conversation_id}/changes/revert", response_model=RevertOut)
async def revert_changes(request: Request, conversation_id: str, body: RevertIn) -> RevertOut:
    """Put the thread's changes back. A file that changed again since the thread last
    wrote it is left as it is, and said so."""
    if not await deps.store(request).exists(conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    ids = None if body.ids is None else set(body.ids)
    report = await _changes(request).revert(
        OPERATOR_ID, conversation_id, await _roots(request), ids
    )
    return RevertOut(
        reverted=report.reverted,
        skipped=[SkippedOut(path=path, reason=reason) for path, reason in report.skipped],
    )


def _patch(path: str, before: bytes | None, after: bytes | None) -> str | None:
    try:
        old = (before or b"").decode("utf-8").splitlines(keepends=True)
        new = (after or b"").decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None
    lines = difflib.unified_diff(
        old,
        new,
        fromfile="/dev/null" if before is None else f"a/{path}",
        tofile="/dev/null" if after is None else f"b/{path}",
    )
    return "".join(lines)


def _rewind_out(plan: RewindPlan) -> RewindOut:
    def files(listed: list[ThreadChangeFile]) -> list[RewindFileOut]:
        return [RewindFileOut(project_id=f.project_id, path=f.path) for f in listed]

    return RewindOut(
        restore=files(plan.restore),
        delete=files(plan.delete),
        conflicts=[
            RewindConflictOut(project_id=c.project_id, path=c.path, reason=c.reason)
            for c in plan.conflicts
        ],
        unrestorable=files(plan.unrestorable),
        preview_token=plan.preview_token,
    )


async def rewind_prompt_ids(request: Request, conversation_id: str, message_id: str) -> list[str]:
    """The prompts whose file changes rewinding to ``message_id`` takes back, read from the
    active branch as it is now."""
    ids = await deps.store(request).turn_prompt_ids(conversation_id, message_id)
    if ids is None:
        raise NotFoundError("message not found")
    return ids


async def rewind_files(
    request: Request, conversation_id: str, message_id: str, preview_token: str
) -> RewindPlan:
    """Put the files back to before ``message_id``'s turn, if the preview still holds."""
    prompt_ids = await rewind_prompt_ids(request, conversation_id, message_id)
    return await _changes(request).rewind_files(
        OPERATOR_ID, conversation_id, prompt_ids, await _roots(request), preview_token
    )


@router.get("/{conversation_id}/changes/rewind", response_model=RewindOut)
async def preview_rewind(request: Request, conversation_id: str, message_id: str) -> RewindOut:
    """What rewinding the files to before this message's turn would do. Writes nothing."""
    if not await deps.store(request).exists(conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    prompt_ids = await rewind_prompt_ids(request, conversation_id, message_id)
    plan = await _changes(request).rewind_plan(
        OPERATOR_ID, conversation_id, prompt_ids, await _roots(request)
    )
    return _rewind_out(plan)


@router.post("/{conversation_id}/changes/rewind", response_model=RewindOut)
async def confirm_rewind(request: Request, conversation_id: str, body: RewindIn) -> RewindOut:
    """Put the files back as previewed. 409 when a file or the conversation changed since."""
    if not await deps.store(request).exists(conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    plan = await rewind_files(request, conversation_id, body.message_id, body.preview_token)
    return _rewind_out(plan)
