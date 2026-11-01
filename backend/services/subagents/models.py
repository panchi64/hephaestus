"""Which model a spawned agent runs on.

First match wins: the agent's pin, which is the operator's and is never overridden; the
model the spawning agent asked for, from the operator's choosable list; the ``subagent``
role; the spawning agent's own model.
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from core.exceptions import DegradedCapabilityError, NotFoundError
from services.agent_models import AgentModels, ChoosableModel
from services.agents.definition import AgentDefinition
from services.conversations import ModelChoice
from services.registry import ModelRegistry

type ModelSource = Literal["pin", "spawn", "role", "parent"]


@dataclass(frozen=True)
class SpawnModel:
    choice: ModelChoice
    source: ModelSource


@dataclass(frozen=True)
class OfferedModel:
    """A choosable model whose endpoint can serve it now."""

    #: What the spawning agent passes as ``model``: ``<endpoint name>/<model>``.
    id: str
    entry: ChoosableModel
    provider: str
    local: bool
    context_window: int | None
    vision: bool


def is_pinned(agent: AgentDefinition) -> bool:
    return agent.endpoint_id is not None or agent.model is not None


def spawn_model(
    agent: AgentDefinition,
    requested: ChoosableModel | None,
    *,
    role_bound: bool,
    parent: ModelChoice,
) -> SpawnModel:
    if is_pinned(agent):
        return SpawnModel(ModelChoice(endpoint_id=agent.endpoint_id, model=agent.model), "pin")
    if requested is not None:
        return SpawnModel(
            ModelChoice(endpoint_id=requested.endpoint_id, model=requested.model), "spawn"
        )
    if role_bound:
        return SpawnModel(ModelChoice(role="subagent"), "role")
    return SpawnModel(parent, "parent")


def is_local(base_url: str) -> bool:
    """Whether an endpoint is on this machine or its network: loopback, a private address,
    or a ``.local``/``localhost`` name."""
    host = (urlsplit(base_url).hostname or "").lower()
    if host == "localhost" or host.endswith((".local", ".localhost", ".lan", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


async def offer(
    registry: ModelRegistry, entry: ChoosableModel, owner_id: str
) -> OfferedModel | None:
    """``entry`` as Talos is offered it; None when its endpoint is gone, disabled or can't
    drive tools."""
    try:
        specs = await registry.resolve_specs(
            "main",
            owner_id=owner_id,
            override_endpoint_id=entry.endpoint_id,
            override_model=entry.model,
        )
    except DegradedCapabilityError, NotFoundError:
        return None
    spec = specs[0]
    return OfferedModel(
        id=f"{spec.name}/{entry.model}",
        entry=entry,
        provider=spec.provider,
        local=is_local(spec.base_url),
        context_window=spec.context_window,
        vision=spec.vision,
    )


async def offered(
    registry: ModelRegistry, models: AgentModels, owner_id: str
) -> list[OfferedModel]:
    """The operator's choosable list, less the entries :func:`offer` drops."""
    entries = await models.choosable(owner_id)
    offers = await asyncio.gather(*(offer(registry, entry, owner_id) for entry in entries))
    return [o for o in offers if o is not None]
