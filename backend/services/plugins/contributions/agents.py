"""``agents``: agent definitions from ``agents/<name>.md``, in a project's agent-file format."""

from __future__ import annotations

from dataclasses import replace
from typing import override

from core.exceptions import PluginValidationError
from services.agents.definition import AgentDefinition
from services.agents.files import AgentFileError, AgentNameError, parse_agent_file
from services.plugins.fields import in_folder

from .base import BundleSource, Described, TypedContribution
from .registry import CONTRIBUTIONS


class Agents(TypedContribution[AgentDefinition]):
    @override
    def parse(self, source: BundleSource) -> tuple[AgentDefinition, ...]:
        if source.declared:
            raise PluginValidationError(self.key, "agents come from agents/<name>.md files")
        agents: list[AgentDefinition] = []
        for path, stem, blob in in_folder(source.files or {}, "agents", ".md"):
            try:
                agent = parse_agent_file(blob.decode(), name=stem)
            except (AgentFileError, AgentNameError, UnicodeDecodeError) as exc:
                raise PluginValidationError("agents", f"{path}: {exc}") from None
            agents.append(replace(agent, source="plugin", published=True, id=None))
        if len({agent.name for agent in agents}) != len(agents):
            raise PluginValidationError("agents", "two agents share a name")
        return tuple(agents)

    @override
    def describe_one(self, item: AgentDefinition) -> Described:
        return {"name": item.name}


AGENTS = CONTRIBUTIONS.register(Agents("agents", "Agents", AgentDefinition))
