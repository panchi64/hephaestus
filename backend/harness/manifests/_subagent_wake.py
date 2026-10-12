"""What happens when a sub-agent finishes: its report reaches the thread that launched it.

This is the half that makes sub-agents asynchronous rather than merely non-blocking. The
launching turn does not wait — it starts sub-agents, carries on, and ends. Something has to
carry each report back afterwards and get the model working again, and that something is
here.

**It rides the mid-turn steering road, and that is the whole design.** A report is queued on
the parent's Run exactly as an operator's typed message is, and ``agent/turn.py`` hands a
queued message to the *next, not-yet-sent* model request — on a node ``agent.iter()`` yields
before it streams. So a sub-agent finishing while the parent is mid-answer cannot interrupt
it: the tokens finish, and the report lands at the following boundary. There is deliberately
no second delivery route, because a second route is how that guarantee gets lost.

**The one thing that road does not cover** is a report queued during the parent's *final*
stream, where no further request is ever made. The substrate drops what is still pending at
terminal, which is right for the operator (their client still holds their own text and
rebuilds the bubble from the replay) and silently lossy for a report, which nothing else
holds. So a terminal run is checked for undelivered reports and they are re-delivered as a
turn of their own.

**One lock per parent thread.** Two sub-agents finishing in the same instant, and a report
being re-delivered while a third lands, must not race into two turns that each replay a
history the other has moved past. Serialising per parent is what makes "several at once" the
ordinary case rather than the interesting one.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from agent.summarize import resolve_auto_compact_policy
from core.exceptions import NotFoundError
from harness.manifest import DormantCategory, HarnessContext
from routes.chat import ConversationBusyError, compose_turn, resolve_turn_models
from runs import Run, RunRegistry, RunStatus
from services.conversations import ConversationStore
from services.offline import OfflineModeService
from services.registry import ModelRegistry
from services.settings_store import (
    SettingsStore,
    get_agent_request_limit_override,
    get_context_thresholds,
)
from services.subagent_store import SubagentStore
from services.subagents.report import report_envelope
from services.tool_policy import effective_disabled_tools
from services.uploads import UploadStore
from services.workspace import DELEGATION_SEP
from services.workspace_fork import ForkTarget, merge_summary, reopen_fork

logger = logging.getLogger(__name__)

#: The kind a wake turn is submitted under — a thread continuing itself (``runs/lanes.py``).
WAKE_KIND = "wake"

#: How many turns a thread may take without the operator saying anything, before the wake
#: stops submitting new ones.
#:
#: A safety rail, not a preference, which is why it is a constant rather than a setting: a
#: sub-agent can wake the parent, and the woken parent can launch another sub-agent, and
#: nothing in that loop needs a human. Every wake is a full turn against a large context, so
#: the loop is the difference between a useful night and an expensive one. At the ceiling
#: the report is still delivered and still persisted — the thread simply stops driving
#: itself, and the operator's next message clears the count.
WAKE_CHAIN_LIMIT = 25


class SubagentWake:
    """Carries a finished sub-agent's report back to the thread that launched it."""

    def __init__(self, ctx: HarnessContext, records: SubagentStore) -> None:
        self._ctx = ctx
        self._records = records
        self._conversations = ctx.services.get(ConversationStore)
        self._models = ctx.services.get(ModelRegistry)
        self._runs = ctx.services.get(RunRegistry)
        self._settings = ctx.services.get(SettingsStore)
        self._offline = ctx.services.get(OfflineModeService)
        self._uploads = ctx.services.get(UploadStore)
        # One lock per parent thread, bounded like every other per-conversation cache a
        # long-lived process keeps.
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        # Consecutive turns this thread has driven itself, cleared whenever the operator
        # sends anything. In memory on purpose: a restart is a fresh start, and the count
        # is a rail against a runaway loop rather than a fact about the conversation.
        self._chain: OrderedDict[str, int] = OrderedDict()

    # -- the run-terminal seam ------------------------------------------------------

    def observed(self, run: Run) -> None:
        """The sync half: note that the operator is driving this thread again.

        Their own turn is the thing that clears the wake chain, and it has to be noticed
        here rather than at the wake itself — by the time a report arrives, the turn that
        proves a human is present may be long over.
        """
        if run.kind == "chat" and run.conversation_id:
            self._chain.pop(run.conversation_id, None)

    async def settled(self, run: Run, _watched: bool) -> None:
        """The async half: a run reached terminal. If it was a sub-agent, report it back;
        if it was a parent, rescue anything still queued for it."""
        try:
            await self._report(run)
            await self._rescue(run)
        except Exception:
            # Never let this escape: it runs as a background task, and a feature that
            # failed to deliver one report must not take the dispatcher's other hooks or
            # the run's own teardown with it.
            logger.exception("subagents: could not settle run %s", run.id)

    # -- a sub-agent finished -------------------------------------------------------

    async def _report(self, run: Run) -> None:
        row = await self._records.by_run(run.id)
        if row is None:
            return  # an ordinary run, not one of ours
        summary, error = await self._outcome(run, row.child_conversation_id)
        merged = await self._merge(row)
        try:
            await self._records.settle(
                row.id,
                status=_status_of(run),
                summary=None if summary is None else _joined(summary, merged),
                error=error,
                context_used=(
                    getattr(run.metrics, "context_used", None) if run.metrics else None
                ),
                context_window=run.context_window,
            )
        except Exception:
            # Guarded on its own, and this is the whole reason it is: closing the row is
            # bookkeeping, and delivering the report is the work. A write that failed —
            # two turns and the write-behind drainers can hold SQLite's single writer lock
            # past its retries — would otherwise cost the parent a report nothing else
            # holds, on top of a card that reads "working" forever. The card is wrong
            # until the next restart reconciles it; the thread still hears what happened.
            logger.exception("subagents: could not close out %s", row.id)
        if not row.parent_conversation_id:
            return  # launched from a thread that no longer exists, or from none
        await self.deliver(
            row.owner_id,
            row.parent_conversation_id,
            _report_text(row.handle or row.spec_name, row.spec_name, summary, error, merged),
        )

    async def _outcome(self, run: Run, conversation_id: str) -> tuple[str | None, str | None]:
        """What the sub-agent has to say for itself: its report, or why there isn't one.

        A sub-agent that errored or was stopped still did work, and its transcript still
        holds it — so its last answer is handed over beside the failure rather than
        thrown away. The alternative tells the parent nothing it can act on.
        """
        answer = await self._latest_answer(conversation_id)
        if run.status is RunStatus.done:
            return answer or "(it finished without reporting anything)", None
        reason = run.error or run.detail or run.status.value
        return answer, f"The sub-agent did not finish: {reason}"

    async def _latest_answer(self, conversation_id: str) -> str | None:
        try:
            turns = await self._conversations.messages_view(conversation_id)
        except NotFoundError:
            return None
        return next(
            (t.content for t in reversed(turns) if t.role == "assistant" and t.content), None
        )

    async def _merge(self, row) -> str | None:
        """Land an isolated sub-agent's work back where it was forked from.

        Only for one that worked apart: a sharing sub-agent's changes are already in the
        workspace, with nothing to merge and nothing that could conflict.
        """
        if row.workspace_policy != "isolated" or not row.workspace_key:
            return None
        parent_key, _, _ = row.workspace_key.partition(DELEGATION_SEP)
        fork = reopen_fork(
            self._ctx.capabilities,
            kind=row.workspace_kind or "sandbox",
            target=ForkTarget(
                parent_key=parent_key,
                child_key=row.workspace_key,
                delegation_id=row.delegation_id or "",
                owner_id=row.owner_id,
                project_id=row.project_id,
                # The *parent's* conversation, which is what a child checkout is keyed on.
                conversation_id=parent_key,
            ),
        )
        if fork is None:
            return None
        try:
            return merge_summary(await fork.merge())
        except Exception as exc:  # noqa: BLE001 — a failed merge is news, not a crash
            logger.warning("subagents: merging %s failed", row.id, exc_info=True)
            return f"Its work could not be merged back: {exc}"
        finally:
            try:
                await asyncio.shield(fork.discard())
            except Exception:  # noqa: BLE001 — a leftover copy is untidy, never fatal
                logger.warning("subagents: could not discard %s", row.workspace_key)

    # -- delivering it --------------------------------------------------------------

    async def deliver(self, owner_id: str, conversation_id: str, report: str) -> None:
        """Put one report in front of the thread that launched the sub-agent.

        Serialised per thread, because the two branches below read and then act on the
        same piece of state — whether a run is going — and two reports arriving together
        must produce one turn carrying both rather than two racing turns.
        """
        async with self._lock_for(conversation_id):
            run = self._runs.active_run_for(conversation_id, owner_id)
            if run is not None and not run.is_terminal:
                # Still going — running, or parked on an approval, which is not terminal.
                # The report waits in the inbox and is handed over at the next model
                # request, so a model mid-answer streams to its end untouched.
                run.enqueue_message(report, source="subagent")
                return
            await self._wake(owner_id, conversation_id, report)

    async def _wake(self, owner_id: str, conversation_id: str, report: str) -> None:
        """Start a turn for a thread that has nothing running, carrying the report."""
        spent = self._chain.get(conversation_id, 0)
        if spent >= WAKE_CHAIN_LIMIT:
            # The report is already recorded against the sub-agent, and its transcript is
            # intact — the thread just stops driving itself. The operator's next message
            # clears the count and the model can pick it up from there.
            logger.warning(
                "subagents: %s has taken %s turns unattended; not waking it again",
                conversation_id,
                spent,
            )
            return
        try:
            binding = await self._conversations.binding(conversation_id)
            models = await resolve_turn_models(self._models, None, None, owner_id=owner_id)
        except Exception:
            # The thread is gone, or nothing can run it. The sub-agent's work survives in
            # its own transcript either way; there is simply nobody to hand it to.
            logger.warning(
                "subagents: cannot wake %s with a report", conversation_id, exc_info=True
            )
            return
        disabled = await effective_disabled_tools(
            self._settings,
            self._offline,
            owner_id,
            vision=models[4],
            # Nobody is in front of a wake turn, so it is not offered the tools that stop
            # and wait for someone — the same footing every unattended turn runs on.
            kind=WAKE_KIND,
            availability=self._ctx.category_availability,
            caps=self._ctx.capabilities,
        )
        try:
            compose_turn(
                # The same envelope a report queued into a *running* turn arrives in. The
                # two roads are the same event, and a model should not be able to tell
                # which one a report took — nor read one of them as the operator talking.
                prompt=report_envelope(report),
                conversation_id=conversation_id,
                models=models,
                capabilities=self._ctx.capabilities,
                categories=self._ctx.tool_categories,
                dormant=DormantCategory.summaries(self._ctx.dormant_categories),
                instruction_providers=self._ctx.instruction_providers,
                prompt_context_providers=self._ctx.prompt_context_providers,
                registry=self._runs,
                store=self._conversations,
                uploads=self._uploads,
                disabled_tools=disabled,
                binding=binding,
                owner_id=owner_id,
                kind=WAKE_KIND,
                request_limit=await get_agent_request_limit_override(self._settings, owner_id),
                context_thresholds=await get_context_thresholds(self._settings, owner_id),
                auto_compact=await resolve_auto_compact_policy(self._settings, owner_id),
            )
        except ConversationBusyError:
            # A turn started between the check above and here. The inbox is the right
            # place for the report after all, and enqueueing is what that turn will drain.
            run = self._runs.active_run_for(conversation_id, owner_id)
            if run is not None and not run.is_terminal:
                run.enqueue_message(report, source="subagent")
                return
            logger.warning("subagents: lost the race to wake %s", conversation_id)
            return
        self._chain[conversation_id] = spent + 1
        self._chain.move_to_end(conversation_id)
        while len(self._chain) > 64:
            self._chain.popitem(last=False)

    # -- the report that nearly got dropped -----------------------------------------

    async def _rescue(self, run: Run) -> None:
        """Re-deliver reports the substrate is about to drop.

        A message still queued when a run reaches terminal is discarded, and for the
        operator that is correct: their client holds their own text and rebuilds the
        pending bubble from the replay. A report has no such second copy. This is the case
        where one arrived during the parent's *final* stream — the turn ended, so the next
        model request that would have carried it never happens.

        Deliberately after :meth:`_report`, so a sibling finishing in the same instant has
        already had its say and both reports go into one turn rather than two.
        """
        if run.conversation_id is None:
            return
        stranded = [m for m in run.pending_messages if m.source == "subagent"]
        if not stranded:
            return
        run.pending_messages = [m for m in run.pending_messages if m.source != "subagent"]
        await self.deliver(
            run.owner_id, run.conversation_id, "\n\n".join(m.text for m in stranded)
        )

    def _lock_for(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        self._locks.move_to_end(conversation_id)
        while len(self._locks) > 64:
            # Only ever drop an idle one: evicting a held lock would let a second report
            # into a thread that one is already mid-delivery on.
            oldest, candidate = next(iter(self._locks.items()))
            if candidate.locked():
                break
            self._locks.pop(oldest)
        return lock


def _status_of(run: Run) -> str:
    if run.status is RunStatus.done:
        return "done"
    return "cancelled" if run.status is RunStatus.cancelled else "failed"


def _joined(summary: str, merged: str | None) -> str:
    return summary if merged is None else f"{summary}\n\n{merged}"


def _report_text(
    handle: str,
    spec_name: str,
    summary: str | None,
    error: str | None,
    merged: str | None,
) -> str:
    """A sub-agent's hand-back, as the launching model reads it.

    Named by its **handle**, because a thread with three sub-agents out gets three of these
    and the spec alone does not tell them apart: "Sub-agent `explorer` finished" three times
    over says nothing about which piece of work just came back, and the report that follows
    is the only thing left to guess from.

    The spec rides along in parentheses so the model can still see what kind of worker this
    was — but only when it differs from the handle, since ``explorer (explorer)`` is noise
    in the first line of every report a sub-agent launched without a handle of its own ever
    sends, which is every row the migration backfilled.
    """
    named = f"Sub-agent `{handle}`"
    if spec_name != handle:
        named += f" ({spec_name})"
    parts = [f"{named} finished."]
    if error:
        parts.append(error)
    if summary:
        parts.append(summary)
    if merged:
        parts.append(merged)
    return "\n\n".join(parts)


