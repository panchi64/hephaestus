"""Routines: the scheduler, how a routine fires, and the agent's `routines_*` tools.

An agent routine runs as an ordinary Run in a new thread each time, by the routine's agent,
through the chat turn's own composition (no forked submission path); a reminder fires its
prompt verbatim as a notification. The executor learns a Run's outcome from a waiter future
resolved synchronously at the terminal transition.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from agent.recompose import ParkedFacts, Recomposed, RecomposeError, register_recompose
from core.api_scopes import ScopeClaim
from core.exceptions import NotFoundError
from core.serde import as_utc
from core.untrusted import wrap_untrusted
from harness.manifest import (
    DormantCategory,
    FeatureManifest,
    FeatureRuntime,
    HarnessContext,
)
from harness.turns import UnattendedTurns
from models.task import TaskOutcome, TaskOutput
from routes import tasks as tasks_routes
from routes.deps import OPERATOR_ID
from runs import Run, RunKind, RunRegistry, RunStatus
from runs.registry import UNSET
from services.agents import AgentCatalog
from services.approval_grants import ONCE_ONLY_TOOLS, ApprovalGrantStore
from services.conversations import ConversationStore
from services.corpus import CorpusIndex
from services.notifications import NotificationService
from services.registry import ModelRegistry
from services.routines import RoutineStore, ScheduleNaturalLanguage
from services.routines.corpus import SOURCE_KIND, SURFACE_ID, routine_runs_adapter
from services.scheduler import (
    ScheduledTaskView,
    SchedulerService,
    TaskRunResult,
    TaskRunSuspended,
)
from services.tool_sensitivity import Sensitivity
from tools.platform import PlatformSource
from tools.routines import GATED_TOOLS as ROUTINES_GATED
from tools.routines import routines_toolset

logger = logging.getLogger(__name__)

# A one-line outcome, enough to judge at a glance whether to open the conversation.
_TASK_SUMMARY_MAX_CHARS = 280

#: The kind a routine's run is submitted under (``runs/lanes.py``).
TASK_KIND = "task"

_SENSITIVITY = {
    "routines_list": Sensitivity.READ,
    # A routine an agent drafts never fires until the operator approves it; rewriting or
    # deleting an approved one is gated on the tool itself.
    "routines_delete": Sensitivity.WORKSPACE_WRITE,
    "routines_write": Sensitivity.WORKSPACE_WRITE,
    # Starts a routine's agent turn, with its pre-authorizations, after this one.
    "routines_run_now": Sensitivity.HOST_EXEC,
}

# `TaskRun.outcome` by the Run status it settled at.
_TASK_OUTCOME_BY_RUN_STATUS = {
    RunStatus.done: TaskOutcome.OK.value,
    RunStatus.error: TaskOutcome.ERROR.value,
    RunStatus.blocked: TaskOutcome.BLOCKED.value,
    RunStatus.cancelled: TaskOutcome.CANCELLED.value,
}


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    services = ctx.services
    conversations = services.get(ConversationStore)
    registry = services.get(ModelRegistry)
    runs = services.get(RunRegistry)
    grants = services.get(ApprovalGrantStore)
    notifications = services.get(NotificationService)
    agents = services.get(AgentCatalog)
    routines = RoutineStore(ctx.engine, ctx.vault, agents)
    services.get(CorpusIndex).register(routine_runs_adapter(ctx.engine, ctx.vault))
    turns = UnattendedTurns(ctx)
    composer = turns.composer

    async def _routine_gone(run_id: str) -> str | None:
        if await scheduler.records_run(run_id):
            return None
        return "the routine this run belonged to no longer exists"

    async def _recompose(facts: ParkedFacts) -> Recomposed:
        if (gone := await _routine_gone(facts.run.id)) is not None:
            raise RecomposeError(gone)
        return await composer.recompose(facts, kind=TASK_KIND, depth=0)

    register_recompose(runs, TASK_KIND, _recompose)

    # By run id: the executor awaits its future for a genuinely terminal state, possibly long
    # after an approval park and resume. Resolved synchronously at the terminal transition,
    # so there is no polling loop.
    run_waiters: dict[str, asyncio.Future[Run]] = {}

    async def _task_run_summary(run: Run, conversation_id: str | None) -> str | None:
        """A plain one-line summary of how the run settled, with no model call. Error,
        blocked and cancelled runs carry their own reason; a `done` run's is the start of
        its final answer."""
        if run.status is RunStatus.error:
            return run.error
        if run.status is RunStatus.blocked:
            return run.detail
        if run.status is RunStatus.cancelled:
            return run.detail or "cancelled"
        if run.status is RunStatus.done and conversation_id is not None:
            turns = await conversations.messages_view(conversation_id)
            for turn in reversed(turns):
                if turn.role == "assistant" and turn.content:
                    return turn.content[:_TASK_SUMMARY_MAX_CHARS]
            return None
        return None

    async def _open_thread(view: ScheduledTaskView) -> str:
        """A new thread for this run, by the routine's agent, seeded with its
        pre-authorizations. A fresh thread is never busy, so nothing holds a run from its
        time. The routine records it as its latest.

        The seeds are whole-tool grants, even on a tool that runs commands: only a tool name
        was approved, with no command to scope to. A seed is undecisive: at the reviewing
        level it informs the review rather than answering for it, as nobody watches the run."""
        conversation_id = await conversations.create_conversation(
            view.owner_id, title=view.title, agent_name=view.agent_name
        )
        for tool_name in view.pre_authorized:
            # Once-only stays once-only: a standing grant on a per-call gate does its damage
            # where nobody reads what it covers.
            if tool_name not in ONCE_ONLY_TOOLS:
                _ = await grants.grant(view.owner_id, conversation_id, tool_name)
        await routines.attach_thread(view.id, conversation_id)
        return conversation_id

    async def _with_last_run(view: ScheduledTaskView) -> str:
        """The routine's prompt plus how its previous run ended, so runs continue across threads."""
        try:
            recent = await routines.runs(view.owner_id, view.id, limit=10)
        except NotFoundError:  # deleted since it fired
            return view.prompt
        previous = next(
            (run for run in recent if run.finished_at is not None and run.summary), None
        )
        if previous is None:
            return view.prompt
        assert previous.finished_at is not None and previous.summary
        stamp = as_utc(previous.finished_at).strftime("%Y-%m-%d %H:%M UTC")
        last = wrap_untrusted(previous.summary, source="the routine's previous run")
        return f"{view.prompt}\n\nHow the previous run ended ({stamp}, {previous.outcome}):\n{last}"

    async def _notify_outcome(
        view: ScheduledTaskView, conversation_id: str | None, summary: str | None
    ) -> None:
        if view.output == TaskOutput.NOTIFICATION.value:
            _ = await notifications.notify(
                view.owner_id,
                "task_outcome",
                view.title,
                body=summary,
                conversation_id=conversation_id,
                task_id=view.id,
            )

    async def _task_executor(view: ScheduledTaskView) -> TaskRunResult:
        """A routine's fire: a turn in a new thread by the routine's agent, composed as an
        interactive turn is (`compose_turn`)."""
        conversation_id = await _open_thread(view)
        thread = await composer.resolve(view.owner_id, conversation_id)
        applied, models = thread.applied, thread.models
        # Nobody watches: tools that wait for someone are withheld, as are unconfigured features.
        disabled = await composer.disabled(
            view.owner_id, kind=TASK_KIND, vision=models.vision, applied=applied, depth=0
        )
        wall_clock = applied.agent.wall_clock_timeout_s
        prompt = await _with_last_run(view)
        waiter: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
        run_id = uuid4().hex
        # Linked first so a restored run always finds its row and a failed link starts nothing.
        if view.task_run_id is not None:
            await scheduler.link_task_run(
                view.task_run_id, run_id=run_id, conversation_id=conversation_id
            )
        try:
            created = await turns.submit(
                run_id=run_id,
                prompt=prompt,
                conversation_id=conversation_id,
                models=models,
                instruction_providers=thread.instruction_providers,
                disabled_tools=disabled,
                binding=applied.binding,
                owner_id=view.owner_id,
                kind=TASK_KIND,
                max_requests=applied.agent.max_requests,
                wall_clock_timeout_s=wall_clock if wall_clock is not None else UNSET,
            )
        except Exception:
            if view.task_run_id is not None:
                try:
                    await scheduler.link_task_run(
                        view.task_run_id, run_id=None, conversation_id=conversation_id
                    )
                except Exception:
                    logger.exception("tasks: could not unlink TaskRun %s", view.task_run_id)
            raise
        # Registered before the first await so the run can't reach terminal first.
        run_waiters[created.run_id] = waiter
        # A parked fire stays open until the operator decides. Shutdown releases the wait
        # with the run still parked; the `TaskRun` stays open for whoever settles the restored run.
        run = await waiter
        if run.is_parked:
            raise TaskRunSuspended

        outcome = _TASK_OUTCOME_BY_RUN_STATUS.get(run.status, TaskOutcome.ERROR.value)
        summary = await _task_run_summary(run, conversation_id)
        await _notify_outcome(view, conversation_id, summary)
        return TaskRunResult(
            outcome=outcome,
            run_id=run.id,
            conversation_id=conversation_id,
            summary=summary,
        )

    async def _task_notify(view: ScheduledTaskView) -> None:
        """A reminder's fire: its prompt verbatim as the notification body, task title as title."""
        _ = await notifications.notify(
            view.owner_id,
            "reminder",
            view.title,
            body=view.prompt,
            task_id=view.id,
        )

    # Single-instance, in-process. Prompts are encrypted, so its tick loop parks while the
    # vault is locked. This manifest builds after everything the executor reaches, so the
    # scheduler stops before all of it and nothing submits runs into a tearing-down process.
    scheduler = SchedulerService(
        ctx.engine,
        ctx.vault,
        executor=_task_executor,
        notify=_task_notify,
    )
    await ctx.lifecycle.start("scheduler", start=scheduler.start, stop=scheduler.stop)

    def _release_parked_fires() -> None:
        """A fire parked at shutdown is kept for the next process, so its waiting executor is
        released (leaving the `TaskRun` open) rather than holding the scheduler's stop."""
        for run_id, waiter in list(run_waiters.items()):
            run = runs.get(run_id)
            if run is not None and run.is_parked and not waiter.done():
                waiter.set_result(run)

    # Registered after the scheduler, so it runs before the scheduler's stop.
    ctx.lifecycle.on_stop("task-parked-fires", _release_parked_fires)

    settling: set[asyncio.Task[None]] = set()

    async def _settle_restored(run: Run) -> None:
        """A restored routine run has no waiting executor; its end finalizes its `TaskRun` here."""
        try:
            outcome = _TASK_OUTCOME_BY_RUN_STATUS.get(run.status, TaskOutcome.ERROR.value)
            summary = await _task_run_summary(run, run.conversation_id)
            view = await scheduler.settle_restored(run.id, outcome=outcome, summary=summary)
            if view is not None:
                await _notify_outcome(view, run.conversation_id, summary)
        except Exception:
            logger.exception("tasks: could not settle the TaskRun of restored run %s", run.id)

    def _resolve_waiter(run: Run) -> None:
        waiter = run_waiters.pop(run.id, None)
        if waiter is not None:
            if not waiter.done():
                waiter.set_result(run)
        elif run.kind == TASK_KIND:
            settle = asyncio.create_task(_settle_restored(run), name=f"task-settle:{run.id}")
            settling.add(settle)
            settle.add_done_callback(settling.discard)

    async def _drain_settling() -> None:
        _ = await asyncio.gather(*list(settling), return_exceptions=True)

    ctx.lifecycle.on_stop("task-settling", _drain_settling)

    async def _restore_problem(run: Run) -> str | None:
        return await _routine_gone(run.id)

    runs.set_restore_check(TASK_KIND, _restore_problem)

    async def _cancel_runs_of(routine_id: str, by_run: str | None) -> None:
        for run_id in await scheduler.open_run_ids(routine_id):
            if run_id != by_run:
                _ = await runs.cancel(run_id)

    routines.before_delete(_cancel_runs_of)

    async def _close_stranded() -> None:
        _ = await ctx.vault.unlocked_event.wait()
        _ = await runs.parks_restored.wait()
        try:
            closed = await scheduler.close_stranded(lambda run_id: runs.get(run_id) is not None)
        except Exception:
            logger.exception("tasks: could not close the stranded TaskRun rows")
            return
        if closed:
            logger.info("tasks: closed %d TaskRun row(s) the last process left open", closed)

    _ = ctx.lifecycle.track("task-stranded-runs", _close_stranded())

    async def _resolve_model():
        resolved = await registry.resolve_background(owner_id=OPERATOR_ID)
        return resolved.model, resolved.reasoning_off

    parser = ScheduleNaturalLanguage(resolve_model=_resolve_model)
    return FeatureRuntime(
        services=(scheduler, routines, parser),
        capabilities=(scheduler, routines),
        state={
            "scheduler": scheduler,
            "routines": routines,
            "schedule_parser": parser,
            "task_run_waiters": run_waiters,
        },
        run_terminal_sync=(_resolve_waiter,),
    )


MANIFEST = FeatureManifest(
    name="tasks",
    # The executor composes the full interactive capability set, so those features build first.
    after=(
        "agents",
        "calendar",
        "corpus",
        "external",
        "mail",
        "memory",
        "notifications",
        "secret-vault",
        "skills",
        "uploads",
        "views",
        "web",
    ),
    routers=(tasks_routes.router,),
    toolsets=(("routines", routines_toolset),),
    dormant=(
        DormantCategory(
            "routines",
            "schedule work to run later or on repeat — drafting routines, listing them, "
            + "running one now",
        ),
    ),
    gated_tools=ROUTINES_GATED,
    api_scopes=(ScopeClaim("tasks", ("/tasks",)),),
    # The webhook trigger is auth-exempt: the per-task unguessable path token is the
    # credential. Every other `/tasks` route stays behind the gate.
    public_prefixes=("/tasks/hooks",),
    tool_sensitivity=_SENSITIVITY,
    # Queues like interactive work in slots of its own, with nobody in front of it.
    run_kinds={TASK_KIND: RunKind("routine", chat_turn=True)},
    # A reminder is worthless if it only lands somewhere the operator isn't looking.
    out_of_band_kinds=frozenset({"reminder"}),
    platform_sources={"routines": PlatformSource((SOURCE_KIND,), SURFACE_ID)},
    build=_build,
)
