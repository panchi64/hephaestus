"""How a routine fires: an agent routine is an ordinary Run in a new thread each time, by the
routine's agent, through the chat turn's own composition (no forked submission path); a
reminder fires its prompt verbatim as a notification. The executor learns a Run's outcome
from a waiter future resolved synchronously at the terminal transition.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from core.exceptions import NotFoundError
from core.serde import as_utc
from core.untrusted import wrap_untrusted
from harness.turns import UnattendedTurns
from runs import Run, RunRegistry, RunStatus
from runs.registry import UNSET
from services.approval_grants import ONCE_ONLY_TOOLS, ApprovalGrantStore
from services.conversations import ConversationStore
from services.notifications import NotificationService

from .models import TaskOutcome, TaskOutput
from .scheduler import ScheduledTaskView, SchedulerService, TaskRunResult, TaskRunSuspended
from .services import RoutineStore

logger = logging.getLogger(__name__)

# A one-line outcome, enough to judge at a glance whether to open the conversation.
_TASK_SUMMARY_MAX_CHARS = 280

#: The kind a routine's run is submitted under (``runs/lanes.py``).
TASK_KIND = "task"

# `TaskRun.outcome` by the Run status it settled at.
_TASK_OUTCOME_BY_RUN_STATUS = {
    RunStatus.done: TaskOutcome.OK.value,
    RunStatus.error: TaskOutcome.ERROR.value,
    RunStatus.blocked: TaskOutcome.BLOCKED.value,
    RunStatus.cancelled: TaskOutcome.CANCELLED.value,
}


class RunWaiters:
    """By run id: the executor awaits its future for a genuinely terminal state, possibly
    long after an approval park and resume. Resolved synchronously at the terminal
    transition, so there is no polling loop."""

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[Run]] = {}

    def register(self, run_id: str, waiter: asyncio.Future[Run]) -> None:
        self._futures[run_id] = waiter

    def run_ids(self) -> list[str]:
        return list(self._futures)

    def resolve(self, run: Run) -> bool:
        """Whether an executor was waiting on this run."""
        waiter = self._futures.pop(run.id, None)
        if waiter is None:
            return False
        if not waiter.done():
            waiter.set_result(run)
        return True

    def release_parked(self, runs: RunRegistry) -> None:
        """A fire parked when the scheduler stops is released (leaving the `TaskRun` open)
        rather than holding the stop. Its waiter is dropped too, so if the run finishes in
        this process it is settled as a restored run is."""
        for run_id, waiter in list(self._futures.items()):
            run = runs.get(run_id)
            if run is not None and run.is_parked and not waiter.done():
                del self._futures[run_id]
                waiter.set_result(run)


class RoutineFires:
    def __init__(
        self,
        *,
        conversations: ConversationStore,
        grants: ApprovalGrantStore,
        notifications: NotificationService,
        routines: RoutineStore,
        turns: UnattendedTurns,
        waiters: RunWaiters,
    ) -> None:
        self._conversations: ConversationStore = conversations
        self._grants: ApprovalGrantStore = grants
        self._notifications: NotificationService = notifications
        self._routines: RoutineStore = routines
        self._turns: UnattendedTurns = turns
        self._waiters: RunWaiters = waiters
        self._scheduler: SchedulerService | None = None
        self._settling: set[asyncio.Task[None]] = set()

    def attach(self, scheduler: SchedulerService) -> None:
        self._scheduler = scheduler

    @property
    def scheduler(self) -> SchedulerService:
        assert self._scheduler is not None
        return self._scheduler

    async def routine_gone(self, run_id: str) -> str | None:
        if await self.scheduler.records_run(run_id):
            return None
        return "the routine this run belonged to no longer exists"

    async def _run_summary(self, run: Run, conversation_id: str | None) -> str | None:
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
            turns = await self._conversations.messages_view(conversation_id)
            for turn in reversed(turns):
                if turn.role == "assistant" and turn.content:
                    return turn.content[:_TASK_SUMMARY_MAX_CHARS]
            return None
        return None

    async def _open_thread(self, view: ScheduledTaskView) -> str:
        """A new thread for this run, by the routine's agent, seeded with its
        pre-authorizations. A fresh thread is never busy, so nothing holds a run from its
        time. The routine records it as its latest.

        The seeds are whole-tool grants, even on a tool that runs commands: only a tool name
        was approved, with no command to scope to. A seed is undecisive: at the reviewing
        level it informs the review rather than answering for it, as nobody watches the run."""
        conversation_id = await self._conversations.create_conversation(
            view.owner_id, title=view.title, agent_name=view.agent_name
        )
        for tool_name in view.pre_authorized:
            # Once-only stays once-only: a standing grant on a per-call gate does its damage
            # where nobody reads what it covers.
            if tool_name not in ONCE_ONLY_TOOLS:
                _ = await self._grants.grant(view.owner_id, conversation_id, tool_name)
        await self._routines.attach_thread(view.id, conversation_id)
        return conversation_id

    async def _with_last_run(self, view: ScheduledTaskView) -> str:
        """The routine's prompt plus how its previous run ended, so runs continue across threads."""
        try:
            recent = await self._routines.runs(view.owner_id, view.id, limit=10)
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
        self, view: ScheduledTaskView, conversation_id: str | None, summary: str | None
    ) -> None:
        if view.output == TaskOutput.NOTIFICATION.value:
            _ = await self._notifications.notify(
                view.owner_id,
                "task_outcome",
                view.title,
                body=summary,
                conversation_id=conversation_id,
                task_id=view.id,
            )

    async def execute(self, view: ScheduledTaskView) -> TaskRunResult:
        """A routine's fire: a turn in a new thread by the routine's agent, composed as an
        interactive turn is (`compose_turn`)."""
        scheduler = self.scheduler
        composer = self._turns.composer
        conversation_id = await self._open_thread(view)
        thread = await composer.resolve(view.owner_id, conversation_id)
        applied, models = thread.applied, thread.models
        # Nobody watches: tools that wait for someone are withheld, as are unconfigured features.
        disabled = await composer.disabled(
            view.owner_id, kind=TASK_KIND, vision=models.vision, applied=applied, depth=0
        )
        wall_clock = applied.agent.wall_clock_timeout_s
        prompt = await self._with_last_run(view)
        waiter: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
        run_id = uuid4().hex
        # Linked first so a restored run always finds its row and a failed link starts nothing.
        if view.task_run_id is not None:
            await scheduler.link_task_run(
                view.task_run_id, run_id=run_id, conversation_id=conversation_id
            )
        try:
            created = await self._turns.submit(
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
        self._waiters.register(created.run_id, waiter)
        # A parked fire stays open until the operator decides. Shutdown releases the wait
        # with the run still parked; the `TaskRun` stays open for whoever settles the restored run.
        run = await waiter
        if run.is_parked:
            raise TaskRunSuspended

        outcome = _TASK_OUTCOME_BY_RUN_STATUS.get(run.status, TaskOutcome.ERROR.value)
        summary = await self._run_summary(run, conversation_id)
        await self._notify_outcome(view, conversation_id, summary)
        return TaskRunResult(
            outcome=outcome,
            run_id=run.id,
            conversation_id=conversation_id,
            summary=summary,
        )

    async def notify(self, view: ScheduledTaskView) -> None:
        """A reminder's fire: its prompt verbatim as the notification body, task title as title."""
        _ = await self._notifications.notify(
            view.owner_id,
            "reminder",
            view.title,
            body=view.prompt,
            task_id=view.id,
        )

    async def _settle_restored(self, run: Run) -> None:
        """A restored routine run has no waiting executor; its end finalizes its `TaskRun` here."""
        try:
            outcome = _TASK_OUTCOME_BY_RUN_STATUS.get(run.status, TaskOutcome.ERROR.value)
            summary = await self._run_summary(run, run.conversation_id)
            view = await self.scheduler.settle_restored(run.id, outcome=outcome, summary=summary)
            if view is not None:
                await self._notify_outcome(view, run.conversation_id, summary)
        except Exception:
            logger.exception("tasks: could not settle the TaskRun of restored run %s", run.id)

    def on_run_terminal(self, run: Run) -> None:
        if self._waiters.resolve(run):
            return
        if run.kind == TASK_KIND:
            settle = asyncio.create_task(self._settle_restored(run), name=f"task-settle:{run.id}")
            self._settling.add(settle)
            settle.add_done_callback(self._settling.discard)

    async def drain_settling(self) -> None:
        _ = await asyncio.gather(*list(self._settling), return_exceptions=True)
