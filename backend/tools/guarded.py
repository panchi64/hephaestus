"""The toolset that stops a guarded call the permission gate never deferred.

At a level that asks nothing, or for a call the level does not gate, nothing defers a
call before it runs, so ``agent/gating.py`` never sees it. This wrapper raises the
deferral itself, with the guard's reason as metadata, and the call is then ruled on as any
deferred one is (``services/permissions/guards.py``). A call the operator already approved
re-enters with ``tool_call_approved`` set and passes.

It sits inside the recovering wrapper and outside the namespaced stack, so a nested
``run_code`` call, which goes through the same stack, is stopped the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast, override

from pydantic import JsonValue
from pydantic_ai import ApprovalRequired, RunContext
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from core.config import get_settings
from services.home.table import RunHome
from services.permissions.guards import GUARD_KEY, GUARDED_TOOLS, guard
from services.projects.worktree import WorktreeBusyError
from services.sandbox.thread_shells import ThreadShells

from .deps import RunDeps
from .files import unread_refusal
from .home import resolve_run_home

_FILE_REWRITES = frozenset({"files_write_file", "files_edit_file"})


def shell_cwd(deps: RunDeps) -> str | None:
    """Where the thread's shell stands, or None before its first command."""
    shells = deps.caps.get_optional(ThreadShells)
    return shells.cwd(deps.workspace_key) if shells is not None else None


async def guard_home(deps: RunDeps) -> RunHome | None:
    """The run's home, which the guard measures paths against; None when it has none."""
    try:
        return await resolve_run_home(deps)
    except WorktreeBusyError:
        return None


@dataclass
class GuardedToolset(WrapperToolset[RunDeps]):
    @override
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, JsonValue],
        ctx: RunContext[RunDeps],
        tool: ToolsetTool[RunDeps],
    ) -> object:
        if name in GUARDED_TOOLS and not ctx.tool_call_approved:
            if name in _FILE_REWRITES:
                # Refused before it is put to the operator: nobody is asked to approve a
                # change the read ledger would turn away.
                unread = await unread_refusal(ctx.deps, name, dict(tool_args))
                if unread is not None:
                    return unread
            reason = guard(
                name,
                tool_args,
                home=await guard_home(ctx.deps),
                settings=get_settings(),
                cwd=shell_cwd(ctx.deps),
            )
            if reason is not None:
                raise ApprovalRequired(metadata={GUARD_KEY: reason})
        return cast(object, await self.wrapped.call_tool(name, tool_args, ctx, tool))
