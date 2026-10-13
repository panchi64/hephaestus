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
from routes.chat import compose_turn, resolve_turn_models
from routes.deps import OPERATOR_ID
from runs import Run, RunRegistry, RunStatus
from services.agents import AgentCatalog, lower_of
from services.agents.thread import DELEGATION_TOOLS, may_delegate, resolve_thread_agent
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
    get_agent_request_limit_override,
    get_context_thresholds,
)
from services.tool_policy import effective_disabled_tools
from services.uploads import UploadStore
from tools.routines import GATED_TOOLS as ROUTINES_GATED
from tools.routines import routines_toolset

# A scheduled task's outcome summary is a short factual line, not a transcript —
# just enough for the operator to judge at a glance whether to open the conversation.
_TASK_SUMMARY_MAX_CHARS = 280

# `TaskRun.outcome` from the Run status it settled at — the three failure-shaped
# statuses map onto the matching `TaskOutcome` verbatim; `cancelled` covers both an
# operator-cancelled run and one still parked (never approved/denied) at shutdown.
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
    settings_store = services.get(SettingsStore)
    offline = services.get(OfflineModeService)
    agents = services.get(AgentCatalog)
    routines = RoutineStore(ctx.engine, ctx.vault, agents)

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
                await grants.grant(view.owner_id, conversation_id, tool_name)
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
        stamp = as_utc(previous.finished_at).strftime("%Y-%m-%d %H:%M UTC")
        last = wrap_untrusted(previous.summary, source="the routine's previous run")
        return f"{view.prompt}\n\nHow the previous run ended ({stamp}, {previous.outcome}):\n{last}"

    async def _task_executor(view: ScheduledTaskView) -> TaskRunResult:
        """A routine's fire: a turn in a new thread, run by the routine's agent through
        the same composition an interactive turn uses (`compose_turn`)."""
        conversation_id = await _open_thread(view)
        applied = await resolve_thread_agent(
            agents,
            view.owner_id,
            await conversations.binding(conversation_id),
            conversation_id,
            projects=services.get_optional(ProjectStore),
            worktrees=services.get_optional(WorktreeManager),
        )
        models = await resolve_turn_models(
            registry, applied.agent.endpoint_id, applied.agent.model, owner_id=view.owner_id
        )
        disabled = await effective_disabled_tools(
            settings_store,
            offline,
            view.owner_id,
            vision=models[4],
            # Nobody is watching, so tools that stop and wait for someone are withheld,
            # and so are features never set up.
            kind="task",
            availability=ctx.category_availability,
            caps=ctx.capabilities,
        )
        if not may_delegate(applied.agent, 0, ctx.settings.agent_max_depth):
            disabled |= DELEGATION_TOOLS
        request_limit = lower_of(
            applied.agent.request_limit,
            await get_agent_request_limit_override(settings_store, view.owner_id),
        )
        thresholds = await get_context_thresholds(settings_store, view.owner_id)
        timeouts = {}
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
            instruction_providers=(*ctx.instruction_providers, *applied.instructions),
            prompt_context_providers=ctx.prompt_context_providers,
            registry=runs,
            store=conversations,
            uploads=services.get(UploadStore),
            disabled_tools=disabled | applied.withheld,
            binding=applied.binding,
            request_limit=request_limit,
            context_thresholds=thresholds,
            auto_compact=auto_compact,
            owner_id=view.owner_id,
            # Routines take the interactive lane (`runs/lanes.py`): they run on schedule.
            kind="task",
            **timeouts,
        )
        # Registered before the first await below, so the run cannot reach terminal first.
        run_waiters[created.run_id] = waiter
        # Waits for a terminal outcome: a fire parked for approval stays open until the
        # operator decides, and shutdown cancels live runs, which resolves this.
        run = await waiter

        outcome = _TASK_OUTCOME_BY_RUN_STATUS.get(run.status, TaskOutcome.ERROR.value)
        summary = await _task_run_summary(run, conversation_id)
        if view.output == TaskOutput.NOTIFICATION.value:
            await notifications.notify(
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
        await notifications.notify(
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
            "running one now",
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
