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
from typing import override

from pydantic import JsonValue
from pydantic_ai import ApprovalRequired, RunContext
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from core.config import get_settings
from services.permissions.guards import GUARD_KEY, GUARDED_TOOLS, guard
from services.projects.worktree import WorktreeBusyError

from .deps import RunDeps
from .home import resolve_run_home


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
            try:
                home = await resolve_run_home(ctx.deps)
            except WorktreeBusyError:
                home = None
            reason = guard(name, tool_args, home=home, settings=get_settings())
            if reason is not None:
                raise ApprovalRequired(metadata={GUARD_KEY: reason})
        return await self.wrapped.call_tool(name, tool_args, ctx, tool)
