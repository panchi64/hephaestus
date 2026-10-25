"""Conversation inboxes: the sealed journal that keeps them across a restart, and the
dispatcher that starts the next turn from one when its thread goes idle.

The inboxes themselves are the run substrate's (``runs/inbox.py``); this wires what lives
above it.
"""

from __future__ import annotations

import logging

from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from harness.manifests._inbox_dispatch import InboxDispatcher
from routes.inbox import router as inbox_router
from runs import RunRegistry
from services.inbox_journal import SealedInboxJournal

logger = logging.getLogger(__name__)


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    runs = ctx.services.get(RunRegistry)
    journal = SealedInboxJournal(ctx.engine, ctx.vault)
    await ctx.lifecycle.start("inbox-journal", start=journal.start, stop=journal.stop)
    runs.attach_journal(journal)
    dispatcher = InboxDispatcher(ctx)
    # Stops before the journal (registered after it), so a dispatch in flight finishes its
    # inbox writes first.
    ctx.lifecycle.on_stop("inbox-dispatch", dispatcher.drain)

    async def restore() -> None:
        """Once the key is in, put back what a previous process left queued and start the
        turns it was waiting for."""
        _ = await ctx.vault.unlocked_event.wait()
        _ = await runs.parks_restored.wait()
        try:
            loaded = await journal.restore_into(runs)
        except Exception:
            logger.exception("inbox: could not restore the stored inboxes")
            return
        for each in loaded:
            await dispatcher.dispatch(each.owner_id, each.conversation_id)

    _ = ctx.lifecycle.track("inbox-restore", restore())
    return FeatureRuntime(
        services=(journal, dispatcher),
        state={"inbox_dispatcher": dispatcher},
        run_terminal_sync=(dispatcher.observed,),
        run_terminal=(dispatcher.settled,),
    )


MANIFEST = FeatureManifest(
    name="inbox",
    composes_turns=True,
    routers=(inbox_router,),
    # Starting a turn composes a full interactive one, so every feature contributing to
    # that turn's capability set must have built first.
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
    build=_build,
)
