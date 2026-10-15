"""Host surface — the two things the operator's own machine does that a browser can't.

Reading a path off the desktop, and acting on one. A browser can do neither: ``<input
type="file">`` hands over bytes with no location, and ``file://`` navigation is blocked
from a page. Both are done by the process running on the operator's machine, so the
chooser's answer comes back as data and the opener's argument goes out as data.

Progressive enhancement, never a requirement. ``GET /host/file-picker`` reports whether
this host can open a chooser at all, and every surface that offers a BROWSE control also
takes a typed path; a host with no opener answers ``POST /host/open`` with a sentence
saying so, and the path is still there to read in the prose that named it.

Both halves are **agent-unreachable by construction** — see ``services/host_picker`` and
``services/host_open`` for the platform helpers and the rule.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes import deps
from routes.deps import OPERATOR_ID
from services import host_open, host_picker
from services.home.paths import HOME, HomePathError, absolute, parse
from services.host_picker import PickerAvailability, PickMode

router = APIRouter(prefix="/host", tags=["host"])


class PickRequest(BaseModel):
    mode: PickMode = "file"
    title: str = "Choose"
    start_dir: str | None = None
    extensions: list[str] | None = None  # bare, e.g. ["gguf"]


class PickResult(BaseModel):
    path: str | None = None  # None ⇒ the operator cancelled the dialog


@router.get("/file-picker", response_model=PickerAvailability)
async def file_picker_availability() -> PickerAvailability:
    """Whether this host can open a native chooser. The path field works either way —
    this only decides whether a BROWSE control is worth offering."""
    return host_picker.probe()


@router.post("/file-picker", response_model=PickResult)
async def open_file_picker(body: PickRequest) -> PickResult:
    """Open a native file/folder dialog on the host and return what was chosen."""
    try:
        path = await host_picker.pick(
            body.mode,
            title=body.title,
            start_dir=body.start_dir,
            extensions=body.extensions,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return PickResult(path=path)


class OpenRequest(BaseModel):
    #: Absolute, or relative to a workspace — the two ways the model spells a file.
    path: str


class OpenResult(BaseModel):
    path: str  # the absolute path that was actually opened


async def _searchable_roots(request: Request) -> list[Path]:
    """Where a path in an answer is allowed to resolve, in the order it is looked for.

    The whole permission model of this route, written as a list. A path arrives from a
    click on model-written prose, so nothing outside these directories may be opened —
    and which of them is tried first decides *which copy* of a file the operator gets:

    * the active project before the rest, then most-recently-opened, because that is
      the work they are in;
    * each project's **worktree** before their own checkout, because a worktree thread's
      files live in the worktree and a file the agent just created exists nowhere else.

    Both come from the cheap lookups (no git probe): a click must not cost N subprocesses.
    """
    projects = deps.projects(request)
    worktrees = deps.worktrees(request)
    roots = await projects.workspace_roots(OPERATOR_ID)
    active = await projects.active_id(OPERATOR_ID)
    # Stable, so the store's most-recently-opened order survives the active-first sort.
    ordered = sorted(roots, key=lambda project_id: project_id != active)
    out: list[Path] = []
    for project_id in ordered:
        worktree = worktrees.path_for(project_id)
        if worktree.is_dir():
            out.append(worktree)
        out.append(roots[project_id])
    return out


async def _home_project(request: Request, path: str) -> tuple[list[Path], str] | None:
    """The folders and in-project path a path in the agent's home names, when it names a
    registered project (``~/projects/<slug>/…``); None for anything else. The project's
    own folder comes first, because that is what ``~/projects/<slug>`` is mounted on."""
    try:
        target = parse(absolute(path, cwd=HOME))
    except HomePathError:
        return None
    if target.place.kind != "projects" or not target.rel:
        return None
    projects = deps.projects(request)
    slugs = await projects.slugs(OPERATOR_ID)
    project_id = next((pid for pid, slug in slugs.items() if slug == target.place.name), None)
    if project_id is None:
        return None
    root = (await projects.workspace_roots(OPERATOR_ID)).get(project_id)
    if root is None:
        return None
    worktree = deps.worktrees(request).path_for(project_id)
    return [root, *([worktree] if worktree.is_dir() else [])], target.rel


@router.post("/open", response_model=OpenResult)
async def open_in_editor(request: Request, body: OpenRequest) -> OpenResult:
    """Open a file from the operator's projects in whatever their machine opens it with.

    Operator-initiated, so it is a plain REST action rather than an approval-gated tool:
    the agent has no path to it, and the only thing it can influence is the *text* of a
    path it wrote — which is exactly why the two fences in ``services/host_open`` are not
    optional. It refuses anything outside the operator's project roots, and anything
    inside them the host would *run* rather than show: the agent writes into those roots,
    so being contained is no evidence that the file at the end is a file to read.
    """
    in_home = await _home_project(request, body.path)
    if in_home is not None:
        roots, rel = in_home
        target = host_open.resolve_within(roots, rel)
    else:
        target = host_open.resolve_within(await _searchable_roots(request), body.path)
    try:
        await host_open.open_path(target)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return OpenResult(path=str(target))
