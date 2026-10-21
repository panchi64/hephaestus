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

from agent.finalize import persist_parked_cancel, stamp_parked_block
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

_LOST = "a run waiting for approval could not be restored after the restart"
_BUSY = "its conversation already has a run in progress"


class _Journal(ParkedJournal):
    def __init__(self, store: SealedParkedStore, conversations: ConversationStore) -> None:
        self._store: SealedParkedStore = store
        self._conversations: ConversationStore = conversations
        self._threads: dict[str, str] = {}

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
        self.hold(run.id, run.conversation_id)
        return True

    def hold(self, run_id: str, conversation_id: str) -> None:
        """Keep the thread's tree cached for as long as the run is parked."""
        self._threads[run_id] = conversation_id
        self._conversations.pin(conversation_id)

    @override
    def forget(self, run_id: str) -> None:
        self._store.forget(run_id)
        conversation_id = self._threads.pop(run_id, None)
        if conversation_id is not None and conversation_id not in self._threads.values():
            self._conversations.unpin(conversation_id)


def _rebuild(stored: StoredPark, conversations: ConversationStore) -> Run:
    """The parked run a stored park describes. Raises when it cannot be read."""
    if stored.payload is None:
        raise ValueError("the sealed payload could not be opened")
    parked, endpoint = decode_park(stored.payload, kind=stored.kind)
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
    return run


async def _restore(
    store: SealedParkedStore,
    runs: RunRegistry,
    journal: _Journal,
    conversations: ConversationStore,
    notifications: NotificationService,
) -> None:
    """Every readable park is registered before any other is ended, since ending a child
    run can wake the parent whose own park is still to be restored. One park failing
    never stops the rest."""
    ending: list[tuple[Run, str]] = []
    for stored in await store.load():
        if runs.get(stored.run_id) is not None:
            continue
        try:
            run = _rebuild(stored, conversations)
        except Exception:
            logger.exception("parked turns: run %s could not be read back", stored.run_id)
            ending.append(
                (
                    Run(
                        id=stored.run_id,
                        kind=stored.kind,
                        owner_id=stored.owner_id,
                        conversation_id=stored.conversation_id,
                        stream=RunStream(stored.last_event_id),
                    ),
                    _LOST,
                )
            )
            continue
        try:
            problem = await runs.restore_problem(run)
            if problem is None and runs.active_run_for(stored.conversation_id, run.owner_id):
                # The live run owns the thread's leaf, so the park leaves only a marker on
                # its prompt instead of its turn.
                run.on_park_cancel = partial(stamp_parked_block, run, store=conversations)
                problem = _BUSY
            if problem is not None:
                ending.append((run, problem))
                continue
            parked = run.parked_payload
            assert isinstance(parked, ParkedTurn)
            events, _, _ = announcements(parked.requests, parked.settled)
            runs.restore_parked(run, events)
            journal.hold(run.id, stored.conversation_id)
        except Exception:
            logger.exception("parked turns: run %s could not be restored", stored.run_id)
    for run, reason in ending:
        try:
            runs.end_unrestorable(run, reason)
            _ = await notifications.notify(
                run.owner_id,
                "run_restore_failed",
                "A run could not be restored",
                body=reason,
                conversation_id=run.conversation_id,
            )
        except Exception:
            logger.exception("parked turns: run %s could not be ended", run.id)


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    runs = ctx.services.get(RunRegistry)
    conversations = ctx.services.get(ConversationStore)
    notifications = ctx.services.get(NotificationService)
    store = SealedParkedStore(ctx.engine, ctx.vault)
    await ctx.lifecycle.start("parked-turns", start=store.start, stop=store.stop)
    journal = _Journal(store, conversations)
    runs.attach_parked_journal(journal)
    app = ctx.app
    if app is not None:

        async def recompose(facts: ParkedFacts) -> Recomposed:
            return await recompose_chat(app, facts)

        register_recompose(runs, "chat", recompose)

    async def restore() -> None:
        _ = await ctx.vault.unlocked_event.wait()
        _ = await ctx.built.wait()
        try:
            await _restore(store, runs, journal, conversations, notifications)
        except Exception:
            logger.exception("parked turns: could not restore the stored parks")
        finally:
            runs.parks_restored.set()

    _ = ctx.lifecycle.track("parked-turns-restore", restore())
    return FeatureRuntime(services=(store,))


MANIFEST = FeatureManifest(name="parked-turns", after=("notifications",), build=_build)
