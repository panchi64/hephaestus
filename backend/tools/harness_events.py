"""Owning the events the harness's own toolsets emit.

Several `pydantic_ai_harness` capabilities — `planning`, `filesystem`, `subagents`,
`compaction`, `system_reminders`, `spend` — emit `CapabilityEvent`s from inside their tool
functions. This codebase registers those toolsets **directly** rather than registering the
capabilities that own them, deliberately: a capability contributes its tools itself, which
would put them outside the namespaced, operator-toggleable catalog whose whole promise is
that the settings list and the agent's real stack cannot diverge.

Pydantic AI now refuses a `CapabilityEvent` it cannot attribute to a registered capability,
which turned that choice into a crash: the harness emits, the library cannot name an owner,
and the `UserError` takes the turn with it. Attribution needs two things, and a lifted
toolset has neither:

1. **A registered capability to attribute to.** :func:`harness_events_capability` is that,
   and it is deliberately empty — no tools, no instructions, no hooks — so it cannot
   reintroduce the tools or the prompt injection that made the capability form the rejected
   shape. One owner covers every lifted toolset, because the id is an attribution handle
   rather than a claim about behaviour, and nothing here consumes these events: the
   product's own frames are emitted by `services/` and matched in `agent/translate.py` by
   `isinstance`, so the harness's fall through unread.

2. **A `capability_id` on the tool definition the call arrived through**, which the library
   reads off `tool_manager.tools[ctx.tool_name]`. :func:`own_harness_events` stamps it, and
   `tools/toolsets.py` applies that to the whole catalog — safe for every tool rather than
   only the lifted ones, because both places the field gates a tool require the owning
   capability to have `defer_loading=True`, and this owner never does.

The second half has a wrinkle that :func:`attributable` closes. That registry is keyed by
the *namespaced* name (`tasks_write`), but a `PrefixedToolset` rewrites `ctx.tool_name` to
the *unprefixed* one (`write`) before delegating inward — so the lookup misses for every
prefixed toolset, which is all of ours. A wrapper that delegates to a harness toolset
restates the namespaced name on the way in.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import ToolDefinition

from .deps import RunDeps

#: The id every tool definition is stamped with, and the id the owner below is registered
#: under. The two have to agree — the library looks the stamp up among the run's registered
#: capabilities and treats an unknown one as no owner at all.
HARNESS_EVENTS_CAPABILITY_ID = "harness"


@dataclass
class _HarnessEventOwner(AbstractCapability[RunDeps]):
    """The registered owner for a lifted harness toolset's events, and nothing else."""

    id: str | None = HARNESS_EVENTS_CAPABILITY_ID


def harness_events_capability() -> AbstractCapability[RunDeps]:
    """The owner to register on the agent (see the module docstring)."""
    return _HarnessEventOwner()


def own_harness_events(tool_def: ToolDefinition) -> ToolDefinition:
    """Stamp one tool definition with the owning capability id.

    Left alone where a definition already names an owner: a genuinely capability-contributed
    tool knows better than this default does.
    """
    if tool_def.capability_id is not None:
        return tool_def
    return replace(tool_def, capability_id=HARNESS_EVENTS_CAPABILITY_ID)


def attributable(ctx: RunContext[RunDeps], namespaced_name: str) -> RunContext[RunDeps]:
    """``ctx`` with the tool named as the *catalog* names it.

    Call this on the context handed to a harness toolset, so an event it emits can be
    attributed. Nothing else on these paths reads ``tool_name``: the harness dispatches on
    the name passed alongside it, and resolves its stores through our own resolvers.
    """
    return replace(ctx, tool_name=namespaced_name)
