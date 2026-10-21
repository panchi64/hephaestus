"""Parked runs that survive a restart: the sealed store behind the registry's park journal,
and the pass that puts every stored park back once the vault opens.

A run waiting on an approval or a question is one row. A restart, clean or not, leaves it
there, and the restored run waits in ``awaiting_input`` with the events it was waiting on
replayed into its stream. What a row cannot hold (the agent, the compaction context) is
rebuilt on approval by the hook its kind's composer registered (``agent/recompose.py``).
"""

from __future__ import annotations

import logging
from functools import partial
from typing import override

from agent.finalize import persist_parked_cancel
from agent.parked_codec import decode_park, encode_park
from agent.parking import ParkedTurn, announcements
from agent.recompose import ParkedFacts, Recomposed, register_recompose
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from routes.chat.submit import recompose_chat
from runs import ParkedJournal, Run, RunRegistry, RunStream
from services.conversations import ConversationStore
from services.notifications import NotificationService
from services.parked_turns import SealedParkedStore, StoredPark

logger = logging.getLogger(__name__)


class _Journal(ParkedJournal):
    def __init__(self, store: SealedParkedStore) -> None:
        self._store: SealedParkedStore = store

    @override
    def record(self, run: Run) -> bool:
        parked = run.parked_payload
        if not isinstance(parked, ParkedTurn) or run.conversation_id is None:
            return False
        try:
            payload = encode_park(parked, run)
        except Exception:
            logger.exception("parked turns: run %s cannot be written, so it won't survive", run.id)
            return False
        self._store.record(
            StoredPark(
                run_id=run.id,
                owner_id=run.owner_id,
                conversation_id=run.conversation_id,
                kind=run.kind,
                prompt_message_id=parked.prompt_message_id,
                last_event_id=run.stream.last_seq,
                payload=payload,
            )
        )
        return True

    @override
    def forget(self, run_id: str) -> None:
        self._store.forget(run_id)


async def _abandon(
    stored: StoredPark,
    runs: RunRegistry,
    store: SealedParkedStore,
    notifications: NotificationService,
) -> None:
    """A park that cannot be read back is dropped for good rather than retried every boot.
    Its run ends blocked, so what waits on a run's end (its routine's `TaskRun`, a
    sub-agent's parent) hears of it, and the operator is told once."""
    if runs.get(stored.run_id) is not None:
        store.forget(stored.run_id)
        return
    reason = "a run waiting for approval could not be restored after the restart"
    runs.end_unrestorable(
        Run(
            id=stored.run_id,
            kind=stored.kind,
            owner_id=stored.owner_id,
            conversation_id=stored.conversation_id,
            stream=RunStream(stored.last_event_id),
        ),
        reason,
    )
    _ = await notifications.notify(
        stored.owner_id,
        "run_restore_failed",
        "A run could not be restored",
        body=reason,
        conversation_id=stored.conversation_id,
    )


async def _restore(
    store: SealedParkedStore,
    runs: RunRegistry,
    conversations: ConversationStore,
    notifications: NotificationService,
) -> None:
    """Every readable park is registered before any unreadable one is ended, since ending
    a child run can wake the parent whose own park is still to be restored."""
    lost: list[StoredPark] = []
    vetoed: list[tuple[Run, str]] = []
    for stored in await store.load():
        if runs.get(stored.run_id) is not None:
            continue
        try:
            if stored.payload is None:
                raise ValueError("the sealed payload could not be opened")
            parked, endpoint = decode_park(stored.payload, kind=stored.kind)
            # The turn is written into the thread's cached tree when it ends, and an
            # uncached thread would be written as a second root.
            _ = await conversations.tree(stored.conversation_id)
        except Exception:
            logger.exception("parked turns: run %s could not be read back", stored.run_id)
            lost.append(stored)
            continue
        run = Run(
            id=stored.run_id,
            kind=stored.kind,
            owner_id=stored.owner_id,
            conversation_id=stored.conversation_id,
            endpoint=endpoint,
            prompt_message_id=stored.prompt_message_id,
            stream=RunStream(stored.last_event_id),
        )
        run.park(parked)
        run.on_park_cancel = partial(persist_parked_cancel, run, store=conversations)
        if (problem := await runs.restore_problem(run)) is not None:
            vetoed.append((run, problem))
            continue
        events, _, _ = announcements(parked.requests, parked.settled)
        runs.restore_parked(run, events)
    for stored in lost:
        await _abandon(stored, runs, store, notifications)
    for run, problem in vetoed:
        runs.end_unrestorable(run, problem)


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    runs = ctx.services.get(RunRegistry)
    conversations = ctx.services.get(ConversationStore)
    notifications = ctx.services.get(NotificationService)
    store = SealedParkedStore(ctx.engine, ctx.vault)
    await ctx.lifecycle.start("parked-turns", start=store.start, stop=store.stop)
    runs.attach_parked_journal(_Journal(store))
    app = ctx.app
    if app is not None:

        async def recompose(facts: ParkedFacts) -> Recomposed:
            return await recompose_chat(app, facts)

        register_recompose(runs, "chat", recompose)

    async def restore() -> None:
        _ = await ctx.vault.unlocked_event.wait()
        _ = await ctx.built.wait()
        try:
            await _restore(store, runs, conversations, notifications)
        except Exception:
            logger.exception("parked turns: could not restore the stored parks")
        finally:
            runs.parks_restored.set()

    _ = ctx.lifecycle.track("parked-turns-restore", restore())
    return FeatureRuntime(services=(store,))


MANIFEST = FeatureManifest(name="parked-turns", after=("notifications",), build=_build)
