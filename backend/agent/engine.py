"""The agent engine: the orchestrator that drives a Pydantic AI ``Agent`` on a Run.

The library owns the within-turn loop, tool selection, validation and fallback; this
package owns the run lifecycle, event stream, bounds, and approval pause/resume.

A turn is driven by :func:`agent.turn.drive_turn`, shared by the initial run and every
resume. When the model requests an approval-required tool, Pydantic AI ends the turn with
``DeferredToolRequests`` without executing it; we surface ``approval.required``, park the
Run (``awaiting_input``), and stash a :class:`ParkedTurn` to resume from. ``ask_user``
defers the same way for an answer, which comes back as the call's own result.

This module only sequences one run: arm the stop-flush hooks, prepare, drive, verify,
record, name the thread, write the overhead, plus the error and ``finally`` paths. A turn's
settings are resolved once here and handed to every step (``prelude``, ``turn``,
``verify``, ``folding``, ``metrics``, ``finalize``), so fold threshold, bounds and gauge
frames measure against one set of values. (``naming.py`` and ``gating.py`` still read
their own.) The neighbours own the rest:

- ``factory.py`` — building the turn's ``Agent``: what it is told, what it is offered.
- ``prelude.py`` — everything settled before the model is called, in a load-bearing order.
- ``turn.py`` — one turn to its end: the ``agent.iter`` loop and the three ways it stops.
- ``verify.py`` — whether a finished turn is worth judging, and its one bounded correction.
- ``folding.py`` — when a *turn* folds the thread, and what that does to its persistence
  boundary. (``summarize.py`` is the fold itself.)
- ``metrics.py`` — the context gauge and the room check.
- ``finalize.py`` — where a finished turn goes, and what a park hands its eventual resume.
- ``flush.py`` — persisting a turn that was stopped from outside, shared by both
  orchestrators so a bound, a cancel and an unhandled exception cannot drift apart.
- ``history.py`` — the surgeries on a message list before it reaches a model or the store.
- ``naming.py`` — when and how a fresh thread gets named. (``title.py`` is the model call.)
- ``model_errors.py`` — reading a provider's failure, and what the operator is told.
- ``gating.py`` — ruling on the calls a turn deferred. The rules are ``services/permissions``'.
- ``parking.py`` — the park itself, and the continuation payload a resume works from.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence

from pydantic_ai import (
    AbstractToolset,
    DeferredToolResults,
    ModelMessage,
    ModelRequest,
    ToolApproved,
    ToolDenied,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.config import Settings, get_settings
from core.container import ServiceContainer
from runs import (
    DEFAULT_CONTEXT_THRESHOLDS,
    ContextThresholds,
    Endpoint,
    FoldPoint,
    Orchestrator,
    Run,
    RunStatus,
)
from services.commands.spec import Invocation
from services.conversations import (
    ConversationBinding,
    ConversationStore,
)
from services.settings_store import TurnTuning
from services.tool_policy import vision_disabled_tools
from services.uploads import UploadStore
from tools import (
    NO_DISABLED_TOOLS,
    InstructionProvider,
    PromptContextProvider,
    RunDeps,
)

from .code_mode import code_mode_limits
from .compaction_context import build_compaction_context
from .factory import NO_DORMANT, TurnAgent, build_agent
from .finalize import (
    finalize,
    flush_recorder,
    parked_context,
    parked_start,
    persist_parked_cancel,
)
from .flush import PersistContext, TurnFlush
from .folding import fold
from .history import TurnStart
from .meta import Judge, make_utility_judge
from .metrics import post_fold_metrics
from .naming import (
    TitleContext,
    discard_title,
    maybe_title,
    reap_title,
    release_title,
)
from .parking import DEFAULT_BINDING, ParkedTurn
from .prelude import TurnSetup, prepare_turn, record_prompt
from .recompose import ParkedFacts, Recomposed
from .summarize import (
    AutoCompactPolicy,
    CompactionOutcome,
    FoldResult,
    build_auto_compact_policy,
)
from .title import last_user_text
from .turn import NO_CAPS, drive_turn, turn_deps
from .verify import should_verify, verify_and_correct

logger = logging.getLogger(__name__)


async def _tuned(capabilities: ServiceContainer, owner_id: str) -> Settings:
    """The config, with the operator's own bounds on how a turn waits and carries on."""
    settings = get_settings()
    tuning = capabilities.get_optional(TurnTuning)
    return settings if tuning is None else await tuning.apply(settings, owner_id)


def turn_agent(
    model: Model,
    *,
    categories: Mapping[str, AbstractToolset[RunDeps]] | None = None,
    dormant: Mapping[str, str] = NO_DORMANT,
    instruction_providers: Sequence[InstructionProvider] = (),
    settings: Settings,
    binding: ConversationBinding = DEFAULT_BINDING,
) -> TurnAgent:
    """The agent a chat turn runs on, and the one its compaction summary is written on.

    Shared so the summary request matches the turn's (same brief and tools before the same
    messages) and a local engine serves it from its cached prefix."""
    return build_agent(
        model,
        spawned=binding.spawned,
        categories=categories,
        instruction_providers=instruction_providers,
        dormant=dormant,
        code_mode=code_mode_limits(settings),
        retries={"tools": settings.agent_tool_retries, "output": settings.agent_output_retries},
        tool_call_timeout_s=settings.tool_call_timeout_s,
    )


async def recompose_turn(
    facts: ParkedFacts,
    *,
    model: Model,
    categories: Mapping[str, AbstractToolset[RunDeps]] | None = None,
    dormant: Mapping[str, str] = NO_DORMANT,
    instruction_providers: Sequence[InstructionProvider] = (),
    capabilities: ServiceContainer = NO_CAPS,
    store: ConversationStore | None = None,
    disabled_tools: frozenset[str] = NO_DISABLED_TOOLS,
    auto_compact: AutoCompactPolicy | None = None,
    context_window: int | None = None,
    context_thresholds: ContextThresholds = DEFAULT_CONTEXT_THRESHOLDS,
    title_model: Model | None = None,
    title_settings: ModelSettings | None = None,
    endpoint: Endpoint | None = None,
) -> Recomposed:
    """The agent and compaction context a restored park resumes with, built as
    :func:`build_chat_orchestrator` builds them for a fresh turn. The run's gauge facts are
    stamped too, since a resume reports metrics against them."""
    run = facts.run
    settings = await _tuned(capabilities, run.owner_id)
    run.context_window = context_window
    run.context_thresholds = context_thresholds
    run.context_fold = (
        FoldPoint.of(auto_compact.threshold, active=auto_compact.enabled)
        if auto_compact is not None
        else None
    )
    agent = turn_agent(
        model,
        categories=categories,
        instruction_providers=instruction_providers,
        dormant=dormant,
        settings=settings,
        binding=facts.binding,
    )
    compaction = build_compaction_context(
        store=store,
        conversation_id=facts.conversation_id,
        policy=auto_compact or build_auto_compact_policy(settings),
        agent=agent,
        deps=turn_deps(
            run,
            caps=capabilities,
            disabled_tools=disabled_tools,
            conversation_id=facts.conversation_id,
            binding=facts.binding,
            workspace_key=facts.workspace_key,
        ),
        settings=settings,
    )
    title = (
        TitleContext(title_model, title_settings or {})
        if facts.titled and title_model is not None and settings.title_enabled
        else None
    )
    return Recomposed(agent, compaction, title, endpoint)


async def fold_now(
    run: Run,
    *,
    model: Model,
    store: ConversationStore,
    conversation_id: str,
    categories: Mapping[str, AbstractToolset[RunDeps]] | None = None,
    dormant: Mapping[str, str] = NO_DORMANT,
    instruction_providers: Sequence[InstructionProvider] = (),
    capabilities: ServiceContainer = NO_CAPS,
    disabled_tools: frozenset[str] = NO_DISABLED_TOOLS,
    binding: ConversationBinding = DEFAULT_BINDING,
    workspace_key: str = "",
    context_window: int | None = None,
    context_thresholds: ContextThresholds = DEFAULT_CONTEXT_THRESHOLDS,
    auto_compact: AutoCompactPolicy | None = None,
    focus: str | None = None,
) -> FoldResult:
    """The operator's own fold, on the agent and deps a chat turn on this thread would use,
    so the thread's main model writes the summary as in an automatic fold. No policy: this
    is not a trigger. ``focus`` is what the operator asked the briefing to keep.

    A landed fold leaves the run with real metrics: the run has no turn to read a gauge
    from, and the registry closes it with its stashed metrics, so an empty frame would blank
    the client's meter. ``context_window``, ``context_thresholds`` and ``auto_compact`` only
    shape that frame (severity level, fold mark)."""
    settings = get_settings()
    run.context_window = context_window
    run.context_thresholds = context_thresholds
    run.context_fold = (
        FoldPoint.of(auto_compact.threshold, active=auto_compact.enabled)
        if auto_compact is not None
        else None
    )
    agent = turn_agent(
        model,
        categories=categories,
        instruction_providers=instruction_providers,
        dormant=dormant,
        settings=settings,
        binding=binding,
    )
    ctx = build_compaction_context(
        store=store,
        conversation_id=conversation_id,
        agent=agent,
        deps=turn_deps(
            run,
            caps=capabilities,
            disabled_tools=disabled_tools,
            conversation_id=conversation_id,
            binding=binding,
            workspace_key=workspace_key,
        ),
        settings=settings,
    )
    assert ctx is not None  # a store and a conversation always make one
    result = await fold(run, ctx, reason="manual", focus=focus)
    if isinstance(result, CompactionOutcome):
        run.prior_timings = await store.timings(conversation_id)
        run.context_overhead = await store.get_overhead(conversation_id)
        run.set_metrics(
            post_fold_metrics(run, await store.model_history(conversation_id), settings=settings)
        )
    return result


def build_chat_orchestrator(
    prompt: str | None,
    *,
    model: Model,
    categories: Mapping[str, AbstractToolset[RunDeps]] | None = None,
    dormant: Mapping[str, str] = NO_DORMANT,
    instruction_providers: Sequence[InstructionProvider] = (),
    prompt_context_providers: Sequence[PromptContextProvider] = (),
    judge: Judge | None = None,
    utility_model: Model | None = None,
    utility_settings: ModelSettings | None = None,
    title_model: Model | None = None,
    title_settings: ModelSettings | None = None,
    capabilities: ServiceContainer = NO_CAPS,
    store: ConversationStore | None = None,
    conversation_id: str | None = None,
    context_window: int | None = None,
    context_thresholds: ContextThresholds = DEFAULT_CONTEXT_THRESHOLDS,
    uploads: UploadStore | None = None,
    attachment_ids: list[str] | None = None,
    file_refs: list[str] | None = None,
    turn_context: str = "",
    command: Invocation | None = None,
    vision: bool = False,
    auto_compact: AutoCompactPolicy | None = None,
    disabled_tools: frozenset[str] = NO_DISABLED_TOOLS,
    binding: ConversationBinding = DEFAULT_BINDING,
    workspace_key: str = "",
    max_requests: int | None = None,
    ledger_prompt_message_id: str | None = None,
) -> Orchestrator:
    """Build the orchestrator for one chat turn.

    ``prompt`` is the operator's message, or ``None`` to regenerate: re-run a history that
    already ends in the user request, producing a sibling answer.

    ``attachment_ids`` are files attached to this message (resolved via ``uploads``). Their
    bytes are staged into the conversation's sandbox and the turn carries a marker naming
    each file and path; the model pages through it rather than having text poured into
    context. ``vision`` additionally hands an image over as pixels (retained on persist).
    Only a fresh turn injects attachments; a regenerate's history already has the markers.

    ``file_refs`` are workspace-relative paths named with ``@``: a reference, not an
    injection. They are resolved against the run's workspace and narrowed to what exists,
    so a path picked before a worktree existed can't point at an unopenable file.

    ``turn_context`` is a block the caller resolved for this turn (today a slash command's
    expansion). It rides the tail of the user prompt and is stripped before recording, so
    persisted text stays what the operator typed. It is a parameter, not a
    ``PromptContextProvider``, because a provider re-resolves from
    ``(caps, owner_id, conversation_id)`` and never sees this request.

    ``command`` is the invocation behind that block (name and argument), written onto the
    user request so a regenerate can rebuild the block. The block itself is not persisted:
    a stored template would replay a stale copy of an edited file and a directive like
    "before anything else in this turn" would be false by the next turn.

    ``model`` is the resolved ``main`` model. ``categories`` overrides the tool catalog and
    ``dormant`` (category → one-line summary) names groups whose schemas the model loads
    only by asking. The verifier's judge is ``judge``, else built from ``utility_model``;
    with neither, verification is skipped. ``utility_settings`` carries that model's
    reasoning-off settings. With ``store`` and ``conversation_id`` the turn continues prior
    history and persists; without them it runs stateless. The verifier runs only when
    enabled (by default only on tool-producing turns). With ``title_model`` and
    ``title_enabled``, the first completed turn of a fresh thread is auto-named, using
    ``title_settings``.

    ``binding`` is the thread's workspace kind, project and level, read by the caller; it
    decides where file work happens (``services/workspace.py``) and which workspace tools
    the gate offers.

    A completed turn writes what its request weighed besides the conversation
    (``ConversationStore.set_overhead``), since a cold load can't measure it and neither
    the brief nor the tool schemas reach the message history.

    ``context_thresholds`` are the severity boundaries for the composer gauge's amber and
    red; they only set the metrics' ``level``.

    ``auto_compact`` is the compaction policy (operator default plus per-thread override;
    absent ⇒ config defaults). When the replayed history plus the turn about to run reaches
    its share of ``context_window``, everything since the newest checkpoint is summarized
    onto a new one before the agent runs. The same fold recovers a mid-turn provider
    refusal of an over-long request. This turn's agent on ``model`` writes the summary (so
    the cached prefix is reused), never the utility model.
    """

    async def orchestrate(run: Run) -> None:
        settings = await _tuned(capabilities, run.owner_id)
        run.context_window = context_window
        run.context_thresholds = context_thresholds
        # From the same policy object the trigger reads, so the mark on screen is the one
        # that acts, including the conversation's own override.
        run.context_fold = (
            FoldPoint.of(auto_compact.threshold, active=auto_compact.enabled)
            if auto_compact is not None
            else None
        )
        agent = turn_agent(
            model,
            categories=categories,
            instruction_providers=instruction_providers,
            dormant=dormant,
            settings=settings,
            binding=binding,
        )
        announced: set[str] = set()

        # --- the stop-flush hooks, armed before anything that can suspend -------------
        # The prelude awaits (history read, an unlimited fold, staging, context providers)
        # without emitting, so the inactivity watchdog sees an idle run, and a fold running
        # to its limit trips it (the two bounds share a default). Hooks armed after that
        # would be `None` when needed and the typed message would vanish on reload. They
        # read `setup`, filled in place by `prepare_turn` (`prelude.py`); an early hook sees
        # defaults, the right record for a turn that stopped before it began.
        # `setup.turn_start` is one shared mutable object because an in-turn fold moves it
        # and `drive_turn` rewrites it in place.
        setup = TurnSetup()
        # Only a fresh prompt records a user request to stamp; a regenerate re-answers one
        # already stamped, and `prepare_turn` ignores the expansion's twin for the same reason.
        stamped = command if prompt is not None else None
        # Reachable mid-turn so a bound can flush the turn's output before the registry
        # force-cancels the task and the turn is lost on reload (`RunRegistry._flush_timeout`).
        partial_history_ref: list[Callable[[], list[ModelMessage]]] = []

        def _turn_messages_or_prompt() -> list[ModelMessage]:
            # The turn's slice of the partial history, opening request included (`finalize`
            # sets that aside once the prompt is on record). If a bound trips before the
            # first step lands, only the typed prompt: otherwise the flush returns early and
            # the stop marker has no node to land on.
            if partial_history_ref:
                turn = setup.turn_start.slice(partial_history_ref[0]())
                if turn:
                    return turn
            if isinstance(prompt, str) and prompt:
                return [ModelRequest(parts=[UserPromptPart(prompt)])]
            return []

        def _flush_context() -> PersistContext:
            # Read at flush time: `turn_start` and the stamps are unknown when the hooks arm.
            # A stop inside the prelude finds the prompt unrecorded, so it is written here
            # for the stop marker to land on. The boundary carries only the prompt's id
            # because `_turn_messages_or_prompt` returns an already-sliced list.
            record_prompt(
                setup,
                run,
                prompt=prompt,
                command=stamped,
                store=store,
                conversation_id=conversation_id,
            )
            return PersistContext(
                conversation_id=conversation_id,
                start=TurnStart(prompt_message_id=setup.turn_start.prompt_message_id),
                clean_drop=_flush_clean_drop(),
            )

        def _flush_clean_drop() -> tuple[int, int] | None:
            # A stop during a verifier correction must drop the rejected answer and the
            # synthetic nudge, as the completed path does, or they persist and replay to the
            # model. `drop_ref` holds absolute indices; the hooks hand `finalize` an
            # already-sliced list, so rebase onto it.
            if not drop_ref:
                return None
            reject_idx, nudge_idx = drop_ref[0]
            start = setup.turn_start.index
            if reject_idx < start:
                return None
            return reject_idx - start, nudge_idx - start

        # Set by `verify_and_correct` when it commits to a correction.
        drop_ref: list[tuple[int, int]] = []
        flush = TurnFlush(
            run,
            messages=_turn_messages_or_prompt,
            context=_flush_context,
            record=flush_recorder(run, store),
        )
        flush.arm()
        # -----------------------------------------------------------------------------

        # A regenerate answers the request the leaf was reseated onto, which the detail should
        # name. Read before the prelude, whose fold would leave a checkpoint at the tip.
        if prompt is None and store is not None and conversation_id is not None:
            run.prompt_message_id = await store.leaf_id(conversation_id)

        # Outside the `try` below on purpose: an exception here takes the armed hooks' path,
        # not `flush.flush_error()`.
        await prepare_turn(
            setup,
            run,
            agent,
            prompt=prompt,
            store=store,
            conversation_id=conversation_id,
            settings=settings,
            caps=capabilities,
            uploads=uploads,
            attachment_ids=attachment_ids,
            file_refs=file_refs,
            turn_context=turn_context,
            vision=vision,
            binding=binding,
            prompt_context_providers=prompt_context_providers,
            auto_compact=auto_compact,
            disabled_tools=disabled_tools,
            context_window=context_window,
            title_model=title_model,
            title_settings=title_settings,
            workspace_key=workspace_key,
        )
        # Recorded before the model is called so a thread opened mid-run has it and an early
        # stop leaves it standing.
        record_prompt(
            setup,
            run,
            prompt=prompt,
            command=stamped,
            store=store,
            conversation_id=conversation_id,
        )
        if setup.stop_notice is not None:
            _ = run.messages.take_stop_notice()

        try:
            turn = await drive_turn(
                run,
                agent,
                settings=settings,
                prompt=setup.user_prompt,
                message_history=setup.model_history,
                announced=announced,
                caps=capabilities,
                conversation_id=conversation_id,
                disabled_tools=disabled_tools,
                binding=binding,
                vision=vision,
                partial_history_ref=partial_history_ref,
                store=store,
                compaction=setup.compaction,
                turn_start=setup.turn_start,
                workspace_key=workspace_key,
                max_requests=max_requests,
                ledger_prompt_message_id=ledger_prompt_message_id,
            )

            if (
                run.status is not RunStatus.awaiting_input
                and turn.answer is not None
                and settings.verify_enabled
                and should_verify(settings, run)
            ):
                judging = judge or (
                    make_utility_judge(utility_model, model_settings=utility_settings)
                    if utility_model
                    else None
                )
                if judging is not None:
                    # A regenerate judges against the last user turn in history.
                    verify_prompt = (
                        prompt if prompt is not None else last_user_text(setup.history or [])
                    )
                    assert setup.policy is not None
                    turn = await verify_and_correct(
                        run,
                        agent,
                        verify_prompt,
                        turn,
                        announced,
                        judging,
                        settings=settings,
                        caps=capabilities,
                        conversation_id=conversation_id,
                        disabled_tools=disabled_tools,
                        binding=binding,
                        vision=vision,
                        partial_history_ref=partial_history_ref,
                        store=store,
                        drop_ref=drop_ref,
                        # The correction can't fold, so it is skipped when the window lacks
                        # room, measured at the share a fold would fire at.
                        context_threshold=setup.policy.threshold,
                        # Deferred calls are reviewed against the same opening request.
                        turn_start=setup.turn_start,
                        workspace_key=workspace_key,
                        max_requests=max_requests,
                        ledger_prompt_message_id=ledger_prompt_message_id,
                    )

            finalize(
                run,
                turn,
                store=store,
                # Not `_flush_context()`: this path uses the real `turn_start` (an in-turn
                # fold may have moved it) and the verifier's drop range.
                context=PersistContext(
                    conversation_id=conversation_id,
                    start=setup.turn_start,
                    clean_drop=turn.clean_drop,
                ),
            )
            # Disarmed now the turn is recorded: a bound or cancel during the awaits below
            # must not re-run `finalize` and double-record it or stamp a stop on an answer.
            flush.disarm()
            flush.done = True

            if run.status is RunStatus.awaiting_input:
                # Armed before any further `await`: a concurrent cancel of this now-visible
                # parked run needs the persistence context wired (see
                # `persist_parked_cancel`).
                run.on_park_cancel = lambda: persist_parked_cancel(run, store=store)
                # Parked before an answer: abandon the concurrent namer so its model call
                # doesn't outlive the run, and carry the context forward so the resume names
                # the thread if this got there first. A namer already announcing finishes,
                # so the thread shows a name while awaiting approval; the resume's
                # `set_title_if_absent` then finds it and emits nothing twice.
                await discard_title(setup.title_namer)
                if isinstance(run.parked_payload, ParkedTurn):
                    run.parked_payload.title = setup.title_ctx
            else:
                # A running namer outlives the run; cleared so `finally` doesn't reap it.
                release_title(run, setup.title_namer)
                setup.title_namer = None

            # What this turn's requests weighed besides the conversation, for the thread's
            # next cold load (neither brief nor schemas reach the history).
            #
            # Last and never fatal: it must follow the verifier (whose correction leaves a
            # newer measurement) and `finalize` (no overhead for an unrecorded turn), and
            # must not `await` between a park and the `on_park_cancel` arming above. The
            # write is swallowed because it is a readout; failing would turn an answered
            # turn into an errored run and route its messages through the degraded error-flush.
            if store is not None and conversation_id is not None:
                try:
                    await store.set_overhead(conversation_id, run.context_overhead)
                except Exception:
                    logger.warning("failed to record context overhead", exc_info=True)
        except Exception:
            # Persist what the turn produced, with a legible marker, so an escaped error
            # doesn't drop the operator's prompt. Never touches `run.status`: the registry
            # decides the outcome. Flushes via `_turn_messages_or_prompt` because an error
            # before the first step leaves `partial_history_ref` empty and the typed prompt
            # would be lost.
            flush.flush_error()
            # The task is unwinding, so no further hook call is legitimate.
            flush.disarm()
            raise
        finally:
            await reap_title(setup.title_namer)

    return orchestrate


def build_resume_orchestrator(
    parked: ParkedTurn,
    decisions: Mapping[str, ToolApproved | ToolDenied | bool],
    *,
    answers: dict[str, str] | None = None,
    capabilities: ServiceContainer = NO_CAPS,
    store: ConversationStore | None = None,
    disabled_tools: frozenset[str] = NO_DISABLED_TOOLS,
) -> Orchestrator:
    """Resume a parked turn with the operator's approve/deny decisions and answers.

    Approvals and answers resume together: the park has a single continuation, and a second
    start would run against a history the first had moved past.
    """

    async def orchestrate(run: Run) -> None:
        agent = parked.agent
        if agent is None:
            raise RuntimeError("a restored park resumes only after its agent is recomposed")
        settings = await _tuned(capabilities, run.owner_id)
        # `calls` carries values, which Pydantic AI wraps in a `ToolReturn`, so the answer
        # lands in history as the call's own result.
        results = DeferredToolResults(approvals=dict(decisions), calls=answers or {})

        # A resumed turn gets fresh timeouts too (`RunRegistry.resume`), so it needs the
        # same flush-before-force-cancel hook.
        partial_history_ref: list[Callable[[], list[ModelMessage]]] = []
        # The destination rode on the `ParkedTurn`; only the persistence start can still
        # move, when an overflow recovery folds the thread under this turn.
        turn_start = parked_start(parked)
        flush = TurnFlush(
            run,
            messages=lambda: partial_history_ref[0]() if partial_history_ref else [],
            context=lambda: parked_context(parked, turn_start),
            record=flush_recorder(run, store),
        )
        flush.arm()
        try:
            turn = await drive_turn(
                run,
                agent,
                settings=settings,
                message_history=parked.message_history,
                deferred_results=results,
                announced=parked.announced,
                caps=capabilities,
                conversation_id=parked.conversation_id,
                # The route re-reads operator and offline sources so a tool switched off while
                # parked stays hidden; vision comes off the payload since only the parked
                # agent knows its model (`ParkedTurn.vision`).
                disabled_tools=disabled_tools | vision_disabled_tools(parked.vision),
                # Binding and workspace_key come from the payload so the resume works in
                # the same place the parked turn did.
                binding=parked.binding,
                partial_history_ref=partial_history_ref,
                store=store,
                # A turn parked inside a verifier correction holds a drop range indexed
                # into the pre-fold history, so it can't fold (as the correction couldn't).
                compaction=parked.compaction,
                turn_start=turn_start,
                correcting=parked.clean_drop is not None,
                workspace_key=parked.workspace_key,
                max_requests=parked.max_requests,
                requests_used=parked.requests_used,
                ledger_prompt_message_id=parked.ledger_prompt_message_id,
            )
            finalize(run, turn, store=store, context=parked_context(parked, turn_start))
            # A bound or cancel after this must not re-finalize.
            flush.disarm()
            flush.done = True

            if run.status is RunStatus.awaiting_input:
                # Re-parked: re-arm the park-cancel flush and carry the title context
                # forward so the eventual completion still names the thread.
                run.on_park_cancel = lambda: persist_parked_cancel(run, store=store)
                if isinstance(run.parked_payload, ParkedTurn):
                    run.parked_payload.title = parked.title
            else:
                # A first turn that parked then resumed is still the opening exchange
                # (`persist_from == 0`: no prior turns).
                maybe_title(
                    run,
                    title=parked.title,
                    store=store,
                    conversation_id=parked.conversation_id,
                    is_first_turn=parked.persist_from == 0,
                )
        except Exception:
            # As in the chat orchestrator: an unhandled exception must not drop the turn,
            # which on a resume includes everything since the park.
            flush.flush_error()
            flush.disarm()
            raise

    return orchestrate
