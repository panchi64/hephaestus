"""The attention surface: a durable notification record and its own live stream, separate
from the per-run event protocol.

The emit policy (which run outcomes are noteworthy) lives here as run-terminal hooks: the
approval-backstop resolve and the conversation-linked completion/failure notices. Core's
push channel composes in at construction; a feature adds its own at build.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from core.api_scopes import ScopeClaim
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from harness.run_terminal import RunTerminalDispatcher
from models.notification import NotificationKind
from routes import notifications as notifications_routes
from routes.deps import OPERATOR_ID
from runs import Run, RunStatus
from services.agents.definition import agent_display_name
from services.conversations import ConversationStore
from services.notification_channels import default_channels
from services.notifications import NotificationService
from services.settings_store import SettingsStore

logger = logging.getLogger(__name__)

#: A conversation whose saves keep failing is announced once per this window.
SAVE_FAILED_WINDOW_S = 600.0


def save_failure_reporter(
    notifications: NotificationService,
    *,
    window_s: float = SAVE_FAILED_WINDOW_S,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[str], None]:
    """A store drop listener that raises a `save_failed` notice, at most once per
    conversation per window. It must be called with an event loop running."""
    last: dict[str, float] = {}
    tasks: set[asyncio.Task[None]] = set()

    async def _notify(conversation_id: str) -> None:
        try:
            _ = await notifications.notify(
                OPERATOR_ID,
                NotificationKind.SAVE_FAILED,
                "A conversation could not be saved",
                body="The latest messages were not written to disk and may be lost on restart.",
                conversation_id=conversation_id,
            )
        except Exception:
            logger.exception("notifications: failed to report a save failure")

    def report(conversation_id: str) -> None:
        now = clock()
        previous = last.get(conversation_id)
        if previous is not None and now - previous < window_s:
            return
        last[conversation_id] = now
        task = asyncio.get_running_loop().create_task(_notify(conversation_id))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    return report


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    conversations = ctx.services.get(ConversationStore)
    notifications = NotificationService(
        ctx.engine,
        ctx.vault,
        channels=default_channels(ctx.services.get(SettingsStore), ctx.vault),
    )
    await ctx.lifecycle.start("notifications", start=notifications.start, stop=notifications.stop)
    conversations.set_save_failed_listener(save_failure_reporter(notifications))
    # In-flight run-terminal tasks write here; registered after so they stop first, and a run
    # finishing at shutdown never notifies through a stopped store.
    ctx.lifecycle.on_stop("run-terminal-notifies", ctx.services.get(RunTerminalDispatcher).drain)

    async def _resolve_dangling_approvals(run: Run, _watched: bool) -> None:
        """The backstop: a cancel-while-parked never reaches the approve route, and a normal
        completion may still carry an undecided approval_needed."""
        try:
            _ = await notifications.resolve_for_run(OPERATOR_ID, run.id)
        except Exception:
            logger.exception("notifications: failed to resolve run %s at terminal", run.id)

    async def _notify_conversation_terminal(run: Run, watched: bool) -> None:
        """Only conversation-linked runs notify (a detached run has no thread to deep-link);
        cancelled and blocked outcomes stay silent, being asked-for or non-failure stops.

        A hidden thread is the exception: a notification is a deep link, and an ephemeral
        conversation is kept out of the session list, so announcing one points somewhere
        unreachable. This matters for sub-agents (a fan-out of six unwatched would raise six
        notices while the operator watches the cards; the launching thread still notifies
        when its own turn ends). Compare panes are ephemeral too and lose their
        `run_failed` notice by design: the failure is on the pane being watched."""
        if run.conversation_id is None or run.status in (RunStatus.cancelled, RunStatus.blocked):
            return
        try:
            if await conversations.is_hidden(run.conversation_id):
                return
        except Exception:
            logger.exception("notifications: failed to read run %s's thread", run.id)
            return
        try:
            summary = await conversations.get_summary(run.conversation_id, OPERATOR_ID)
            title = summary.title if summary is not None and summary.title else "this conversation"
            agent = agent_display_name(summary.agent_name if summary is not None else None)
            if run.status is RunStatus.error:
                _ = await notifications.notify(
                    OPERATOR_ID,
                    NotificationKind.RUN_FAILED,
                    f'{agent} hit an error in "{title}"',
                    body=run.error,
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                )
            elif run.status is RunStatus.done and not watched:
                # Only notify a plain completion when nobody was watching — a
                # subscriber attached to the run's own stream already saw it finish.
                _ = await notifications.notify(
                    OPERATOR_ID,
                    "run_completed",
                    f'{agent} finished "{title}"',
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                )
        except Exception:
            logger.exception("notifications: failed to notify run %s at terminal", run.id)

    return FeatureRuntime(
        services=(notifications,),
        # Agent-facing so the engine's park path can notify — the engine resolves it
        # from the bag rather than carrying a dedicated notifier parameter.
        capabilities=(notifications,),
        state={"notifications": notifications},
        run_terminal=(_resolve_dangling_approvals, _notify_conversation_terminal),
    )


MANIFEST = FeatureManifest(
    name="notifications",
    routers=(notifications_routes.router,),
    api_scopes=(ScopeClaim("tasks", ("/notifications",)),),
    build=_build,
)
