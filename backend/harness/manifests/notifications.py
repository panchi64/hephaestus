"""The attention surface (`AE-3.2`) — a durable notification record + its own live
stream, separate from the frozen per-run event protocol.

The substrate records/streams what it's told; the *emit policy* — which run
outcomes are noteworthy — lives here too, as run-terminal hooks: the
approval-backstop resolve and the conversation-linked completion/failure notices.
Out-of-band channels (email through the operator's own mailbox, a push webhook)
compose in at construction.
"""

from __future__ import annotations

import logging

from core.api_scopes import ScopeClaim
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from harness.run_terminal import RunTerminalDispatcher
from routes import notifications as notifications_routes
from routes.deps import OPERATOR_ID
from runs import Run, RunStatus
from services.conversations import ConversationStore
from services.mail import MailService
from services.notification_channels import default_channels
from services.notifications import NotificationService
from services.settings_store import SettingsStore

logger = logging.getLogger(__name__)


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    conversations = ctx.services.get(ConversationStore)
    # The email channel takes the mail service as a *callable* rather than a value
    # because the dependency runs both ways — mail raises triage alerts through this
    # surface, and this surface sends through mail. Resolving late is what breaks
    # the cycle; the channel is only ever called long after both exist.
    notifications = NotificationService(
        ctx.engine,
        ctx.vault,
        channels=default_channels(
            lambda: ctx.services.get(MailService),
            ctx.services.get(SettingsStore),
            ctx.vault,
        ),
    )
    await ctx.lifecycle.start(
        "notifications", start=notifications.start, stop=notifications.stop
    )
    # In-flight run-terminal tasks are this surface's writers — cancel and await
    # them right before it stops (registered after ⇒ stops earlier), so a run
    # finishing at shutdown never leaves a task notifying through a stopped store.
    ctx.lifecycle.on_stop(
        "run-terminal-notifies", ctx.services.get(RunTerminalDispatcher).drain
    )

    async def _resolve_dangling_approvals(run: Run, watched: bool) -> None:
        """The backstop: a cancel-while-parked never reaches the approve route, and
        even a normal completion may still carry a dangling approval_needed if the
        operator never decided it."""
        try:
            await notifications.resolve_for_run(OPERATOR_ID, run.id)
        except Exception:
            logger.exception("notifications: failed to resolve run %s at terminal", run.id)

    async def _notify_conversation_terminal(run: Run, watched: bool) -> None:
        """Only conversation-linked runs notify (a stateless/detached run has no thread
        to deep-link to); cancelled and blocked outcomes stay silent — the operator asked
        for the cancel, and a bound/limit stop isn't a noteworthy failure.

        A **hidden** thread is the exception. A notification is a deep link, and an
        ephemeral conversation is deliberately kept out of the session list — so one
        finishing announces a thread the operator cannot find their way back to.

        Sub-agents are what made this worth fixing: a fan-out of six researchers finishing
        unwatched is six such notices at once, and what the operator watches is the card,
        which is where the report lands anyway. The thread that launched them still
        notifies for itself when its own turn ends.

        **Compare panes are ephemeral too, and are deliberately included.** They lose the
        `run_failed` notice they used to get unconditionally, which is the right trade for
        the same reason: the operator is looking at the pane, the failure is on it, and the
        link would have gone somewhere they cannot reach from the session list."""
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
            if run.status is RunStatus.error:
                await notifications.notify(
                    OPERATOR_ID,
                    "run_failed",
                    f'"{title}" hit an error',
                    body=run.error,
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                )
            elif run.status is RunStatus.done and not watched:
                # Only notify a plain completion when nobody was watching — a
                # subscriber attached to the run's own stream already saw it finish.
                await notifications.notify(
                    OPERATOR_ID,
                    "run_completed",
                    f'"{title}" finished',
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
