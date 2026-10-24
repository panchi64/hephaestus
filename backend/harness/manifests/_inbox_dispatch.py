"""Starting the next turn from a conversation's inbox (``runs/inbox.py``).

A live run drains its thread's inbox at every model request, so this only acts on a thread
with nothing running: when a run ends, when a delivery lands on an idle thread, and on boot
for whatever the journal held across a restart.

**What the turn opens on** is the inbox's lead. An operator message opens a turn composed
exactly as if they had just sent it (``routes/chat/queued.py``). Anything else opens an
unattended turn, the same kind a sub-agent wake opens. Everything else deliverable rides
into that turn's first request, in delivery order.

**Three things keep a thread idle with messages waiting.** The operator's Stop holds it
until they act again. A delegation tree that has taken :data:`WAKE_CHAIN_LIMIT` turns
without the operator stops driving itself. And a thread no turn can be opened on (its
endpoint or agent is gone) stops trying. In the last two, deliveries wait in their inboxes
and go out with the operator's next turn in the tree. None of them drops anything.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from contextlib import suppress

from fastapi import FastAPI, HTTPException

from agent.injected import injected_text
from agent.recompose import ParkedFacts, Recomposed, register_recompose
from agent.summarize import resolve_auto_compact_policy
from harness.manifest import DormantCategory, HarnessContext
from models.subagent import SubagentRecord
from routes.chat import (
    ConversationBusyError,
    WallClockOverride,
    compose_turn,
    submit_queued,
)
from routes.chat.thread_composer import ThreadComposer
from routes.deps import app_state
from runs import QueuedMessage, Run, RunRegistry
from services.agents import AgentCatalog, lower_of
from services.conversations import ConversationStore
from services.notifications import NotificationService
from services.offline import OfflineModeService
from services.projects import ProjectStore, WorktreeManager
from services.registry import ModelRegistry
from services.settings_store import SettingsStore, get_context_thresholds
from services.subagent_store import SubagentStore
from services.uploads import UploadStore
from services.workspace import split_workspace_key

logger = logging.getLogger(__name__)

#: The kind a turn opened by an agent's delivery is submitted under (``runs/lanes.py``).
WAKE_KIND = "wake"

#: How many turns a delegation tree may take without the operator saying anything, before
#: wakes stop. A safety rail rather than a setting: every wake is a full turn against a
#: large context, and agents waking each other need no human.
WAKE_CHAIN_LIMIT = 25

#: How many thread→root mappings are remembered.
_ROOTS_CACHED = 512

#: How many message-to-ledger-prompt notes are remembered.
_LEDGER_NOTES_CACHED = 512

#: The longest :meth:`InboxDispatcher.settle` waits on each thing it waits for.
SETTLE_TIMEOUT_S = 3.0


class InboxDispatcher:
    """Starts the turn that takes a waiting inbox, for a thread with nothing running."""

    def __init__(self, ctx: HarnessContext) -> None:
        self._ctx: HarnessContext = ctx
        self._runs: RunRegistry = ctx.services.get(RunRegistry)
        self._conversations: ConversationStore = ctx.services.get(ConversationStore)
        self._models: ModelRegistry = ctx.services.get(ModelRegistry)
        self._settings: SettingsStore = ctx.services.get(SettingsStore)
        self._offline: OfflineModeService = ctx.services.get(OfflineModeService)
        self._uploads: UploadStore = ctx.services.get(UploadStore)
        self._agents: AgentCatalog = ctx.services.get(AgentCatalog)
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        # Consecutive unattended turns per tree root, cleared whenever the operator drives
        # the tree. In memory on purpose: a rail against a runaway loop, not a fact.
        self._chain: OrderedDict[str, int] = OrderedDict()
        self._roots: OrderedDict[str, str] = OrderedDict()
        # The (ledger thread, prompt) a delivered message was sent under, by message id. A
        # restart drops it and the wake falls back to the prompt stored on the agent's row.
        self._message_ledger: OrderedDict[str, tuple[str, str]] = OrderedDict()
        # Threads of each tree left waiting at the ceiling, by root.
        self._stalled: dict[str, set[tuple[str, str]]] = {}
        self._kicks: set[asyncio.Task[None]] = set()
        self._composer: ThreadComposer = ThreadComposer(
            agents=self._agents,
            model_registry=self._models,
            conversations=self._conversations,
            settings_store=self._settings,
            offline=self._offline,
            settings=ctx.settings,
            capabilities=ctx.capabilities,
            categories=ctx.tool_categories,
            dormant=DormantCategory.summaries(ctx.dormant_categories),
            instruction_providers=ctx.instruction_providers,
            availability=ctx.category_availability,
            projects=ctx.services.get_optional(ProjectStore),
            worktrees=ctx.services.get_optional(WorktreeManager),
        )
        register_recompose(self._runs, WAKE_KIND, self._recompose)

    async def _recompose(self, facts: ParkedFacts) -> Recomposed:
        records = self._ctx.services.get_optional(SubagentStore)
        row = (
            await records.by_child(facts.conversation_id)
            if records is not None and facts.conversation_id is not None
            else None
        )
        return await self._composer.recompose(
            facts, kind=WAKE_KIND, depth=row.depth if row is not None else 0
        )

    @property
    def pending(self) -> bool:
        return bool(self._kicks)

    async def drain(self) -> None:
        """Wait for scheduled dispatches; for shutdown and for tests."""
        while self._kicks:
            _ = await asyncio.gather(*self._kicks, return_exceptions=True)

    async def settle(self, conversation_id: str) -> None:
        """Return once nothing is about to start a turn on this thread: the run-end hooks
        already fired (each may dispatch), scheduled dispatches, and one in progress.
        Bounded, so a stuck hook cannot hold a client's request open."""
        pending = {*app_state(self._app).run_terminal_tasks, *self._kicks}
        if pending:
            _ = await asyncio.wait(pending, timeout=SETTLE_TIMEOUT_S)
        with suppress(TimeoutError):
            async with asyncio.timeout(SETTLE_TIMEOUT_S), self._lock_for(conversation_id):
                pass

    @property
    def _app(self) -> FastAPI:
        app = self._ctx.app
        if app is None:
            raise RuntimeError("the inbox dispatcher needs the app to start an operator turn")
        return app

    # -- the tree's budget --------------------------------------------------------------

    def remember_root(self, conversation_id: str, root: str) -> None:
        """Note which tree a spawned agent's thread belongs to."""
        if not root:
            return
        self._roots[conversation_id] = root
        self._roots.move_to_end(conversation_id)
        while len(self._roots) > _ROOTS_CACHED:
            _ = self._roots.popitem(last=False)

    def note_ledger_prompt(self, message_id: str, prompt: str | None, thread: str | None) -> None:
        """Remember which turn's prompt a message's sender was running under, and the
        thread whose ledger that prompt belongs to."""
        if prompt is None or thread is None:
            return
        self._message_ledger[message_id] = (thread, prompt)
        while len(self._message_ledger) > _LEDGER_NOTES_CACHED:
            _ = self._message_ledger.popitem(last=False)

    def _wake_prompt(self, message_id: str, row: SubagentRecord) -> str | None:
        """The prompt a wake of this agent files its changes under: the sender's, when the
        sender files in the ledger this agent does, else the one stored on its row."""
        owner, _ = split_workspace_key(row.workspace_key or "")
        noted = self._message_ledger.get(message_id)
        if noted is not None and noted[0] == (owner or row.child_conversation_id):
            return noted[1]
        return row.ledger_prompt_message_id

    async def root_of(self, conversation_id: str) -> str:
        cached = self._roots.get(conversation_id)
        if cached is not None:
            return cached
        records = self._ctx.services.get_optional(SubagentStore)
        row = await records.by_child(conversation_id) if records is not None else None
        root = (row.root_conversation_id or row.parent_conversation_id) if row else ""
        root = root or conversation_id
        self.remember_root(conversation_id, root)
        return root

    def unattended_turns(self, root: str) -> int:
        return self._chain.get(root, 0)

    def spend_budget(self, root: str) -> None:
        """Mark the tree's whole unattended-turn budget as used."""
        self._chain[root] = WAKE_CHAIN_LIMIT

    # -- the run-terminal seam ----------------------------------------------------------

    def observed(self, run: Run) -> None:
        """The sync half: an operator turn clears its tree's budget before anything reacts
        to it ending."""
        if run.kind == "chat" and run.conversation_id:
            root = self._roots.get(run.conversation_id, run.conversation_id)
            _ = self._chain.pop(root, None)

    async def settled(self, run: Run, _watched: bool) -> None:
        """The async half: a run on a thread ended, so whatever waits in its inbox gets the
        next turn, and an operator turn releases the threads its tree left waiting."""
        if run.conversation_id is None:
            return
        try:
            await self.dispatch(run.owner_id, run.conversation_id)
            if run.kind == "chat":
                root = await self.root_of(run.conversation_id)
                for owner_id, conversation_id in self._stalled.pop(root, set()):
                    await self.dispatch(owner_id, conversation_id)
        except Exception:
            logger.exception("inbox: could not dispatch after run %s", run.id)

    # -- dispatching --------------------------------------------------------------------

    def kick(self, owner_id: str, conversation_id: str) -> None:
        """Schedule a dispatch from synchronous code: a claim released with nothing having
        started a run, so nothing else will take what queued behind it."""
        if self._runs.closing or not self._runs.inbox(conversation_id, owner_id).messages:
            return
        task = asyncio.create_task(self.dispatch(owner_id, conversation_id))
        self._kicks.add(task)
        task.add_done_callback(self._kicks.discard)

    async def dispatch(self, owner_id: str, conversation_id: str) -> None:
        """Start the turn that takes this thread's inbox, if nothing is running on it and
        nothing holds it."""
        async with self._lock_for(conversation_id):
            while not self._runs.closing:
                if self._runs.active_run_for(conversation_id, owner_id) is not None:
                    return
                inbox = self._runs.inbox(conversation_id, owner_id)
                if inbox.waiting_for_operator:
                    return
                lead = inbox.lead()
                if lead is None or (lead.source != "operator" and self.stalled(conversation_id)):
                    return
                try:
                    if lead.source == "operator":
                        _ = await submit_queued(self._app, conversation_id, lead)
                    else:
                        await self._wake(owner_id, conversation_id, lead)
                    return
                except ConversationBusyError:
                    return
                except HTTPException as exc:
                    if exc.status_code == 409:
                        return
                    reason = str(exc.detail)
                except Exception as exc:
                    logger.exception("inbox: could not start %s on %s", lead.id, conversation_id)
                    reason = type(exc).__name__
                if lead.source != "operator":
                    return
                await self._hold_failed(owner_id, conversation_id, lead, reason)

    async def _hold_failed(
        self, owner_id: str, conversation_id: str, lead: QueuedMessage, reason: str
    ) -> None:
        """Set aside an operator message that cannot start a turn, so it stops blocking what
        waits behind it. It shows as Held until the operator edits or releases it."""
        _ = self._runs.inbox(conversation_id, owner_id).hold(lead.id, True)
        notifications = self._ctx.capabilities.get_optional(NotificationService)
        if notifications is None:
            return
        try:
            _ = await notifications.notify(
                owner_id,
                "system",
                "Queued message could not start",
                body=(
                    f"Your queued message could not start ({reason}), so it is held. "
                    + "Edit or release it to try again."
                ),
                conversation_id=conversation_id,
            )
        except Exception:
            logger.exception("inbox: could not say %s was set aside", lead.id)

    async def _wake(self, owner_id: str, conversation_id: str, lead: QueuedMessage) -> None:
        """Open an unattended turn on a delivery, within the tree's budget. A request that
        holds the thread's claim wins: it submits or releases, and either way drains the
        inbox, so the delivery stays queued."""
        self._runs.claim(conversation_id, owner_id)
        try:
            await self._open_wake(owner_id, conversation_id, lead)
        finally:
            self._runs.release(conversation_id, owner_id)

    async def _open_wake(self, owner_id: str, conversation_id: str, lead: QueuedMessage) -> None:
        root = await self.root_of(conversation_id)
        spent = self._chain.get(root, 0)
        if spent >= WAKE_CHAIN_LIMIT:
            await self._stall(root, owner_id, conversation_id)
            return
        records = self._ctx.services.get_optional(SubagentStore)
        row = await records.by_child(conversation_id) if records is not None else None
        try:
            thread = await self._composer.resolve(owner_id, conversation_id)
            applied, models = thread.applied, thread.models
        except Exception as exc:
            # Nothing can run the thread (its endpoint or agent is gone). Retrying on every
            # dispatch would leave every delivery behind this one stuck in silence, so the
            # thread waits for the operator, whose turn takes the deliveries with it.
            logger.warning("inbox: cannot open a turn on %s", conversation_id, exc_info=True)
            reason = str(exc.detail) if isinstance(exc, HTTPException) else type(exc).__name__
            await self._stall(root, owner_id, conversation_id, reason=reason)
            return
        # Nobody is in front of a wake turn, so it is not offered the tools that stop and
        # wait for someone.
        disabled = await self._composer.disabled(
            owner_id,
            kind=WAKE_KIND,
            vision=models.vision,
            applied=applied,
            depth=row.depth if row is not None else 0,
        )
        # A spawned agent's turn gets the wall clock its first one had: nobody is watching.
        timeouts: WallClockOverride = {}
        if row is not None:
            timeouts["wall_clock_timeout_s"] = lower_of(
                applied.agent.wall_clock_timeout_s,
                self._ctx.settings.subagent_wall_clock_timeout_s,
            )
        created = compose_turn(
            # The envelope a delivery injected into a running turn arrives in, so the model
            # cannot tell which road it took, nor read it as the operator.
            prompt=injected_text(lead),
            conversation_id=conversation_id,
            models=models,
            capabilities=self._ctx.capabilities,
            categories=self._ctx.tool_categories,
            dormant=DormantCategory.summaries(self._ctx.dormant_categories),
            instruction_providers=thread.instruction_providers,
            prompt_context_providers=self._ctx.prompt_context_providers,
            registry=self._runs,
            store=self._conversations,
            uploads=self._uploads,
            disabled_tools=disabled,
            binding=applied.binding,
            owner_id=owner_id,
            kind=WAKE_KIND,
            context_thresholds=await get_context_thresholds(self._settings, owner_id),
            auto_compact=await resolve_auto_compact_policy(self._settings, owner_id),
            ephemeral=row is not None,
            max_requests=applied.agent.max_requests,
            # Edits belong to the turn that sent the message when it files in the same
            # thread's ledger as this agent, else to the one that launched the agent. A wake
            # that took several messages files under its lead message's prompt.
            ledger_prompt_message_id=(self._wake_prompt(lead.id, row) if row is not None else None),
            **timeouts,
        )
        self._runs.inbox(conversation_id, owner_id).take(lead)
        if row is not None and records is not None:
            await records.reopen(row.id, created.run_id or "")
        self._chain[root] = spent + 1
        self._chain.move_to_end(root)
        while len(self._chain) > 64:
            _ = self._chain.popitem(last=False)

    async def _stall(
        self, root: str, owner_id: str, conversation_id: str, *, reason: str | None = None
    ) -> None:
        """Leave a thread's deliveries waiting for the operator: at the tree's ceiling, or
        with ``reason`` when no turn can be opened on it. The operator is told the first
        time the tree stops so they know it is waiting on them."""
        waiting = self._stalled.setdefault(root, set())
        first = not waiting
        waiting.add((owner_id, conversation_id))
        logger.warning(
            "inbox: deliveries on %s wait for the operator (%s)",
            conversation_id,
            reason or f"the tree under {root} took {WAKE_CHAIN_LIMIT} turns unattended",
        )
        if not first:
            return
        notifications = self._ctx.capabilities.get_optional(NotificationService)
        if notifications is None:
            return
        cause = (
            f"This thread's sub-agents ran {WAKE_CHAIN_LIMIT} turns without you, so their "
            + "reports are held."
            if reason is None
            else f"This thread's sub-agent reports could not start a turn: {reason}."
        )
        try:
            _ = await notifications.notify(
                owner_id,
                "system",
                "Sub-agents paused for you",
                body=f"{cause} Write anything in this thread to release them.",
                conversation_id=root,
            )
        except Exception:
            logger.exception("inbox: could not say the tree under %s paused", root)

    def stalled(self, conversation_id: str) -> bool:
        """Whether this thread's deliveries wait for the operator's next turn: the tree hit
        its unattended-turn ceiling, or no turn could be opened on the thread."""
        return any(
            conversation_id == waiting
            for threads in self._stalled.values()
            for _owner, waiting in threads
        )

    def _lock_for(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        self._locks.move_to_end(conversation_id)
        while len(self._locks) > 64:
            # Only ever drop an idle one: evicting a held lock would let a second dispatch
            # into a thread that one is already mid-way through.
            oldest, candidate = next(iter(self._locks.items()))
            if candidate.locked():
                break
            _ = self._locks.pop(oldest)
        return lock
