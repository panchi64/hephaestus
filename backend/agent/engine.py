"""The agent engine — the first real orchestrator on the Run substrate.

Wraps Pydantic AI's ``Agent`` and drives it via ``agent.iter()`` so the chassis
can observe every step and stream it (translation lives in ``translate.py``).
The library owns the within-turn loop, tool selection, validation, and fallback;
we own the run lifecycle, the event stream, bounds, and the approval pause/resume
for sensitive actions.

A turn is driven by :func:`agent.turn.drive_turn`, shared by the initial run and every
resume. When the model requests a sensitive (approval-required) tool, Pydantic AI
ends the turn with ``DeferredToolRequests`` *without executing it*; we surface
``approval.required``, park the Run (``awaiting_input``), and stash a
:class:`ParkedTurn` so an approve decision can resume exactly where it left off.
``ask_user`` takes the same road for the other reason a turn stops on the operator:
the call defers for an *answer* rather than a permission, and the answer they give
comes back as the call's own result.

What lives *here* is the sequencing of one run and nothing else: arm the stop-flush
hooks, prepare the turn, drive it, verify it, record it, name the thread, write the
overhead — plus the error and ``finally`` paths that hold when any of that is cut short.
A turn's settings are resolved **once**, here, and handed to everything the sequence
calls — ``prelude``, ``turn``, ``verify``, ``folding``, ``metrics``, ``finalize``, none of
which reads them for itself — so a turn's fold threshold, its bounds and every gauge frame
measure against one set of values rather than each re-reading the cached singleton.
(``naming.py`` and ``gating.py``, carved out of this file long before those were, still
read their own.) The neighbours below carry the concerns that aren't the sequence, each
with its own reason to change and its own module docstring saying what it owns:

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
    discard_title,
    maybe_title,
    reap_title,
    settle_title,
)
from .parking import DEFAULT_BINDING, ParkedTurn
from .prelude import TurnSetup, prepare_turn, record_prompt
from .summarize import AutoCompactPolicy, CompactionOutcome, FoldResult
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
) -> TurnAgent:
    """The agent a chat turn runs on — and the one its compaction summary is written on.

    One function for both because the summary's whole value is that its request matches
    the turn's: the same brief and the same tool array in front of the same messages, so a
    local engine serves it from the prefix it already holds. An operator's hand-started
    fold composes its agent here too, rather than a lookalike that could drift."""
    return build_agent(
        model,
        categories=categories,
        instruction_providers=instruction_providers,
        dormant=dormant,
        code_mode=code_mode_limits(settings),
        retries={"tools": settings.agent_tool_retries, "output": settings.agent_output_retries},
        tool_call_timeout_s=settings.tool_call_timeout_s,
    )


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
) -> FoldResult:
    """The operator's own fold, on the agent and deps a chat turn on this thread would run
    with — so the summary is written by the thread's main model, exactly as an automatic
    fold's is. No policy: nothing here is a trigger, so there is nothing to decide.

    **A fold that lands leaves the run with real metrics.** The run has no turn to take a
    gauge reading from, and the registry closes every run with its stashed metrics — so
    without this the closing frame was an empty one, which a client reads as the
    authoritative state of the thread and blanks the meter on. ``context_window``,
    ``context_thresholds`` and ``auto_compact`` are what a chat turn's orchestrator would
    have put on the run; they only shape the frame (severity level, fold mark)."""
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
    assert ctx is not None  # noqa: S101 — a store and a conversation always make one
    result = await fold(run, ctx, reason="manual")
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
    request_limit: int | None = None,
    workspace_key: str = "",
) -> Orchestrator:
    """Build the orchestrator for one chat turn (one always-agent path).

    ``prompt`` is the operator's message, or ``None`` to **regenerate**: re-run
    from a history that already ends in the user request (the caller moved the
    active leaf there), producing a fresh answer as a sibling of the previous one.

    ``attachment_ids`` are files the operator attached to *this* message (resolved via
    ``uploads``). Their original bytes are staged into the conversation's sandbox and the
    turn carries a short marker naming each file and its path — the model reads and pages
    through the file itself rather than having its text poured into context. ``vision``
    additionally hands an image over as pixels, which is the one attachment kind that
    still rides inline (and is retained on persist). Attachments are injected only on a
    fresh turn; a regenerate (``prompt is None``) re-runs prior history, which already
    carries the markers.

    ``file_refs`` are workspace-relative paths the operator named with ``@``. A
    **reference, not an injection**: the turn carries the paths and the model reads what it
    wants with ``files_read_file``. Resolved against the run's own workspace and silently
    narrowed to what is actually there, so a path picked before a worktree existed cannot
    point the model at a file it has no way to open.

    ``turn_context`` is a block the *caller* resolved for this one turn — today the
    expansion of a slash command the operator picked. It rides the tail of the turn's user
    prompt beside the manifests' own per-turn context and is stripped before the turn is
    recorded, so what persists stays the text the operator actually typed. It is a
    parameter rather than a ``PromptContextProvider`` because a provider re-resolves from
    ``(caps, owner_id, conversation_id)`` and never sees *this* request — which is exactly
    what a per-invocation block is.

    ``command`` is the *invocation* behind that block — the name the operator typed and the
    argument after it — written onto the turn's user request so a later regenerate can ask
    what it meant and build the block again. Deliberately not the block itself: a template
    persisted into history would replay an old copy of a file that has since been edited,
    and a directive reading "before anything else in this turn" is false by the next turn.
    The pair is not redundant, it is the split between *what to say now* and *what to write
    down*, and only the second survives the turn.

    ``model`` is the resolved ``main`` model (the route resolves it from the
    registry, with any per-conversation override). ``categories`` overrides the
    tool catalog, and ``dormant`` — category → one-line summary — names the groups within
    it whose schemas the model loads for itself, so this turn pays for them only if it
    asks. The verifier's judge is ``judge`` if injected, else one built
    from ``utility_model`` when given; with neither, verification is skipped (a
    graceful degradation when no utility model is configured). ``utility_settings``
    carries that model's reasoning-off settings so the judge, like the namer, requests
    reasoning off. With ``store`` +
    ``conversation_id`` the turn continues prior history and persists its new
    messages; without them it runs stateless. The verifier only runs when enabled
    in settings (and, by default, only on tool-producing turns). With
    ``title_model`` (and ``title_enabled`` in settings) the *first* completed turn
    of a fresh thread is auto-named; ``title_settings`` carries the model's
    reasoning-off settings so the namer runs fast.

    ``request_limit`` is the turn's model-round-trip ceiling — the operator's setting
    when the caller resolved one, else the config default. It bounds the *whole* turn,
    grant-resume and mid-run-steering continuations included, so a steady drip of
    steering messages can't extend it.

    ``binding`` is the thread's binding (its workspace kind, project and level), read off
    the conversation by the caller. It decides where this turn's file work happens
    (``services/workspace.py``) and which workspace tools the gate offers.

    A completed turn writes what its request weighed besides the conversation onto the
    thread (``ConversationStore.set_overhead``), so a later *reload* can still break the
    context down — a cold load has no request to measure, and neither the standing brief
    nor the tool schemas reach the message history.

    ``context_thresholds`` are the operator's severity boundaries for that window — the
    fullness at which the composer's gauge turns amber and then red. They only decide the
    ``level`` on the emitted metrics; nothing in the turn's behaviour keys off them.

    ``auto_compact`` is the conversation-compaction policy (the operator's default folded
    with any per-thread override; absent ⇒ the config defaults). When the replayed history
    *plus the turn about to run* would reach its share of ``context_window``, everything
    since the newest checkpoint is summarized onto a new one before the agent runs, and
    the turn continues from that summary. The same fold is the recovery when a provider
    refuses an over-long request mid-turn. The summary is written by this turn's own agent
    on ``model`` — the conversation continuing, so the engine's cached prefix is reused —
    never by the utility model.
    """

    async def orchestrate(run: Run) -> None:
        settings = await _tuned(capabilities, run.owner_id)
        run.context_window = context_window
        run.context_thresholds = context_thresholds
        # The gauge's fold mark comes off the *same* policy object the trigger reads, so
        # the number on screen is the number that will act on the thread — including the
        # conversation's own on/off override, which a client reading the global setting
        # would miss.
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
        )
        announced: set[str] = set()

        # --- the stop-flush hooks, armed before anything that can suspend -------------
        # Everything in the prelude below awaits (a history read, a whole fold with no
        # time limit, attachment staging, the context providers) and none
        # of it emits, so the inactivity watchdog is ticking against a run that looks idle
        # — and the compaction bound and the inactivity bound share a default, so a fold
        # running to its own limit trips it. Armed after that window, the hooks would be
        # `None` exactly when they are needed and the operator's typed message would vanish
        # on reload. What they read is `setup`, filled in place by `prepare_turn` (see
        # `prelude.py`); a hook firing early sees its defaults, which is the correct record
        # for a turn that stopped before it began. `setup.turn_start` — where this turn's
        # own messages begin in the replayed history — is one shared mutable object because
        # an in-turn fold moves it: `drive_turn` rewrites it in place, and every reader
        # below reads through it rather than closing over a stale integer.
        setup = TurnSetup()
        # Only a turn that carries a fresh prompt records a user request, and the stamp
        # belongs on that request. A regenerate re-answers a request already in the tree —
        # one that has carried its own stamp since the day it was sent — so stamping here
        # would have nothing to write on, and `prepare_turn` ignores the expansion's twin
        # for the same reason.
        stamped = command if prompt is not None else None
        # Reachable mid-turn so a wall-clock/inactivity bound can flush whatever the
        # turn has produced before the registry force-cancels this task (which would
        # otherwise interrupt us before we reach `finalize` below and silently drop
        # the turn on the next reload — see `RunRegistry._flush_timeout`).
        partial_history_ref: list[Callable[[], list[ModelMessage]]] = []

        def _turn_messages_or_prompt() -> list[ModelMessage]:
            # The turn's own messages — its slice of the partial history, opening request
            # included (`finalize` sets that one aside once the prompt is on record) — or,
            # if the bound tripped in the pre-model setup window (before the first step
            # landed and `partial_history_ref` is still empty), the operator's typed prompt
            # alone. Without the fallback a stop there hands the flush nothing and it
            # returns early; with it, `_flush_context` writes the prompt down and the stop
            # marker lands on it.
            if partial_history_ref:
                turn = setup.turn_start.slice(partial_history_ref[0]())
                if turn:
                    return turn
            if isinstance(prompt, str) and prompt:
                return [ModelRequest(parts=[UserPromptPart(prompt)])]
            return []

        def _flush_context() -> PersistContext:
            # Read at flush time, not at arm time: `turn_start` and the prompt's stamps are
            # only known once the turn is under way, and the hooks are armed before that
            # (see above). A stop that lands inside the prelude finds the prompt not yet
            # on record, so it is written here, as far as the prelude got, and the stop
            # marker has a node to land on. The boundary is a default one with only the
            # prompt's id, because `_turn_messages_or_prompt` hands over an already-sliced list.
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
            # A stop landing *during* a verifier correction must drop the same two
            # messages the completed path drops — the rejected answer and the synthetic
            # nudge the operator never sent — or they persist as real transcript and
            # replay to the model on every later turn. `drop_ref` carries the range in
            # absolute history indices; the hooks above hand `finalize` an already
            # sliced list with `start=0`, so rebase it onto that slice.
            if not drop_ref:
                return None
            reject_idx, nudge_idx = drop_ref[0]
            start = setup.turn_start.index
            if reject_idx < start:
                return None
            return reject_idx - start, nudge_idx - start

        # Set by `verify_and_correct` the moment it commits to a correction, so a stop
        # mid-correction can drop the same range the completed path does.
        drop_ref: list[tuple[int, int]] = []
        flush = TurnFlush(
            run,
            messages=_turn_messages_or_prompt,
            context=_flush_context,
            record=flush_recorder(run, store),
        )
        flush.arm()
        # -----------------------------------------------------------------------------

        # A regenerate has no prompt of its own to write: it answers the request the leaf
        # was reseated onto, and that is the row the detail should name. Read before the
        # prelude, whose fold would leave a checkpoint at the tip.
        if prompt is None and store is not None and conversation_id is not None:
            run.prompt_message_id = await store.leaf_id(conversation_id)

        # Everything the turn needs before the model is called, filled onto `setup` in
        # place. Outside the `try` below, exactly where the code it replaced ran: an
        # exception here takes the armed hooks' path, not `flush.flush_error()`.
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
        # The prompt goes into the store before the model is called, so a thread opened
        # while this runs has it, and a stop before the first token leaves it standing.
        record_prompt(
            setup,
            run,
            prompt=prompt,
            command=stamped,
            store=store,
            conversation_id=conversation_id,
        )

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
                request_limit=request_limit,
                compaction=setup.compaction,
                turn_start=setup.turn_start,
                workspace_key=workspace_key,
            )

            # Verify only a completed turn (not one parked for approval or stopped at
            # a bound), and only when the heuristic says it is worth judging.
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
                if judging is not None:  # no judge and no utility model → skip (degraded)
                    # On a regenerate (prompt is None) the request to judge against is
                    # the last user turn already in history.
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
                        request_limit=request_limit,
                        drop_ref=drop_ref,
                        # The correction cannot fold, so it is skipped outright when the
                        # window has no room left for it. Measured against the same share
                        # of the window a fold would have fired at, whether or not this
                        # turn is one that *could* fold.
                        context_threshold=setup.policy.threshold,
                        # The correction is this turn continuing, so a call it defers is
                        # reviewed against the same opening request the first pass was.
                        turn_start=setup.turn_start,
                        workspace_key=workspace_key,
                    )

            finalize(
                run,
                turn,
                store=store,
                # The completed path measures against the real `turn_start` — which an
                # in-turn fold may have moved — and carries the verifier's own drop range,
                # where a flush hands over an already-sliced list; hence its own context
                # rather than `_flush_context()`.
                context=PersistContext(
                    conversation_id=conversation_id,
                    start=setup.turn_start,
                    clean_drop=turn.clean_drop,
                ),
            )
            # Disarm the flush hooks now the turn is recorded: a wall-clock/inactivity bound
            # or a cancel landing during the post-answer title window (below) must not
            # re-run `finalize` and double-record the turn (or stamp a spurious stop on a
            # completed answer).
            flush.disarm()
            flush.done = True

            if run.status is RunStatus.awaiting_input:
                # Arm the park-cancel flush now, before any further `await` — a
                # concurrent cancel of this now-externally-visible parked run must
                # find `ParkedTurn`'s persistence context already wired (see
                # `persist_parked_cancel`'s docstring for why this can't wait until
                # after `_discard_title`'s own await below).
                run.on_park_cancel = lambda: persist_parked_cancel(run, store=store)
                # Parked for approval before producing an answer: abandon the concurrent
                # namer so its *model call* doesn't outlive the run, and carry the context
                # forward so the resume names the thread if this cancel got there first
                # (the resume titles from history). A namer far enough along to have begun
                # announcing finishes regardless — a thread waiting on an approval shows
                # its name rather than sitting "Untitled" for however long the operator
                # takes to decide — and the resume's `set_title_if_absent` then finds the
                # name in place, returns False, and emits nothing a second time.
                await discard_title(setup.title_namer)
                if isinstance(run.parked_payload, ParkedTurn):
                    run.parked_payload.title = setup.title_ctx
            else:
                # The namer started up-front announces itself the moment the name lands
                # (typically well before the answer does); this only waits for a still-
                # running one so the event is emitted before the orchestrator returns
                # (run.ended) and the open stream carries it.
                await settle_title(setup.title_namer)

            # What this turn's requests weighed besides the conversation, written onto
            # the thread for its own next cold load — neither the brief nor the schemas
            # reach the message history, so this is the only way a reopened conversation
            # can break its footprint down instead of reporting one flat figure.
            #
            # **Last, and never fatal.** It sits here rather than beside `drive_turn`
            # for three reasons, each of which the earlier placement got wrong: it must
            # follow the verifier, whose corrective re-attempt drives further requests
            # and leaves a newer measurement behind; it must follow `finalize`, so a
            # thread never carries overhead for a turn whose messages didn't record; and
            # it must not `await` between a park and the `on_park_cancel` arming above,
            # which a concurrent cancel of the now-visible parked run depends on. The
            # write is swallowed because this is a readout: losing the breakdown costs a
            # reload its detail, where letting the failure out would turn an answered
            # turn into an errored run and route its messages through the degraded
            # error-flush instead of the finalize that already recorded them.
            if store is not None and conversation_id is not None:
                try:
                    await store.set_overhead(conversation_id, run.context_overhead)
                except Exception:
                    logger.warning("failed to record context overhead", exc_info=True)
        except Exception:
            # Anything else that escapes `drive_turn` (a provider error its specific
            # catches don't cover, a tool/dependency raising, …) must still not silently
            # drop the operator's own prompt: persist whatever the turn had produced,
            # carrying a legible marker, before this propagates to the registry's own
            # generic handler, which records the run as `error`. Mirrors the
            # timeout/cancel flush above but never touches `run.status` — the registry
            # is the one that decides the terminal outcome for an unhandled exception.
            # It flushes through `_turn_messages_or_prompt` rather than the raw partial
            # history, for the same reason the two hook paths do: an exception raised
            # before the first step landed leaves `partial_history_ref` empty, and
            # persisting that empty slice drops the operator's own typed prompt exactly
            # the way a bound tripping in the prelude used to.
            flush.flush_error()
            # Disarm now that this path has (or the normal path already did) recorded
            # the turn — the task is unwinding, so no further hook call is legitimate.
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

    Both piles in one resume, because the park was one park: a turn that stopped on an
    approval *and* a question has a single continuation, and starting it twice would run
    the second against a history the first had already moved past.
    """

    async def orchestrate(run: Run) -> None:
        settings = await _tuned(capabilities, run.owner_id)
        # `calls` carries values rather than verdicts — Pydantic AI wraps each one in a
        # `ToolReturn`, so the operator's answer lands in history as that call's own
        # result and the model reads it exactly as it would any other tool's.
        results = DeferredToolResults(approvals=dict(decisions), calls=answers or {})

        # Same reasoning as the chat orchestrator's `_on_timeout`: a resumed turn is
        # bound by fresh wall-clock/inactivity timeouts too (see `RunRegistry.resume`),
        # so it needs the same flush-before-force-cancel hook.
        partial_history_ref: list[Callable[[], list[ModelMessage]]] = []
        # Unlike a chat turn, a resume's destination is already settled — it rode here on
        # the `ParkedTurn` — so only the messages move, and the one index that can still
        # move with them is the persistence start: an overflow recovery folds the thread
        # underneath this turn and rebuilds the history in front of it.
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
                parked.agent,
                settings=settings,
                message_history=parked.message_history,
                deferred_results=results,
                announced=parked.announced,
                caps=capabilities,
                conversation_id=parked.conversation_id,
                # The route re-reads the operator and offline sources so a tool switched
                # off while this was parked stays hidden; the vision half comes off the
                # payload instead, because only the parked agent knows which model it
                # holds (see `ParkedTurn.vision`).
                disabled_tools=disabled_tools | vision_disabled_tools(parked.vision),
                # From the parked payload, not a fresh read: the resumed turn must work
                # in the same place the parked one did.
                binding=parked.binding,
                partial_history_ref=partial_history_ref,
                store=store,
                # The ceiling the parked turn was running under — a resume continues
                # under the same one rather than reverting to the config default.
                request_limit=parked.request_limit,
                # And what it may fold with, so a resume that overruns the window recovers
                # exactly as the original turn would have. A turn that parked *inside* a
                # verifier correction carries a drop range indexed into the pre-fold
                # history, so it is barred from folding for the same reason the correction
                # itself was.
                compaction=parked.compaction,
                turn_start=turn_start,
                correcting=parked.clean_drop is not None,
                # From the parked payload for the same reason `binding` is: the resumed
                # turn must work in the same place the parked one did.
                workspace_key=parked.workspace_key,
            )
            finalize(run, turn, store=store, context=parked_context(parked, turn_start))
            # Disarm the flush hooks now the turn is recorded — a bound or cancel
            # landing during the title window below must not re-finalize (see the
            # chat orchestrator).
            flush.disarm()
            flush.done = True

            if run.status is RunStatus.awaiting_input:
                # Re-parked on a further approval: re-arm the park-cancel flush (see
                # the chat orchestrator's identical wiring) and carry the title
                # context forward to the new parked payload so the eventual
                # completion still names it.
                run.on_park_cancel = lambda: persist_parked_cancel(run, store=store)
                if isinstance(run.parked_payload, ParkedTurn):
                    run.parked_payload.title = parked.title
            else:
                # A first turn that parked then resumed to completion is still the
                # opening exchange — name it (persist_from == 0 means no prior turns).
                await maybe_title(
                    run,
                    title=parked.title,
                    store=store,
                    conversation_id=parked.conversation_id,
                    is_first_turn=parked.persist_from == 0,
                )
        except Exception:
            # Same reasoning as the chat orchestrator's identical clause: an
            # unhandled exception must not silently drop this turn (which, on a
            # resume, includes everything since the original park) from persistence.
            flush.flush_error()
            # Disarm now that this path has (or the normal path already did) recorded
            # the turn — the task is unwinding, so no further hook call is legitimate.
            flush.disarm()
            raise

    return orchestrate
