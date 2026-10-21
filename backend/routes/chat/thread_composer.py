"""What an unattended turn on an existing thread is composed from, in one place.

A routine's fire, the turn an agent's delivery wakes and a sub-agent's turn all resolve the
same things: the agent the thread runs, the model that agent names, the tools withheld from
a run nobody watches, and the instructions its brief adds. They resolve them here so the
turn they submit and the park they are asked to rebuild after a restart (``agent/recompose``)
cannot disagree about any of it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic_ai import AbstractToolset

from agent.engine import recompose_turn
from agent.recompose import ParkedFacts, Recomposed, RecomposeError
from agent.summarize import resolve_auto_compact_policy
from core.config import Settings
from core.container import ServiceContainer
from routes.chat.turn import TurnModels, resolve_turn_models
from services.agents import AgentCatalog, AgentDefinition
from services.agents.thread import DELEGATION_TOOLS, ThreadAgent, may_delegate, resolve_thread_agent
from services.conversations import ConversationStore
from services.offline import OfflineModeService
from services.projects import ProjectStore, WorktreeManager
from services.registry import ModelRegistry
from services.settings_store import SettingsStore, get_context_thresholds
from services.tool_policy import CategoryAvailability, effective_disabled_tools
from tools import InstructionProvider, RunDeps


@dataclass(frozen=True)
class ThreadTurn:
    """The agent a thread runs, the model it names, and the instructions its turn carries."""

    applied: ThreadAgent
    models: TurnModels
    instruction_providers: tuple[InstructionProvider, ...]


@dataclass(frozen=True)
class ThreadComposer:
    agents: AgentCatalog
    model_registry: ModelRegistry
    conversations: ConversationStore
    settings_store: SettingsStore
    offline: OfflineModeService
    settings: Settings
    capabilities: ServiceContainer
    categories: Mapping[str, AbstractToolset[RunDeps]]
    dormant: Mapping[str, str]
    instruction_providers: Sequence[InstructionProvider]
    availability: Sequence[CategoryAvailability]
    projects: ProjectStore | None = None
    worktrees: WorktreeManager | None = None

    async def resolve(self, owner_id: str, conversation_id: str) -> ThreadTurn:
        """The thread's agent and the model it names. Raises when either is gone."""
        applied = await resolve_thread_agent(
            self.agents,
            owner_id,
            await self.conversations.binding(conversation_id),
            conversation_id,
            projects=self.projects,
            worktrees=self.worktrees,
        )
        models = await resolve_turn_models(
            self.model_registry, applied.agent.endpoint_id, applied.agent.model, owner_id=owner_id
        )
        return ThreadTurn(applied, models, (*self.instruction_providers, *applied.instructions))

    async def disabled(
        self, owner_id: str, *, kind: str, vision: bool, agent: AgentDefinition, depth: int
    ) -> frozenset[str]:
        """What is withheld from a run of ``kind`` ``depth`` levels below its tree's root:
        the operator's switches, and delegation where ``agent`` may not delegate."""
        disabled = await effective_disabled_tools(
            self.settings_store,
            self.offline,
            owner_id,
            vision=vision,
            kind=kind,
            availability=self.availability,
            caps=self.capabilities,
        )
        if not may_delegate(agent, depth, self.settings.agent_max_depth):
            disabled |= DELEGATION_TOOLS
        return disabled

    async def recompose(self, facts: ParkedFacts, *, kind: str, depth: int) -> Recomposed:
        """The agent and compaction context a parked turn of ``kind`` resumes with."""
        conversation_id = facts.conversation_id
        if conversation_id is None:
            raise RecomposeError("this run had no conversation to resume in")
        owner_id = facts.run.owner_id
        try:
            thread = await self.resolve(owner_id, conversation_id)
        except HTTPException as exc:
            raise RecomposeError(str(exc.detail)) from exc
        models = thread.models
        return await recompose_turn(
            facts,
            model=models.main,
            categories=self.categories,
            dormant=self.dormant,
            instruction_providers=thread.instruction_providers,
            capabilities=self.capabilities,
            store=self.conversations,
            disabled_tools=await self.disabled(
                owner_id, kind=kind, vision=models.vision, agent=thread.applied.agent, depth=depth
            ),
            auto_compact=await resolve_auto_compact_policy(self.settings_store, owner_id),
            context_window=models.context_window,
            context_thresholds=await get_context_thresholds(self.settings_store, owner_id),
            title_model=models.utility,
            title_settings=models.utility_settings,
            endpoint=models.endpoint,
        )
