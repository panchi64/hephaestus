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
from services.parked_turns import SealedParkedStore, StoredPark

logger = logging.getLogger(__name__)


class _Journal(ParkedJournal):
    def __init__(self, store: SealedParkedStore) -> None:
        self._store: SealedParkedStore = store

    @override
    def record(self, run: Run) -> None:
        parked = run.parked_payload
        if not isinstance(parked, ParkedTurn) or run.conversation_id is None:
            return
        try:
            payload = encode_park(parked, run)
        except Exception:
            logger.exception("parked turns: run %s cannot be written, so it won't survive", run.id)
            return
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

    @override
    def forget(self, run_id: str) -> None:
        self._store.forget(run_id)


async def _restore(
    store: SealedParkedStore, runs: RunRegistry, conversations: ConversationStore
) -> None:
    for stored in await store.load():
        if runs.get(stored.run_id) is not None:
            continue
        try:
            parked, endpoint = decode_park(stored.payload)
        except Exception:
            logger.exception("parked turns: run %s could not be read back", stored.run_id)
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
        events, _, _ = announcements(parked.requests, parked.settled)
        runs.restore_parked(run, events)


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    runs = ctx.services.get(RunRegistry)
    conversations = ctx.services.get(ConversationStore)
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
        try:
            await _restore(store, runs, conversations)
        except Exception:
            logger.exception("parked turns: could not restore the stored parks")
        finally:
            runs.parks_restored.set()

    _ = ctx.lifecycle.track("parked-turns-restore", restore())
    return FeatureRuntime(services=(store,))


MANIFEST = FeatureManifest(name="parked-turns", build=_build)
