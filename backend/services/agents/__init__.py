"""Agents: what one is (``definition``), the ones that ship (``builtin``), the ones a project
declares (``files``), the ones the operator and agents write (``store``), and all of them
resolved together (``catalog``)."""

from __future__ import annotations

from services.agents.builtin import (
    BUILTIN,
    EXPLORER,
    RESEARCHER,
    REVIEWER,
    TEST_RUNNER,
    WORKER,
    builtin_roster,
    delegable,
    describe_roster,
    merged_roster,
)
from services.agents.catalog import AgentCatalog, lower_of
from services.agents.definition import (
    DEFAULT_AGENT,
    UNRESTRICTED,
    AgentDefinition,
    AgentSource,
    ToolAccess,
    WorkspacePolicy,
)
from services.agents.files import AgentNameError, normalize_agent_name, project_agents
from services.agents.store import AgentStore

__all__ = [
    "BUILTIN",
    "DEFAULT_AGENT",
    "EXPLORER",
    "RESEARCHER",
    "REVIEWER",
    "TEST_RUNNER",
    "UNRESTRICTED",
    "WORKER",
    "AgentCatalog",
    "AgentDefinition",
    "AgentNameError",
    "AgentSource",
    "AgentStore",
    "ToolAccess",
    "WorkspacePolicy",
    "builtin_roster",
    "delegable",
    "describe_roster",
    "lower_of",
    "merged_roster",
    "normalize_agent_name",
    "project_agents",
]
