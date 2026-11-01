"""Every agent an owner can run, resolved from all its sources.

Precedence is locality of intent: a stored agent (what the operator or an agent wrote here)
shadows a project's file of the same name, which shadows a built-in, which shadows one a
published plugin contributes. The default agent is never shadowed, so a thread that names
none always runs what ships.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from pathlib import Path

from services.agent_models import AgentModels
from services.agents.builtin import BUILTIN, builtin_roster, merged_roster
from services.agents.definition import DEFAULT_AGENT, AgentDefinition
from services.agents.files import project_agents
from services.agents.store import AgentStore

type AgentSource = Callable[[str], Awaitable[Iterable[AgentDefinition]]]


class AgentCatalog:
    def __init__(self, store: AgentStore, models: AgentModels | None = None) -> None:
        self._store: AgentStore = store
        self._models: AgentModels | None = models
        self._contributors: list[AgentSource] = []

    async def pinned(
        self, owner_id: str, agents: Iterable[AgentDefinition]
    ) -> list[AgentDefinition]:
        """``agents`` with the operator's pins applied to those that have no row to hold one.
        The default agent is never pinned: it runs on ``main``."""
        pins = await self._models.pins(owner_id) if self._models is not None else {}
        out: list[AgentDefinition] = []
        for agent in agents:
            pin = pins.get(f"{agent.source}:{agent.name}")
            if pin is None or agent.id is not None or agent.name == DEFAULT_AGENT:
                out.append(agent)
            else:
                out.append(replace(agent, endpoint_id=pin.endpoint_id, model=pin.model))
        return out

    def contribute(self, source: AgentSource) -> None:
        """Add agents another feature supplies per owner, below every other source."""
        self._contributors.append(source)

    async def contributed(self, owner_id: str) -> list[AgentDefinition]:
        return [agent for source in self._contributors for agent in await source(owner_id)]

    async def agents(
        self,
        owner_id: str,
        *,
        project_root: Path | None = None,
        include_drafts: bool = False,
    ) -> dict[str, AgentDefinition]:
        """The agents by name. Drafts are included only for the operator's own listing."""
        stored = await self._store.list(owner_id, published_only=not include_drafts)
        declared = project_agents(project_root) if project_root is not None else ()
        # One layer in precedence order, so the pins are read once.
        unrowed = await self.pinned(
            owner_id, (*await self.contributed(owner_id), *BUILTIN, *declared)
        )
        roster = merged_roster(unrowed, stored)
        roster[DEFAULT_AGENT] = builtin_roster()[DEFAULT_AGENT]
        return roster

    async def resolve(
        self, owner_id: str, name: str | None, *, project_root: Path | None = None
    ) -> AgentDefinition:
        """The agent a thread runs: ``name`` if it resolves to a published agent, else the
        default."""
        roster = await self.agents(owner_id, project_root=project_root)
        return roster.get(name or DEFAULT_AGENT) or roster[DEFAULT_AGENT]


def lower_of[T: (int, float)](asked: T | None, bound: T | None) -> T | None:
    """The smaller of what an agent asked for and what it is allowed; None on either side
    means that side set nothing."""
    if asked is None:
        return bound
    if bound is None:
        return asked
    return min(asked, bound)
