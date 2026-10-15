"""Which places a thread's home holds, on which folders, and which it may write."""

from __future__ import annotations

from pathlib import Path

from services.projects.store import ProjectStore

from .backend import Mount
from .leases import WriteLeases, place_key
from .paths import SCRATCH, Place


async def thread_mounts(
    *,
    owner_id: str,
    conversation_id: str | None,
    scratch: Path,
    projects: ProjectStore | None,
    leases: WriteLeases | None,
    filed_project_id: str | None = None,
) -> list[Mount]:
    """The thread's scratch, writable, then every registered project.

    A project is writable to a thread that holds a lease on it, or that was started in it:
    starting a thread in a project is the operator saying the agent works there, which is
    what a project thread has always meant.
    """
    mounts = [Mount(SCRATCH, scratch, writable=True)]
    if projects is None:
        return mounts
    slugs = await projects.slugs(owner_id)
    roots = await projects.workspace_roots(owner_id)
    held = (
        await leases.held(owner_id, conversation_id)
        if leases is not None and conversation_id
        else set[str]()
    )
    for project_id, slug in sorted(slugs.items(), key=lambda item: item[1]):
        root = roots.get(project_id)
        if root is None or not slug:
            continue
        place = Place("projects", slug)
        writable = project_id == filed_project_id or place_key(place) in held
        mounts.append(Mount(place, root, writable=writable))
    return mounts
