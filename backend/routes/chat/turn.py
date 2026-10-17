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
    """Resolve the `main` model plus the background (utility/title) pair, raising a
    clear 4xx/503 on misconfiguration.

    Five values: the main model, the background model, its reasoning-off settings, the main
    model's context window, and whether the main model reads images. A conversation's
    compaction summary is written by the main model, not the background one, so the
    operator's own fold resolves through here too.

    Kept separate from the submit step so it runs **before** any conversation
    mutation: a regenerate/edit must not reposition (and persist) the active leaf
    only to fail here, which would leave the thread truncated with no replacement."""
    # Resolve the `main` model now (per-conversation endpoint override included),
    # so a model misconfiguration surfaces as a clear 4xx/503 rather than a run
    # that starts and immediately errors.
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
    # No context window, no turn. The window is discovered from the provider where the
    # provider will say (`ModelRegistry.with_model_limits`), so reaching here means
    # this one won't and the operator hasn't filled it in either.
    #
    # A hard stop rather than a degraded run, because every guard that keeps a thread
    # inside the model's limits measures against this number: the context gauge, the
    # auto-compaction trigger, and the overflow warning. Without it a long thread runs
    # normally right up until the provider rejects a request outright — no warning, no
    # fold, and the operator's first indication that anything was wrong is a failed
    # turn. Refusing up front is the honest version of that, and it names the fix.
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

    # Background work — verification (opt-in) and auto-titling (on by default) —
    # runs on the cheap `utility` model, with the title call wanting its
    # reasoning-off settings too (a fast, no-thinking pass). Resolve the pair only
    # when a background feature is enabled, so a plain chat with both off pays
    # nothing; when no `utility` endpoint is bound, reuse the resolved `main` model
    # (picker override included) so both work without extra setup.
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
    # The expansion of a slash command this turn was sent with, already resolved by the
    # caller. Rides the tail of the turn's user prompt and is stripped before the turn is
    # recorded, so what persists is the `/name` the operator typed.
    turn_context: str = "",
    # …and the invocation that produced it, which *is* written down — so a regenerate can
    # ask what `/reviewer` means now rather than replaying a block built a week ago.
    command: Invocation | None = None,
    ephemeral: bool = False,
    auto_compact: AutoCompactPolicy | None = None,
    context_thresholds: ContextThresholds = DEFAULT_CONTEXT_THRESHOLDS,
    inactivity_timeout_s: float | None | Unset = UNSET,
    wall_clock_timeout_s: float | None | Unset = UNSET,
    kind: str = "chat",
    workspace_key: str = "",
) -> ChatCreated:
    """Build the chat orchestrator from pre-resolved models/capabilities and submit
    the Run — the one composition path a live chat turn (`submit_turn`, resolving
    its resources from the `Request` via `routes.deps`) and an unattended scheduled
    task's execution (`app.py`'s task executor, resolving them straight from
    `app.state`) both fire through, so approval parking, conversation grants, and the
    orchestrator's own wiring can never diverge between the two.

    No failure path after the caller's conversation mutation — ``prompt is None``
    is a regenerate (re-run from a history that already ends in the user request).
    ``ephemeral`` threads (e.g. the compare panes) are hidden from the listing and
    show no title, so auto-titling them is invisible work that only holds the run
    open after the answer — skip it by passing no title model.

    ``kind`` says who asked for the turn — the operator (``chat``, the default), the
    scheduler (``task``), or the agent itself (``linked``). Every one of them composes the
    identical orchestrator; the kind decides only which concurrency lane the run waits in
    (``runs/lanes.py``), so unattended work can never hold up the turn someone is sitting
    in front of."""
    resolved, utility_model, background_settings, context_window, vision, endpoint = models
    orchestrator = build_chat_orchestrator(
        prompt,
        model=resolved,
        categories=categories,
        # Which of those categories start withheld, with the line each is advertised by.
        # Threaded from the same assembled state the catalog is, so an unattended turn's
        # agent defers exactly what an interactive one's does.
        dormant=dormant or {},
        instruction_providers=instruction_providers,
        prompt_context_providers=prompt_context_providers,
        utility_model=utility_model,
        utility_settings=background_settings,
        title_model=None if ephemeral else utility_model,
        title_settings=None if ephemeral else background_settings,
        context_window=context_window,
        # The operator's severity boundaries for that window. They decide only the
        # `level` on the emitted metrics — the gauge's colour — never the turn itself.
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
        # The operator's conversation-compaction policy; absent ⇒ the config defaults.
        auto_compact=auto_compact,
        # While offline mode is active the web services are down, so hide the web
        # tools from the agent rather than let it discover they're unavailable.
        disabled_tools=disabled_tools,
        # The thread's workspace and project. Absent ⇒ an unfiled sandbox thread, which is what
        # a stateless or unattended turn is.
        binding=binding or ConversationBinding(),
        # Which workspace this turn's file work happens in. Empty — every turn an operator
        # sends — means "this conversation's own", filled in by `RunDeps`. A sub-agent's
        # turn is what passes one: it works in the workspace of the thread that launched
        # it, or in a delegated child of it (`services/workspace.py`).
        workspace_key=workspace_key or (binding.workspace_key if binding else ""),
    )
    try:
        run = registry.submit(
            kind=kind,
            owner_id=owner_id,
            orchestrator=orchestrator,
            conversation_id=conversation_id,
            endpoint=endpoint,
            # The operator's inactivity bound, resolved by the interactive caller; the
            # unattended task path omits it (UNSET) and the registry default applies.
            inactivity_timeout_s=inactivity_timeout_s,
            # Same shape for the wall clock — resolved by the interactive caller, UNSET
            # elsewhere. Both defaults are off, so an unattended turn is unbounded in
            # wall-clock terms unless the deploy set `run_wall_clock_timeout_s`.
            wall_clock_timeout_s=wall_clock_timeout_s,
        )
    except ConversationBusyError as exc:
        # The registry's own atomic check-and-claim caught a race the caller's
        # earlier `require_conversation_free` guard couldn't (two requests that
        # both saw no active run before either submitted).
        raise HTTPException(status_code=409, detail=deps.CONVERSATION_BUSY_DETAIL) from exc
    return ChatCreated(run_id=run.id, conversation_id=conversation_id)
