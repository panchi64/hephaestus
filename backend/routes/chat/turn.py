"""Building a chat turn's Run from already-resolved parts: model resolution and the one
orchestrator-build-and-submit step (``compose_turn``).

Request-agnostic, so a non-HTTP caller (the scheduler's agent-task executor, a
sub-agent's wake) drives an ordinary turn through the same path a route does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NamedTuple, TypedDict

from fastapi import HTTPException
from pydantic_ai import AbstractToolset
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from agent import build_chat_orchestrator
from agent.summarize import AutoCompactPolicy
from core.config import get_settings
from core.container import ServiceContainer
from core.exceptions import DegradedCapabilityError, NotFoundError
from routes import deps
from routes.chat.schemas import ChatCreated
from routes.deps import OPERATOR_ID
from runs import (
    DEFAULT_CONTEXT_THRESHOLDS,
    ContextThresholds,
    ConversationBusyError,
    Endpoint,
    RunRegistry,
)
from runs.registry import UNSET, Unset
from services.commands import Invocation
from services.conversations import ConversationBinding, ConversationStore
from services.registry import ModelRegistry
from services.uploads import UploadStore
from tools import NO_DISABLED_TOOLS, InstructionProvider, PromptContextProvider, RunDeps


class TurnModels(NamedTuple):
    """What :func:`resolve_turn_models` resolves."""

    main: Model
    utility: Model
    utility_settings: ModelSettings | None
    context_window: int | None
    vision: bool
    #: The main model's primary endpoint, which the turn's run leases.
    endpoint: Endpoint | None = None


class WallClockOverride(TypedDict, total=False):
    """``compose_turn`` keyword arguments for a caller that sets the wall clock only
    sometimes: absent means the registry default, which ``None`` would not."""

    wall_clock_timeout_s: float | None


async def resolve_turn_models(
    model_registry: ModelRegistry,
    endpoint_id: str | None,
    model: str | None,
    *,
    owner_id: str = OPERATOR_ID,
) -> TurnModels:
    """Resolve the `main` model plus the background (utility/title) pair, raising a clear
    4xx/503 on misconfiguration.

    Returns the main model, the background model with its reasoning-off settings, the main
    model's context window and vision. The compaction summary is written by the main model,
    so the operator's own fold resolves through here too.

    Separate from the submit step so it runs before any conversation mutation: a
    regenerate/edit must not reposition the active leaf only to fail here, leaving the
    thread truncated with no replacement."""
    # Resolved now (picker override included) so a misconfiguration is a clear 4xx/503, not
    # a run that starts and errors.
    try:
        main = await model_registry.resolve_detailed(
            "main",
            owner_id=owner_id,
            override_endpoint_id=endpoint_id,
            override_model=model,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="model endpoint not found") from None
    except DegradedCapabilityError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # No context window, no turn. It is discovered from the provider where possible
    # (`ModelRegistry.with_model_limits`), so reaching here means this one won't say and the
    # operator hasn't set it.
    #
    # A hard stop, not a degraded run: the gauge, the compaction trigger and the overflow
    # warning all measure against it, so without it a long thread runs until the provider
    # rejects a request with no warning or fold. Refusing up front names the fix.
    if main.context_window is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "this model's endpoint doesn't report a context window, so the "
                "conversation can't be kept inside it — set one on the endpoint under "
                "settings › models › advanced before sending"
            ),
        )
    resolved = main.model

    # Verification and auto-titling run on the cheap `utility` model, titling with its
    # reasoning-off settings. Resolved only when one is enabled; with no `utility` endpoint
    # bound, the resolved `main` model (picker override included) is reused.
    settings = get_settings()
    utility_model = resolved
    title_settings: ModelSettings | None = None
    if settings.verify_enabled or settings.title_enabled:
        background = await model_registry.resolve_background(
            owner_id=owner_id,
            override_endpoint_id=endpoint_id,
            override_model=model,
        )
        utility_model = background.model
        title_settings = background.reasoning_off
    return TurnModels(
        resolved, utility_model, title_settings, main.context_window, main.vision, main.endpoint
    )


def compose_turn(
    *,
    prompt: str | None,
    conversation_id: str,
    models: TurnModels,
    capabilities: ServiceContainer,
    registry: RunRegistry,
    store: ConversationStore,
    uploads: UploadStore,
    categories: Mapping[str, AbstractToolset[RunDeps]] | None = None,
    dormant: Mapping[str, str] | None = None,
    instruction_providers: Sequence[InstructionProvider] = (),
    prompt_context_providers: Sequence[PromptContextProvider] = (),
    disabled_tools: frozenset[str] = NO_DISABLED_TOOLS,
    binding: ConversationBinding | None = None,
    owner_id: str = OPERATOR_ID,
    attachment_ids: list[str] | None = None,
    file_refs: list[str] | None = None,
    # A slash command's expansion, resolved by the caller; rides the prompt tail and is
    # stripped before recording, so the `/name` typed is what persists.
    turn_context: str = "",
    # The invocation, which is recorded, so a regenerate resolves `/reviewer` afresh rather
    # than replaying an old block.
    command: Invocation | None = None,
    ephemeral: bool = False,
    auto_compact: AutoCompactPolicy | None = None,
    context_thresholds: ContextThresholds = DEFAULT_CONTEXT_THRESHOLDS,
    inactivity_timeout_s: float | None | Unset = UNSET,
    wall_clock_timeout_s: float | None | Unset = UNSET,
    kind: str = "chat",
    workspace_key: str = "",
    run_id: str | None = None,
    max_requests: int | None = None,
    ledger_prompt_message_id: str | None = None,
) -> ChatCreated:
    """Build the chat orchestrator from pre-resolved parts and submit the Run.

    The one composition path for a live chat turn (`submit_turn`) and an unattended
    scheduled task (`app.py`'s task executor), so approval parking, grants and orchestrator
    wiring can't diverge.

    No failure path follows the caller's conversation mutation. ``prompt is None`` is a
    regenerate. ``ephemeral`` threads (compare panes) show no title, so auto-titling them is
    invisible work; skip it by passing no title model.

    ``kind`` says who asked: the operator (``chat``), the scheduler (``task``) or the agent
    (``linked``). All compose the identical orchestrator; the kind only picks the
    concurrency lane (``runs/lanes.py``), so unattended work can't hold up a watched turn."""
    resolved, utility_model, background_settings, context_window, vision, endpoint = models
    orchestrator = build_chat_orchestrator(
        prompt,
        model=resolved,
        categories=categories,
        # Same assembled state as the catalog, so an unattended turn defers what an
        # interactive one does.
        dormant=dormant or {},
        instruction_providers=instruction_providers,
        prompt_context_providers=prompt_context_providers,
        utility_model=utility_model,
        utility_settings=background_settings,
        title_model=None if ephemeral else utility_model,
        title_settings=None if ephemeral else background_settings,
        context_window=context_window,
        # Decide only the gauge colour (`level` on the metrics), never the turn.
        context_thresholds=context_thresholds,
        capabilities=capabilities,
        store=store,
        conversation_id=conversation_id,
        uploads=uploads,
        attachment_ids=attachment_ids,
        file_refs=file_refs,
        turn_context=turn_context,
        command=command,
        vision=vision,
        # Absent ⇒ the config defaults.
        auto_compact=auto_compact,
        disabled_tools=disabled_tools,
        # Absent ⇒ an unfiled sandbox thread, which a stateless or unattended turn is.
        binding=binding or ConversationBinding(),
        # Empty means this conversation's own, filled in by `RunDeps`. A sub-agent passes the
        # launching thread's workspace or a delegated child's (`services/workspace.py`).
        workspace_key=workspace_key or (binding.workspace_key if binding else ""),
        max_requests=max_requests,
        ledger_prompt_message_id=ledger_prompt_message_id,
    )
    try:
        run = registry.submit(
            kind=kind,
            owner_id=owner_id,
            orchestrator=orchestrator,
            run_id=run_id,
            conversation_id=conversation_id,
            endpoint=endpoint,
            # Resolved by the interactive caller; the unattended path leaves both UNSET for
            # the registry default, so a task is wall-clock unbounded unless the deploy set
            # `run_wall_clock_timeout_s`.
            inactivity_timeout_s=inactivity_timeout_s,
            wall_clock_timeout_s=wall_clock_timeout_s,
        )
    except ConversationBusyError as exc:
        # The registry's atomic check caught a race the caller's earlier guard couldn't.
        raise HTTPException(status_code=409, detail=deps.CONVERSATION_BUSY_DETAIL) from exc
    return ChatCreated(run_id=run.id, conversation_id=conversation_id)
