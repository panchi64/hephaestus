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
from pydantic_ai.exceptions import ApprovalRequired, ModelRetry
from pydantic_ai.workspaces import Workspace
from pydantic_ai_harness import FileSystem

from core.config import get_settings
from core.diagnostics import render_tool_error
from services.home.paths import HOME, absolute, climbs_out
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


def _delegate_read_only(place: str) -> str:
    return render_tool_error(
        f"{place} is read-only to you",
        hints=[
            "a delegated agent cannot be given new write access; write in ~/scratch and "
            + "report the change for the agent that delegated to you to make"
        ],
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
    """The call's ``path`` as the absolute home path it names.

    Spelled out here because the harness checks a path against ``~`` before the home sees
    it, reading ``~`` as a folder name and ``/work`` as outside it. A relative path is
    read from the thread's own place, so ``projects/x`` in a project is that project's own
    folder, and a listing or search with no folder named walks that place.
    """
    path = tool_args.get("path")
    if path is not None and not isinstance(path, str):
        return tool_args
    text = (path or "").strip()
    if text.startswith(("~", "/")):
        return {**tool_args, "path": _spelled(text, cwd=HOME)}
    if not text and name not in _WALKS:
        return tool_args
    try:
        home = await resolve_run_home(ctx.deps)
    except WorktreeBusyError:
        return tool_args
    if home is None:
        return tool_args
    if text in _HERE:
        return {**tool_args, "path": home.default.path}
    return {**tool_args, "path": _spelled(text, cwd=home.default.path)}


def _spelled(text: str, *, cwd: str) -> str:
    """``text`` as an absolute home path; one whose ``..`` leaves its place is sent back."""
    climb = climbs_out(text, cwd=cwd)
    if climb is not None:
        hints = [climb.hint] if climb.hint else []
        raise ModelRetry(render_tool_error(str(climb), hints=hints))
    return absolute(text, cwd=cwd)


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
        return _delegate_read_only(place.path)
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
