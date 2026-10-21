"""Routines (`TASK-1..6`) — the scheduler, how a routine fires, and the agent's
`routines_*` tools.

An agent routine executes as an ordinary Run in a new thread each time, by the routine's
agent, reusing the chat turn's own composition so there is no forked
run-submission path; a reminder fires its prompt verbatim as a notification. The executor
learns a Run's outcome through a waiter future resolved synchronously at the terminal
transition — the same dispatch everything else observes a run's outcome through.
"""

from __future__ import annotations

import asyncio

from agent.recompose import ParkedFacts, Recomposed, register_recompose
from agent.summarize import resolve_auto_compact_policy
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
from models.task import TaskOutcome, TaskOutput
from routes import tasks as tasks_routes
from routes.chat import WallClockOverride, compose_turn
from routes.chat.thread_composer import ThreadComposer
from routes.deps import OPERATOR_ID
from runs import Run, RunRegistry, RunStatus
from services.agents import AgentCatalog
from services.approval_grants import ONCE_ONLY_TOOLS, ApprovalGrantStore
from services.conversations import ConversationStore
from services.notifications import NotificationService
from services.offline import OfflineModeService
from services.projects import ProjectStore, WorktreeManager
from services.registry import ModelRegistry
from services.routines import RoutineStore, ScheduleNaturalLanguage
from services.scheduler import ScheduledTaskView, SchedulerService, TaskRunResult
from services.settings_store import (
    SettingsStore,
    get_context_thresholds,
)
from services.uploads import UploadStore
from tools.routines import GATED_TOOLS as ROUTINES_GATED
from tools.routines import routines_toolset

# A scheduled task's outcome summary is a short factual line, not a transcript —
# just enough for the operator to judge at a glance whether to open the conversation.
_TASK_SUMMARY_MAX_CHARS = 280

#: The kind a routine's run is submitted under (``runs/lanes.py``).
TASK_KIND = "task"

# `TaskRun.outcome` from the Run status it settled at — the three failure-shaped
# statuses map onto the matching `TaskOutcome` verbatim.
_TASK_OUTCOME_BY_RUN_STATUS = {
    RunStatus.done: TaskOutcome.OK.value,
    RunStatus.error: TaskOutcome.ERROR.value,
    RunStatus.blocked: TaskOutcome.BLOCKED.value,
    RunStatus.cancelled: TaskOutcome.CANCELLED.value,
    # Still parked when the process stopped: the run carries on if the operator approves it
    # after the restart, but this fire is over.
    RunStatus.awaiting_input: TaskOutcome.CANCELLED.value,
}


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    services = ctx.services
    conversations = services.get(ConversationStore)
    registry = services.get(ModelRegistry)
    runs = services.get(RunRegistry)
    grants = services.get(ApprovalGrantStore)
    notifications = services.get(NotificationService)
    settings_store = services.get(SettingsStore)
    offline = services.get(OfflineModeService)
    agents = services.get(AgentCatalog)
    routines = RoutineStore(ctx.engine, ctx.vault, agents)
    composer = ThreadComposer(
        agents=agents,
        model_registry=registry,
        conversations=conversations,
        settings_store=settings_store,
        offline=offline,
        settings=ctx.settings,
        capabilities=ctx.capabilities,
        categories=ctx.tool_categories,
        dormant=DormantCategory.summaries(ctx.dormant_categories),
        instruction_providers=ctx.instruction_providers,
        availability=ctx.category_availability,
        projects=services.get_optional(ProjectStore),
        worktrees=services.get_optional(WorktreeManager),
    )

    async def _recompose(facts: ParkedFacts) -> Recomposed:
        return await composer.recompose(facts, kind=TASK_KIND, depth=0)

    register_recompose(runs, TASK_KIND, _recompose)

    # Keyed by run id — the executor awaits one of these futures to learn when its
    # Run reaches a genuinely terminal state, which may be long after an approval
    # park + operator resume round-trip (`AE-3.2`/`AE-3.5`). Resolved synchronously
    # at the terminal transition, so a task execution's eventual settle is observed
    # the same way anything else observes a run's outcome — no polling loop.
    run_waiters: dict[str, asyncio.Future[Run]] = {}

    def _resolve_waiter(run: Run) -> None:
        waiter = run_waiters.pop(run.id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(run)

    async def _task_run_summary(run: Run, conversation_id: str) -> str | None:
        """A short, plain factual line about how the run settled — no extra model
        call. An error/blocked/cancelled run already carries its own operator-legible
        reason; a `done` run's summary is the start of its final answer."""
        if run.status is RunStatus.error:
            return run.error
        if run.status is RunStatus.blocked:
            return run.detail
        if run.status is RunStatus.cancelled:
            return run.detail or "cancelled"
        if run.status is RunStatus.awaiting_input:
            return "waiting for approval when the app stopped"
        if run.status is RunStatus.done:
            turns = await conversations.messages_view(conversation_id)
            for turn in reversed(turns):
                if turn.role == "assistant" and turn.content:
                    return turn.content[:_TASK_SUMMARY_MAX_CHARS]
            return None
        return None

    async def _open_thread(view: ScheduledTaskView) -> str:
        """A new thread for this run, run by the routine's agent and seeded with its
        pre-authorizations (`AE-3.5`). A fresh thread is never busy, so nothing in a
        thread can hold a run back from its time. The routine records it as its latest.

        **The seeds are whole-tool grants, including on a tool that runs commands.** There
        is no proposed command to scope one to, only a tool name the operator approved on
        the routine. A seed is left undecisive: it informs the review at the level that
        reviews rather than answering for it, since nobody is in front of the run."""
        conversation_id = await conversations.create_conversation(
            view.owner_id, title=view.title, agent_name=view.agent_name
        )
        for tool_name in view.pre_authorized:
            # A once-only tool is once-only here too: a standing grant on a per-call gate
            # does its damage exactly where nobody is reading what it covers.
            if tool_name not in ONCE_ONLY_TOOLS:
                _ = await grants.grant(view.owner_id, conversation_id, tool_name)
        await routines.attach_thread(view.id, conversation_id)
        return conversation_id

    async def _with_last_run(view: ScheduledTaskView) -> str:
        """The routine's prompt, followed by how its previous run ended, so each run
        picks up where the last left off without sharing its thread."""
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

    async def _task_executor(view: ScheduledTaskView) -> TaskRunResult:
        """A routine's fire: a turn in a new thread, run by the routine's agent through
        the same composition an interactive turn uses (`compose_turn`)."""
        conversation_id = await _open_thread(view)
        thread = await composer.resolve(view.owner_id, conversation_id)
        applied, models = thread.applied, thread.models
        # Nobody is watching, so tools that stop and wait for someone are withheld, and so
        # are features never set up.
        disabled = await composer.disabled(
            view.owner_id, kind=TASK_KIND, vision=models.vision, applied=applied, depth=0
        )
        thresholds = await get_context_thresholds(settings_store, view.owner_id)
        timeouts: WallClockOverride = {}
        if applied.agent.wall_clock_timeout_s is not None:
            timeouts["wall_clock_timeout_s"] = applied.agent.wall_clock_timeout_s
        auto_compact = await resolve_auto_compact_policy(settings_store, view.owner_id)
        prompt = await _with_last_run(view)
        waiter: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
        created = compose_turn(
            prompt=prompt,
            conversation_id=conversation_id,
            models=models,
            capabilities=ctx.capabilities,
            categories=ctx.tool_categories,
            dormant=DormantCategory.summaries(ctx.dormant_categories),
            instruction_providers=thread.instruction_providers,
            prompt_context_providers=ctx.prompt_context_providers,
            registry=runs,
            store=conversations,
            uploads=services.get(UploadStore),
            disabled_tools=disabled,
            binding=applied.binding,
            context_thresholds=thresholds,
            auto_compact=auto_compact,
            owner_id=view.owner_id,
            # Routines take the interactive lane (`runs/lanes.py`): they run on schedule.
            kind=TASK_KIND,
            **timeouts,
        )
        # Registered before the first await below, so the run cannot reach terminal first.
        run_waiters[created.run_id] = waiter
        # Waits for a terminal outcome: a fire parked for approval stays open until the
        # operator decides. Shutdown leaves a parked run for the next process, so this
        # wait ends with the scheduler's own stop.
        run = await waiter

        outcome = _TASK_OUTCOME_BY_RUN_STATUS.get(run.status, TaskOutcome.ERROR.value)
        summary = await _task_run_summary(run, conversation_id)
        if view.output == TaskOutput.NOTIFICATION.value:
            _ = await notifications.notify(
                view.owner_id,
                "task_outcome",
                view.title,
                body=summary,
                conversation_id=conversation_id,
                task_id=view.id,
            )
        return TaskRunResult(
            outcome=outcome,
            run_id=run.id,
            conversation_id=conversation_id,
            summary=summary,
        )

    async def _task_notify(view: ScheduledTaskView) -> None:
        """A reminder task's fire — its prompt delivered verbatim as the notification
        body (no AI phrasing in v1); title = the task's own title."""
        _ = await notifications.notify(
            view.owner_id,
            "reminder",
            view.title,
            body=view.prompt,
            task_id=view.id,
        )

    # Single-instance, in-process. Lock-aware like the write-behind drainers (task
    # prompts are encrypted): it parks its tick loop while the vault is locked and
    # resumes on unlock. This manifest builds after everything the executor reaches,
    # so the scheduler also stops before all of it — nothing may submit new runs
    # into a tearing-down process.
    scheduler = SchedulerService(
        ctx.engine,
        ctx.vault,
        executor=_task_executor,
        notify=_task_notify,
    )
    await ctx.lifecycle.start("scheduler", start=scheduler.start, stop=scheduler.stop)

    def _release_parked_fires() -> None:
        """A fire still parked at shutdown is kept for the next process, so the executor
        waiting on it is let go to close its `TaskRun` instead of holding the scheduler's
        stop for the whole drain."""
        for run_id, waiter in list(run_waiters.items()):
            run = runs.get(run_id)
            if run is not None and run.is_parked and not waiter.done():
                waiter.set_result(run)

    # Registered after the scheduler, so it runs before the scheduler's stop.
    ctx.lifecycle.on_stop("task-parked-fires", _release_parked_fires)

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
    # The executor composes the full interactive capability set, so the features
    # providing it must have built (and their services registered) first.
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
    # The inbound webhook trigger is auth-exempt: the per-task unguessable token in
    # the path is the credential, so an external caller can fire a task without the
    # operator's session. Every other `/tasks` route stays behind the gate.
    public_prefixes=("/tasks/hooks",),
    build=_build,
)
