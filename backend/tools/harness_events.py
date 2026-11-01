"""Owning the events the harness's own toolsets emit.

The `pydantic_ai_harness` toolsets registered here directly (rather than through their
capabilities, which would put their tools outside the namespaced, toggleable catalog) emit
`CapabilityEvent`s, and Pydantic AI refuses one it cannot attribute to a registered
capability. Attribution needs two things:

1. **A registered owner**: :func:`harness_events_capability`, an empty `Hooks`. Nothing
   consumes the harness's events; ours are matched in `agent/translate.py` by `isinstance`.
2. **A `capability_id` on the tool definition**, looked up by the dispatched (namespaced)
   tool name. :func:`own_harness_events` stamps it and `tools/toolsets.py` applies it to
   the whole catalog. That is safe because the field gates a tool only when its owner sets
   `defer_loading=True`, which this one never does.

Because every catalog tool is stamped, each counts as capability-owned, so the owner must
allow application `CustomEvent`s or every tool's `ctx.emit` (`tools/emit.py`) raises.
`Hooks` is the library's public app-facing capability that allows them.
"""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, Hooks
from pydantic_ai.tools import ToolDefinition

from .deps import RunDeps

#: The stamp and the owner's registered id must agree: an unknown stamp counts as no owner.
HARNESS_EVENTS_CAPABILITY_ID = "harness"


def harness_events_capability() -> AbstractCapability[RunDeps]:
    """The owner to register on the agent."""
    return Hooks[RunDeps](id=HARNESS_EVENTS_CAPABILITY_ID)


def own_harness_events(tool_def: ToolDefinition) -> ToolDefinition:
    """Stamp a tool definition with the owner id, unless it already names an owner."""
    if tool_def.capability_id is not None:
        return tool_def
    return replace(tool_def, capability_id=HARNESS_EVENTS_CAPABILITY_ID)


def attributable(ctx: RunContext[RunDeps], namespaced_name: str) -> RunContext[RunDeps]:
    """``ctx`` with `tool_name` restated as the catalog names it.

    A `PrefixedToolset` hands its inner toolset the unprefixed name, and events stamped
    from ``ctx.tool_name`` must carry the catalog's.
    """
    return replace(ctx, tool_name=namespaced_name)
