"""Filesystem tools over the agent's home.

``pydantic_ai_harness.FileSystem`` supplies the eight tools, with their binary detection,
hash-checked edits and ``ModelRetry`` errors. They act on ``ctx.workspace``, whose file
half is the run's home on the host (``tools/home_workspace.py``); its backend
(``services/home/backend.py``) is the containment boundary, and no VM is needed. Paths
are read from the home, so a result's path can be passed back
as written; a path naming no folder of the home lands in the thread's own place, its
project or else its scratch.

A write into a project the thread may only read asks the operator first, and an approval
leases that project to the thread, so the next write there does not ask again. A write
to the operator's own folder of a project is recorded so they can put the file back
(``services/home/changes.py``), and the first result in a project carries its instruction
files (``tools/briefing.py``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast, override

from pydantic_ai import AbstractToolset, RunContext, Tool, ToolsetTool, WrapperToolset
from pydantic_ai.exceptions import ApprovalRequired, ModelRetry
from pydantic_ai_harness import FileSystem
from pydantic_ai_harness.filesystem import FileSystemToolset

from core.config import get_settings
from core.diagnostics import render_tool_error
from core.untrusted import wrap_untrusted
from services.home.backend import Mount
from services.home.paths import HOME, SCRATCH, absolute, climbs_out
from services.home.table import RunHome
from services.projects.worktree import WorktreeBusyError

from .briefing import with_brief
from .deps import RunDeps
from .describe import registered_tools
from .harness_events import attributable
from .home import change_log, lease, may_lease, resolve_run_home
from .run_home import home_of
from .spill import is_untrusted_spill
from .workspace import unavailable

#: The most characters one ``read_file`` page returns, so a page of a long file, or of a
#: saved tool result, cannot flood the context.
READ_PAGE_CHARS = 40_000

#: The tools that change a file, by their bare names.
WRITE_TOOLS = frozenset({"write_file", "edit_file", "create_directory"})

#: The same, namespaced: the conditionally gated names this category contributes.
GATED_TOOLS = frozenset(f"files_{name}" for name in WRITE_TOOLS)

#: The writes that change a file's content, which a revert can put back.
_FILE_WRITES = frozenset({"write_file", "edit_file"})

#: A successful text read opens with the harness's `[path | N lines | hash:…]` header; a
#: binary read names its type and size instead, and a failure has no header.
_TEXT_READ = re.compile(r"\A\[[^\n]* \| \d+ lines[ \]]")

#: The tools that walk a folder, and the spellings that name no folder in particular.
_WALKS = frozenset({"list_directory", "search_files", "find_files"})
_HERE = frozenset({"", ".", "./"})

#: The home's group folders a relative path may name by dropping its `~/`.
_GROUPS = frozenset({"projects", "worktrees", "shared"})


def _delegate_read_only(place: str) -> str:
    return render_tool_error(
        f"{place} is read-only to you",
        hints=[
            "you may write what the agent that spawned you may, and it cannot write here "
            + "either; finish what you can and say in your report what needs writing, so "
            + "it can ask the operator"
        ],
    )


@dataclass
class _HomeFiles(WrapperToolset[RunDeps]):
    """The harness file tools on ``ctx.workspace``, whose file half is the run's home on
    the host (``tools/home_workspace.py``)."""

    @property
    def tools(self) -> Mapping[str, Tool[RunDeps]]:
        """The static registry ``tools/catalog.py`` enumerates for the settings surface."""
        return registered_tools(self.wrapped)

    @override
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, object],
        ctx: RunContext[RunDeps],
        tool: ToolsetTool[RunDeps],
    ) -> object:
        try:
            home = await resolve_run_home(ctx.deps)
        except WorktreeBusyError as exc:
            return str(exc)
        if home is None:
            return unavailable(ctx.deps)
        tool_args = _in_home_terms(name, tool_args, home)
        path = tool_args.get("path")
        if name in WRITE_TOOLS and isinstance(path, str):
            refusal, leased = await _clear_write(ctx, home, path)
            if refusal is not None:
                return refusal
            if leased:
                # The lease is on the run's memo, which the workspace reads its home from.
                home = await resolve_run_home(ctx.deps) or home
        located = home.locate(path) if isinstance(path, str) else None
        log = change_log(ctx.deps) if name in _FILE_WRITES and located is not None else None
        if log is not None and located is not None:
            await log.before(located[0], [located[1]])
        inward = attributable(ctx, f"files_{name}")
        result = cast(object, await super().call_tool(name, tool_args, inward, tool))
        if log is not None and located is not None:
            await log.after(located[0], [located[1]])
        if located is None:
            return result
        mount, rel = located
        if name == "read_file" and _is_fenced(mount, rel):
            return wrap_untrusted(str(result), source="saved tool output")
        return await with_brief(ctx, home, [mount.place], result)


@dataclass(frozen=True)
class FileRead:
    """A home file read outside a tool call, as ``files_read_file`` would have returned it."""

    path: str
    host: str
    text: str


async def read_again(deps: RunDeps, path: str, *, max_chars: int) -> FileRead | None:
    """``path`` (as a file tool call spelled it) read through the run's home, at most
    ``max_chars`` long, or ``None`` when the home or the file is gone, or the file is an
    image or PDF."""
    home = await home_of(deps)
    if home is None:
        return None
    try:
        path = str(_in_home_terms("read_file", {"path": path}, home)["path"])
        located = home.locate(path)
    except ModelRetry:
        return None
    if located is None:
        return None
    reader = FileSystem[RunDeps](
        root_dir=HOME,
        max_read_lines=get_settings().sandbox_files_max_read_lines,
        max_read_chars=max_chars,
    ).get_toolset()
    if not isinstance(reader, FileSystemToolset):
        return None
    result = await reader.read_file(path, workspace=home.backend)
    if not isinstance(result, str) or not _TEXT_READ.match(result):
        return None
    mount, rel = located
    text = wrap_untrusted(result, source="saved tool output") if _is_fenced(mount, rel) else result
    return FileRead(path=path, host=str(mount.root / rel), text=text)


def _is_fenced(mount: Mount, rel: str) -> bool:
    """Whether a read of this file reaches the model fenced: a saved untrusted tool output."""
    return mount.place == SCRATCH and is_untrusted_spill(rel)


def _in_home_terms(
    name: str, tool_args: dict[str, object], home: RunHome | None
) -> dict[str, object]:
    """The call's ``path`` as the absolute home path it names.

    Spelled out here because the harness checks a path against ``~`` before the home sees
    it, reading ``~`` as a folder name and ``/work`` as outside it. A relative path is
    read from the thread's own place, so ``projects/x`` in a project is that project's own
    folder, unless nothing is there and it names a place of the home: models drop the
    ``~/``. A listing or search with no folder named walks the thread's own place.
    """
    path = tool_args.get("path")
    if path is not None and not isinstance(path, str):
        return tool_args
    text = (path or "").strip()
    if text.startswith(("~", "/")):
        return {**tool_args, "path": _spelled(text, cwd=HOME)}
    if not text and name not in _WALKS:
        return tool_args
    if home is None:
        return tool_args
    if text in _HERE:
        return {**tool_args, "path": home.default.path}
    own = _spelled(text, cwd=home.default.path)
    head = text.split("/", 1)[0]
    at_home = absolute(text, cwd=HOME)
    if (
        head in _GROUPS
        and not _exists(home, f"{home.default.path}/{head}")
        and home.locate(at_home) is not None
    ):
        return {**tool_args, "path": at_home}
    return {**tool_args, "path": own}


def _exists(home: RunHome, path: str) -> bool:
    located = home.locate(path)
    return located is not None and (located[0].root / located[1]).exists()


def _spelled(text: str, *, cwd: str) -> str:
    """``text`` as an absolute home path; one whose ``..`` leaves its place is sent back."""
    climb = climbs_out(text, cwd=cwd)
    if climb is not None:
        hints = [climb.hint] if climb.hint else []
        raise ModelRetry(render_tool_error(str(climb), hints=hints))
    return absolute(text, cwd=cwd)


async def _clear_write(
    ctx: RunContext[RunDeps], home: RunHome, path: str
) -> tuple[str | None, bool]:
    """Ask before a write into a place the thread may only read; lease it once approved.
    Returns the refusal, if any, and whether a lease was taken."""
    place = home.place_of(path)
    if place is None or home.writable(place):
        return None, False
    if not may_lease(ctx.deps):
        return _delegate_read_only(place.path), False
    if not ctx.tool_call_approved:
        raise ApprovalRequired()
    _ = await lease(ctx.deps, home, place)
    return None, True


def files_toolset() -> AbstractToolset[RunDeps]:
    """The ``files`` category, built once at app assembly and shared by every run."""
    toolset = FileSystem[RunDeps](
        root_dir=HOME,
        max_read_lines=get_settings().sandbox_files_max_read_lines,
        max_read_chars=READ_PAGE_CHARS,
    ).get_toolset()
    assert toolset is not None
    return _HomeFiles(toolset)
