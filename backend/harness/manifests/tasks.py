"""The scheduled-tasks feature (`TASK-1..6`) — the scheduler and how a task fires.

An agent task executes as an ordinary Run in a fresh conversation, reusing the chat
turn's own composition so there is no forked run-submission path; a reminder task
fires its prompt verbatim as a notification. The executor learns a Run's outcome
through a waiter future resolved synchronously at the terminal transition — the
same dispatch everything else observes a run's outcome through.
"""

from __future__ import annotations

import asyncio

from agent.summarize import resolve_auto_compact_policy
from core.api_scopes import ScopeClaim
from harness.manifest import (
    DormantCategory,
    FeatureManifest,
    FeatureRuntime,
    HarnessContext,
)
from models.task import TaskOutcome, TaskOutput
from routes import tasks as tasks_routes
from routes.chat import compose_turn, resolve_turn_models
from runs import Run, RunRegistry, RunStatus
from services.approval_grants import ONCE_ONLY_TOOLS, ApprovalGrantStore
from services.conversations import ConversationStore
from services.notifications import NotificationService
from services.offline import OfflineModeService
from services.registry import ModelRegistry
from services.scheduler import ScheduledTaskView, SchedulerService, TaskRunResult
from services.settings_store import (
    SettingsStore,
    get_agent_request_limit_override,
    get_context_thresholds,
)
from services.tool_policy import effective_disabled_tools
from services.uploads import UploadStore

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

    async def _task_executor(view: ScheduledTaskView) -> TaskRunResult:
        """An agent task's fire — an ordinary Run in a fresh conversation (titled from
        the task), seeded with the task's own pre-authorization as a conversation
        grant (`AE-3.5`) so its unattended sensitive actions within that scope don't
        pause; anything outside it still parks + notifies exactly like an
        interactive run. Reuses `routes.chat`'s own turn composition
        (`resolve_turn_models`/`compose_turn`) so a task's run is submitted through
        the identical path a live chat turn is — no forked run-submission logic.

        **These seeds are whole-tool grants, including on a tool that runs commands** —
        the one place `services/approval_grants.py` still writes the empty scope for one
        of those, and deliberately. A grant recorded from an approval names the command
        the agent asked to run; here there is no such call yet, only a tool name the
        operator wrote on the task itself ahead of every turn it will ever take. Scoping
        it to a command nobody has proposed is not possible, and refusing the seed would
        leave a task parked forever on exactly the tool it was configured to use.

        **They are not the operator's wider pick, though they share its shape.** A seed is
        left undecisive, so at the level that reviews it informs the review exactly as it
        always has rather than answering for it. The width that answers is one a person
        chose on a card, in front of the act it would cover; nobody is in front of an
        unattended run, and the thread one opens is an ordinary conversation the operator
        can reopen later — so a seed promoted by its shape would go on authorizing past the
        single execution it was written for."""
        models = await resolve_turn_models(registry, None, None, owner_id=view.owner_id)
        conversation_id = await conversations.create_conversation(
            view.owner_id, title=view.title
        )
        for tool_name in view.pre_authorized:
            # A once-only tool is once-only here too. This is the other door onto the same
            # table, and the unattended run is exactly where a standing grant on a per-call
            # gate would do its damage: nobody is reading what it covers.
            if tool_name in ONCE_ONLY_TOOLS:
                continue
            await grants.grant(view.owner_id, conversation_id, tool_name)

        waiter: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
        created = compose_turn(
            prompt=view.prompt,
            conversation_id=conversation_id,
            models=models,
            # An unattended task reaches the same capabilities, tool catalog, and
            # dynamic instructions an interactive turn does — the app's one assembled
            # set of each, so the two can never diverge.
            capabilities=ctx.capabilities,
            categories=ctx.tool_categories,
            dormant=DormantCategory.summaries(ctx.dormant_categories),
            instruction_providers=ctx.instruction_providers,
            prompt_context_providers=ctx.prompt_context_providers,
            registry=runs,
            store=conversations,
            uploads=services.get(UploadStore),
            # An unattended task's turn honours the operator's disabled set exactly as an
            # interactive one does — a tool switched off is off everywhere, not just where
            # someone is watching.
            disabled_tools=await effective_disabled_tools(
                settings_store,
                offline,
                view.owner_id,
                # `models[4]` is the resolved main model's vision fact, the same one
                # `compose_turn` hands the engine for attachments. It is known here, so
                # the permissive default has no business standing in for it: a tool that
                # answers with an image is withheld from a model that cannot read one,
                # whether or not anyone is watching the turn.
                vision=models[4],
                # And the reason the operator is not asked anything: this is the lane
                # whose whole premise is that nobody is watching, so a tool that would
                # suspend the turn on them is withheld rather than left to hang.
                kind="task",
                # And the same for a feature the operator never set up: nobody is here
                # to connect a mailbox mid-run, so offering `mail_*` to a task that will
                # only be told there is no account is worse here than anywhere.
                availability=ctx.category_availability,
                caps=ctx.capabilities,
            ),
            # Same reasoning for the per-turn model-request ceiling: an unattended task
            # runs under the operator's own setting when they set one, and otherwise
            # under the config default.
            request_limit=await get_agent_request_limit_override(
                settings_store, view.owner_id
            ),
            # And the context gauge's boundaries, so a task's thread reddens at the same
            # fullness an interactive one does — the readout is read in the same UI.
            context_thresholds=await get_context_thresholds(settings_store, view.owner_id),
            # And the same for conversation compaction. Inert today — each fire starts a
            # fresh conversation, so there is never history to fold — but passing the
            # operator's policy rather than letting it default keeps the unattended path
            # from quietly disagreeing with the interactive one the day a task's thread
            # does carry history.
            auto_compact=await resolve_auto_compact_policy(settings_store, view.owner_id),
            owner_id=view.owner_id,
            # Unattended by definition, so it queues in the background lane and can never
            # hold a slot the operator's own next turn is waiting for (`runs/lanes.py`).
            kind="task",
        )
        # Registered before the very first `await` below — the newly submitted Run's
        # task hasn't had a chance to run yet (`RunRegistry.submit` only schedules
        # it), so there is no window for it to reach terminal and fire the terminal
        # dispatch before this waiter exists.
        run_waiters[created.run_id] = waiter
        # Deliberately waits for a *terminal* outcome, not merely for the turn's task to
        # end: a fire that parks for approval is unfinished, and its `TaskRun` row stays
        # open until the operator decides. What keeps that from stranding the process at
        # shutdown is `RunRegistry.shutdown` — registered to stop before this one, it
        # cancels every live run, which fires the terminal hook that resolves this waiter.
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
    return FeatureRuntime(
        services=(scheduler,),
        state={"scheduler": scheduler, "task_run_waiters": run_waiters},
        run_terminal_sync=(_resolve_waiter,),
    )


MANIFEST = FeatureManifest(
    name="tasks",
    # The executor composes the full interactive capability set, so the features
    # providing it must have built (and their services registered) first.
    after=(
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
    api_scopes=(ScopeClaim("tasks", ("/tasks",)),),
    # The inbound webhook trigger is auth-exempt: the per-task unguessable token in
    # the path is the credential, so an external caller can fire a task without the
    # operator's session. Every other `/tasks` route stays behind the gate.
    public_prefixes=("/tasks/hooks",),
    build=_build,
)
