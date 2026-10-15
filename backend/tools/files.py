"""Filesystem tools over the agent's home.

``pydantic_ai_harness.FileSystem`` supplies the eight tools, with their binary detection,
hash-checked edits and ``ModelRetry`` errors. We supply the workspace each call acts on:
the run's home (``tools/home.py``), whose backend (``services/home/backend.py``) is the
containment boundary. Paths are read from the home, so a result's path can be passed back
as written; a path naming no folder of the home lands in the thread's own place, its
project or else its scratch.

A write into a project the thread may only read asks the operator first, and an approval
leases that project to the thread, so the next write there does not ask again.
"""

from __future__ import annotations

from dataclasses import replace
from typing import override

from pydantic_ai import AbstractToolset, RunContext, ToolsetTool
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.workspaces import Workspace
from pydantic_ai_harness import FileSystem

from core.config import get_settings
from services.home.paths import HOME, absolute
from services.projects.worktree import WorktreeBusyError

from .deps import RunDeps
from .home import lease, resolve_run_home
from .rebound import ReboundToolset
from .workspace import unavailable

#: The tools that change a file, by their bare names.
WRITE_TOOLS = frozenset({"write_file", "edit_file", "create_directory"})

#: The same, namespaced: the conditionally gated names this category contributes.
GATED_TOOLS = frozenset(f"files_{name}" for name in WRITE_TOOLS)

#: The tools that walk a folder, and the spellings that name no folder in particular.
_WALKS = frozenset({"list_directory", "search_files", "find_files"})
_HERE = frozenset({"", ".", "./"})

_DELEGATE_READ_ONLY = (
    "error: {place} is read-only to you\n"
    "help: a delegated agent cannot be given new write access; write in ~/scratch and "
    "report the change for the agent that delegated to you to make"
)


class _HomeFiles(ReboundToolset):
    @override
    async def resolve(
        self, name: str, ctx: RunContext[RunDeps]
    ) -> tuple[AbstractToolset[RunDeps], RunContext[RunDeps]] | str:
        try:
            home = await resolve_run_home(ctx.deps)
        except WorktreeBusyError as exc:
            return str(exc)
        if home is None:
            return unavailable(ctx.deps)
        return self._template, replace(ctx, workspace=Workspace(home.backend))

    @override
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, object],
        ctx: RunContext[RunDeps],
        tool: ToolsetTool[RunDeps],
    ) -> object:
        tool_args = await _in_home_terms(ctx, name, tool_args)
        path = tool_args.get("path")
        if name in WRITE_TOOLS and isinstance(path, str):
            refusal = await _clear_write(ctx, path)
            if refusal is not None:
                return refusal
        return await super().call_tool(name, tool_args, ctx, tool)


async def _in_home_terms(
    ctx: RunContext[RunDeps], name: str, tool_args: dict[str, object]
) -> dict[str, object]:
    """The call's ``path`` as the home reads it.

    ``~`` is expanded here because the workspace joins a relative path onto its working
    directory, which would read ``~`` as a folder name. A listing or search with no folder
    named walks the thread's own place, not every project in the home.
    """
    path = tool_args.get("path")
    if isinstance(path, str) and path.strip().startswith("~"):
        return {**tool_args, "path": absolute(path, cwd=HOME)}
    if name in _WALKS and (path is None or (isinstance(path, str) and path.strip() in _HERE)):
        try:
            home = await resolve_run_home(ctx.deps)
        except WorktreeBusyError:
            return tool_args
        if home is not None:
            return {**tool_args, "path": home.default.path}
    return tool_args


async def _clear_write(ctx: RunContext[RunDeps], path: str) -> str | None:
    """Ask before a write into a place the thread may only read; lease it once approved."""
    try:
        home = await resolve_run_home(ctx.deps)
    except WorktreeBusyError:
        return None
    if home is None:
        return None
    place = home.place_of(path)
    if place is None or home.writable(place):
        return None
    if ctx.deps.delegated_approved:
        return _DELEGATE_READ_ONLY.format(place=place.path)
    if not ctx.tool_call_approved:
        raise ApprovalRequired()
    _ = await lease(ctx.deps, home, place)
    return None


def files_toolset() -> AbstractToolset[RunDeps]:
    """The ``files`` category, built once at app assembly and shared by every run."""
    toolset = FileSystem[RunDeps](
        root_dir=HOME,
        max_read_lines=get_settings().sandbox_files_max_read_lines,
    ).get_toolset()
    assert toolset is not None
    return _HomeFiles("files", toolset)
