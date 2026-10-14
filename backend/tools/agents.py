"""The `agents` category — the model writing agents of its own, and delegating to them
(the delegation half is ``tools/delegation.py``).

**What an agent writes is a draft.** A draft runs nowhere: the catalog every launch and
every thread resolves through lists published agents only, so creating one needs no
approval. Publishing it, or changing one that is already published, puts instructions in
front of future turns, so both are the operator's to allow.
"""

from __future__ import annotations

from typing import Literal

from pydantic_ai import AbstractToolset, FunctionToolset, ModelRetry, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from core.exceptions import AgentValidationError
from services.agents import AgentStore, ToolAccess
from services.permissions import PermissionLevel

from .delegation import GATED_TOOLS as DELEGATION_GATED
from .delegation import ProjectRoster, add_delegation_tools
from .deps import RunDeps

#: Raised for approval only when the agent being rewritten is published, or when the agent
#: being spawned can write.
GATED_TOOLS: frozenset[str] = frozenset({"agents_write"}) | DELEGATION_GATED

_UNAVAILABLE = "Agents cannot be written in this deployment."

Workspace = Literal["shared", "isolated", "own"]


def agents_toolset() -> AbstractToolset[RunDeps]:
    toolset = FunctionToolset[RunDeps]()

    @toolset.tool
    async def write(
        ctx: RunContext[RunDeps],
        name: str,
        description: str,
        instructions: str,
        tools: list[str] | None = None,
        deny_tools: list[str] | None = None,
        permission_ceiling: PermissionLevel | None = None,
        workspace: Workspace = "shared",
        can_delegate: bool = False,
    ) -> dict[str, object]:
        """Draft an agent with its own standing brief for recurring work, or rewrite one.

        `description` says what it does and when to pick it. `tools` limits it to those
        tools or categories (`files`, `shell`); `deny_tools` withholds more. `workspace` is
        where it works when delegated: your files, a merged-back copy, or its own.

        A new agent is a draft until the operator publishes it.
        """
        store = ctx.deps.caps.get_optional(AgentStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        values: dict[str, object] = {
            "name": name,
            "description": description,
            "instructions": instructions,
            "tools": ToolAccess(
                allow=None if tools is None else frozenset(tools),
                deny=frozenset(deny_tools or ()),
            ),
            "permission_ceiling": permission_ceiling,
            "workspace": workspace,
            "can_delegate": can_delegate,
        }
        existing = await store.get_by_name(ctx.deps.owner_id, name.strip().lower())
        try:
            if existing is None or existing.id is None:
                agent = await store.create(ctx.deps.owner_id, values, source="agent")
            elif not existing.published and existing.source != "agent":
                return {
                    "ok": False,
                    "error": "That draft is the operator's own; leave it for them to finish.",
                }
            elif existing.published and not ctx.tool_call_approved:
                raise ApprovalRequired()
            else:
                agent = await store.update(ctx.deps.owner_id, existing.id, values)
        except AgentValidationError as exc:
            raise ModelRetry(f"That agent was rejected ({exc.field}): {exc}") from None
        return {
            "ok": True,
            "name": agent.name,
            "status": "published" if agent.published else "draft",
        }

    @toolset.tool(requires_approval=True)
    async def publish(ctx: RunContext[RunDeps], name: str) -> dict[str, object]:
        """Ask the operator to publish an agent you drafted so it can run."""
        store = ctx.deps.caps.get_optional(AgentStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        agent = await store.get_by_name(ctx.deps.owner_id, name)
        if agent is None or agent.id is None:
            raise ModelRetry(f"There is no stored agent called {name!r} to publish.")
        _ = await store.set_published(ctx.deps.owner_id, agent.id, True)
        return {"ok": True, "name": agent.name, "status": "published"}

    add_delegation_tools(toolset)
    return ProjectRoster(toolset)
