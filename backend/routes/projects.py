"""The projects surface — the operator's working directories and the active selection.

Thin over `services/projects`. Two shapes deserve a note:

`repo` on every listing is **probed live**, not stored. Whether a directory is a git
repository, and how many changes are uncommitted, are facts about the world that change
without us; caching them would mean showing the operator a stale answer to the one
question that decides whether a worktree is safe to open.

`uncommittedChanges` is surfaced deliberately, and the UI is expected to show it. A
worktree branches from the project's base ref, so uncommitted work in the
operator's own tree is **invisible to the agent**. That is the price of never touching
their tree, and it should be read on the project screen rather than discovered halfway
through a session.

camelCase out, matching the `corpus`/`uploads`/`tasks` surfaces the frontend
seams were built against.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from core.exceptions import InvalidInputError, NotFoundError
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from routes.http import content_disposition
from services.artifacts import guess_content_type
from services.projects import ProjectView, WorktreeError
from services.projects.listing import list_files, read_file

router = APIRouter(prefix="/projects", tags=["projects"])


class RepoOut(CamelModel):
    exists: bool
    is_git_repo: bool
    uncommitted_changes: int | None = None
    current_branch: str | None = None


class ProjectOut(CamelModel):
    id: str
    name: str
    root_path: str
    git_initialized: bool
    base_ref: str
    archived: bool
    created_at: datetime
    last_opened_at: datetime
    repo: RepoOut


class FileEntryOut(CamelModel):
    #: Relative to the listed root, forward slashes, and the string the operator's
    #: reference carries — so it resolves the same way wherever the run's files are.
    path: str
    name: str


class FilesOut(CamelModel):
    #: Which filesystem answered. `worktree` is the thread's own branch checkout;
    #: `project` is the operator's checkout, which is what a thread gets before its
    #: first turn has created a worktree.
    root: str
    entries: list[FileEntryOut]
    #: The scan hit its bound. Said out loud rather than implying the tree is this small.
    truncated: bool


class ProjectCreate(CamelModel):
    name: str = ""
    root_path: str


class ProjectEnsure(CamelModel):
    """A directory, with no name — the name is the directory's own. Naming a project is a
    thing the operator may do later, not a form standing between them and starting work."""

    root_path: str


class ProjectUpdate(CamelModel):
    name: str | None = None
    base_ref: str | None = None
    archived: bool | None = None


class ProjectsOut(CamelModel):
    projects: list[ProjectOut]
    #: The operator's current selection, or null when nothing is active — which means
    #: they see exactly the unfiled rows they saw before projects existed.
    active_id: str | None = None


def _out(view: ProjectView) -> ProjectOut:
    return ProjectOut(
        id=view.id,
        name=view.name,
        root_path=view.root_path,
        git_initialized=view.git_initialized,
        base_ref=view.base_ref,
        archived=view.archived,
        created_at=view.created_at,
        last_opened_at=view.last_opened_at,
        repo=RepoOut(
            exists=view.probe.exists,
            is_git_repo=view.probe.is_git_repo,
            uncommitted_changes=view.probe.uncommitted_changes,
            current_branch=view.probe.current_branch,
        ),
    )


@router.get("", response_model=ProjectsOut)
async def list_projects(request: Request, include_archived: bool = False) -> ProjectsOut:
    store = deps.projects(request)
    views = await store.list(OPERATOR_ID, include_archived=include_archived)
    return ProjectsOut(
        projects=[_out(v) for v in views],
        active_id=await store.active_id(OPERATOR_ID),
    )


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(request: Request, body: ProjectCreate) -> ProjectOut:
    try:
        return _out(await deps.projects(request).create(OPERATOR_ID, body.name, body.root_path))
    except InvalidInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/ensure", response_model=ProjectOut)
async def ensure_project(request: Request, body: ProjectEnsure) -> ProjectOut:
    """The project for a directory, created on first use.

    What lets a code session start from *any* directory instead of from a project the
    operator had to file first. It is idempotent by the directory itself, so pointing at
    the same folder twice returns the same project and the same worktree machinery rather
    than a second row cutting a second branch from one repository.

    Declared **before** ``/{project_id}``: FastAPI matches in declaration order, so the
    dynamic route would otherwise swallow ``ensure`` as an id and answer 404.
    """
    try:
        return _out(await deps.projects(request).ensure_for_path(OPERATOR_ID, body.root_path))
    except InvalidInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(request: Request, project_id: str) -> ProjectOut:
    try:
        return _out(await deps.projects(request).get(OPERATOR_ID, project_id))
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{project_id}/files", response_model=FilesOut)
async def list_project_files(
    request: Request,
    project_id: str,
    conversation_id: str | None = None,
    query: str = "",
    limit: int = 200,
) -> FilesOut:
    """The files a worktree thread can reference — what the composer's `@` picker browses.

    **Rooted on the project, upgraded by the conversation.** `conversation_id` is
    optional because the operator types `@` in a composer that may not have started a
    thread yet; naming one asks for that thread's own worktree, and the resolver hands
    back the project root whenever there isn't one. The response says which answered, so
    the picker can label a listing that is the operator's checkout rather than the
    agent's view of it.

    It **never creates a worktree**: acquiring the project's single checkout as a side
    effect of typing a character would take it from whatever else holds it.

    Not on `/worktrees`, which is the natural-looking home: that router resolves a
    conversation to its project and 404s until a thread is saved with a binding, which is
    precisely the moment the picker is most useful.
    """
    # Through the shared resolver rather than repeating it: the `/` picker and the send
    # that resolves a picked command read the same pair, and three copies of "which tree is
    # this thread looking at" is three chances for them to disagree. The 404 stays here,
    # because a listing for a project that does not exist is a request to refuse — where
    # for the two pickers it is a keystroke, and an empty menu is the right answer.
    root, from_worktree = await deps.composer_tree(request, project_id, conversation_id)
    if root is None:
        raise HTTPException(status_code=404, detail="project not found")
    listing = await list_files(root, query=query, limit=max(1, min(limit, 500)))
    return FilesOut(
        root="worktree" if from_worktree else "project",
        entries=[FileEntryOut(path=e.path, name=e.name) for e in listing.entries],
        truncated=listing.truncated,
    )


#: How much of a file the operator's viewer will be handed. A worktree thread's tree is
#: browsable now, so a path can be asked for directly rather than only picked out of a
#: listing that already honours `.gitignore` — and a worktree holds whatever the agent
#: built in it. Generous for source, far under what would make the response the problem.
_FILE_MAX_BYTES = 2 * 1024 * 1024


@router.get("/{project_id}/file")
async def read_project_file(
    request: Request,
    project_id: str,
    path: str,
    conversation_id: str | None = None,
) -> Response:
    """One file out of the tree `list_files` just listed, for the operator to read.

    **The same resolver, deliberately.** It answers for whichever checkout
    :func:`~routes.deps.composer_tree` names — the thread's own worktree when it has one,
    the project root otherwise — because a viewer reading from a different tree than the
    list it was picked from is the failure mode worth spending a shared resolver on.

    **It never creates a worktree**, for the same reason the listing does not: opening a
    file must not acquire the project's single checkout from whatever holds it.

    A path that escapes the root, names a symlink out of it, names a directory, or simply
    is not there all come back **404**. That is one answer to four questions on purpose —
    distinguishing them would describe the filesystem around a path the operator is not
    being allowed to read.

    Served with the same inert headers as a View's bytes: a sandboxing CSP and `nosniff`,
    so a file out of a repository cannot execute anything in the operator's origin.
    """
    root, _from_worktree = await deps.composer_tree(request, project_id, conversation_id)
    if root is None:
        raise HTTPException(status_code=404, detail="project not found")
    content = await read_file(root, path, max_bytes=_FILE_MAX_BYTES)
    if content is None:
        raise HTTPException(status_code=404, detail="file not found")
    name = path.rsplit("/", 1)[-1]
    headers = {
        "Content-Security-Policy": (
            "sandbox; default-src 'none'; img-src 'self' data: blob:; "
            "style-src 'unsafe-inline'; font-src 'self' data:"
        ),
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": content_disposition(name, inline=True, fallback="file"),
        # Stated rather than implied: a viewer showing the first two megabytes of a file
        # has to be able to say so, and a header is the one place that fact can ride
        # alongside raw bytes.
        "X-Content-Truncated": "true" if content.truncated else "false",
    }
    return Response(
        content=content.data,
        media_type=guess_content_type(name),
        headers=headers,
    )


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(request: Request, project_id: str, body: ProjectUpdate) -> ProjectOut:
    try:
        return _out(
            await deps.projects(request).update(
                OPERATOR_ID,
                project_id,
                name=body.name,
                base_ref=body.base_ref,
                archived=body.archived,
            )
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{project_id}", status_code=204)
async def delete_project(request: Request, project_id: str) -> None:
    try:
        await deps.projects(request).delete(OPERATOR_ID, project_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{project_id}/init-repo", response_model=ProjectOut)
async def init_repo(request: Request, project_id: str) -> ProjectOut:
    """Make the project's directory a git repository, with the operator's explicit yes.

    A worktree needs one — a worktree is cut from it — but running `git init` and
    committing someone's whole directory is a real, visible side effect, so it is never
    implicit. This route *is* the confirmation: the UI asks, the operator answers, and
    only then does anything happen. The agent has no path to it.
    """
    store = deps.projects(request)
    try:
        project = await store.get(OPERATOR_ID, project_id)
        created = await deps.worktrees(request).ensure_repo(Path(project.root_path), confirmed=True)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidInputError, WorktreeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if created:
        await store.update(OPERATOR_ID, project_id, git_initialized=True)
    return _out(await store.get(OPERATOR_ID, project_id))


@router.post("/{project_id}/activate", response_model=ProjectsOut)
async def activate_project(request: Request, project_id: str) -> ProjectsOut:
    """Make this the operator's active project. Returns the whole listing, because the
    selection changes what every other surface will return and the client should reseat
    from one shape rather than patching a local guess."""
    store = deps.projects(request)
    try:
        await store.activate(OPERATOR_ID, project_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await list_projects(request)


@router.post("/deactivate", response_model=ProjectsOut)
async def deactivate_project(request: Request) -> ProjectsOut:
    """Clear the selection — the ALL PROJECTS state, which is the app's original
    behavior and not an empty one."""
    await deps.projects(request).activate(OPERATOR_ID, None)
    return await list_projects(request)
