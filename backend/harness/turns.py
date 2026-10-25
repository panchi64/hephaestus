"""Agent turns a feature starts on its own, with no request behind them.

A routine firing, a woken thread and a spawned sub-agent each compose a turn the way an
interactive one is composed (``compose_turn``), from the same catalog and providers; this is
the part they share, so the three can't drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent.summarize import resolve_auto_compact_policy
from harness.manifest import DormantCategory, HarnessContext
from routes.chat import TurnModels, compose_turn
from routes.chat.schemas import ChatCreated
from routes.chat.thread_composer import ThreadComposer
from runs import RunRegistry
from runs.registry import UNSET, Unset
from services.agents import AgentCatalog
from services.conversations import ConversationBinding, ConversationStore
from services.offline import OfflineModeService
from services.projects import ProjectStore, WorktreeManager
from services.registry import ModelRegistry
from services.settings_store import SettingsStore, get_context_thresholds
from services.uploads import UploadStore
from tools import InstructionProvider


def thread_composer(ctx: HarnessContext) -> ThreadComposer:
    """Resolves a thread's agent, models and withheld tools as a request would."""
    return ThreadComposer(
        agents=ctx.services.get(AgentCatalog),
        model_registry=ctx.services.get(ModelRegistry),
        conversations=ctx.services.get(ConversationStore),
        settings_store=ctx.services.get(SettingsStore),
        offline=ctx.services.get(OfflineModeService),
        settings=ctx.settings,
        capabilities=ctx.capabilities,
        categories=ctx.tool_categories,
        dormant=DormantCategory.summaries(ctx.dormant_categories),
        instruction_providers=ctx.instruction_providers,
        availability=ctx.category_availability,
        projects=ctx.services.get_optional(ProjectStore),
        worktrees=ctx.services.get_optional(WorktreeManager),
    )


class UnattendedTurns:
    """Submits a turn nobody asked for over HTTP, composed from the harness's catalog."""

    def __init__(self, ctx: HarnessContext) -> None:
        self._ctx: HarnessContext = ctx
        self._runs: RunRegistry = ctx.services.get(RunRegistry)
        self._conversations: ConversationStore = ctx.services.get(ConversationStore)
        self._settings: SettingsStore = ctx.services.get(SettingsStore)
        self._uploads: UploadStore = ctx.services.get(UploadStore)
        self.composer: ThreadComposer = thread_composer(ctx)

    async def submit(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        prompt: str,
        kind: str,
        models: TurnModels,
        binding: ConversationBinding,
        disabled_tools: frozenset[str],
        instruction_providers: Sequence[InstructionProvider],
        max_requests: int | None = None,
        wall_clock_timeout_s: float | None | Unset = UNSET,
        ephemeral: bool = False,
        run_id: str | None = None,
        ledger_prompt_message_id: str | None = None,
    ) -> ChatCreated:
        return compose_turn(
            run_id=run_id,
            prompt=prompt,
            conversation_id=conversation_id,
            models=models,
            capabilities=self._ctx.capabilities,
            categories=self._ctx.tool_categories,
            dormant=DormantCategory.summaries(self._ctx.dormant_categories),
            instruction_providers=instruction_providers,
            prompt_context_providers=self._ctx.prompt_context_providers,
            registry=self._runs,
            store=self._conversations,
            uploads=self._uploads,
            disabled_tools=disabled_tools,
            binding=binding,
            context_thresholds=await get_context_thresholds(self._settings, owner_id),
            auto_compact=await resolve_auto_compact_policy(self._settings, owner_id),
            owner_id=owner_id,
            kind=kind,
            max_requests=max_requests,
            wall_clock_timeout_s=wall_clock_timeout_s,
            ephemeral=ephemeral,
            ledger_prompt_message_id=ledger_prompt_message_id,
        )
