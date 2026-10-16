"""Carrying words between the agents of a delegation tree: a finished agent's report up to
the thread that spawned it, and a message from one agent to another.

**Both ride the mid-turn steering road.** A delivery is queued on the recipient's Run when
it has one, exactly as an operator's typed message is, and handed to its *next,
not-yet-sent* model request, so nothing interrupts a model mid-stream. A recipient with
nothing running is woken with a turn of its own carrying the delivery.

**The one thing that road does not cover** is a delivery queued during a run's final
stream, where no further request is ever made. The substrate drops what is still pending
at terminal, so a terminal run is checked for undelivered reports and messages and they are
re-delivered as a turn of their own.

**One lock per recipient thread**, so two deliveries landing together produce one turn
carrying both rather than two racing turns.

**One wake budget per tree.** Agents waking each other need no human, so a tree may take
:data:`WAKE_CHAIN_LIMIT` turns unattended before wakes stop; the operator sending anything
into the tree clears it. A delivery that would have woken a thread past the ceiling is
held, the operator is told once, and everything held goes out when they next send a turn
into the tree, so no report is lost to the rail.

**Every report says how the work ended.** A spawned agent files one with
``subagent_report`` (``tools/report.py``), held here by run until that run ends. An
unattended turn that ends without one is reported as failed, so the parent always hears
which of done, blocked or failed it is dealing with.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass

from agent.summarize import resolve_auto_compact_policy
from core.exceptions import NotFoundError
from core.fork import MergeReport
from harness.manifest import DormantCategory, HarnessContext
from models.subagent import SubagentRecord
from routes.chat import (
    ConversationBusyError,
    WallClockOverride,
    compose_turn,
    resolve_turn_models,
)
from runs import Run, RunRegistry, RunStatus
from runs.events import MessageSource
from runs.lanes import CHAT_TURN_KINDS, is_attended
from services.agents import AgentCatalog, lower_of
from services.agents.thread import DELEGATION_TOOLS, may_delegate, resolve_thread_agent
from services.conversations import ConversationStore
from services.home.paths import Place
from services.home.table import child_worktree_name
from services.notifications import NotificationService
from services.offline import OfflineModeService
from services.projects import ProjectStore, WorktreeManager
from services.projects.repo import WorktreeError, branch_for, child_branch_for
from services.registry import ModelRegistry
from services.sandbox.thread_shells import ThreadShells
from services.settings_store import (
    SettingsStore,
    get_agent_request_limit_override,
    get_context_thresholds,
)
from services.subagent_store import SubagentStore
from services.subagents import FiledReport
from services.subagents.callsigns import designation
from services.subagents.report import message_envelope, report_envelope
from services.tool_policy import effective_disabled_tools
from services.uploads import UploadStore
from services.workspace import split_workspace_key

logger = logging.getLogger(__name__)

#: How many changed files a parked sub-agent's note names before counting the rest.
_FILES_LISTED = 20

#: The kind a wake turn is submitted under — a thread continuing itself (``runs/lanes.py``).
WAKE_KIND = "wake"

#: How many turns a delegation tree may take without the operator saying anything, before
#: wakes stop. A safety rail rather than a setting: every wake is a full turn against a
#: large context, and agents waking each other need no human. At the ceiling deliveries are
#: still recorded; the tree simply stops driving itself.
WAKE_CHAIN_LIMIT = 25

#: How many thread→root mappings are remembered for clearing the budget synchronously.
_ROOTS_CACHED = 512

#: How many filed reports wait for their run to end. One per running sub-agent at most.
_FILED_CACHED = 256

#: What an unattended turn that ended without filing a report is settled with.
NO_REPORT = "it ended without reporting"


@dataclass(frozen=True)
class _Held:
    """A delivery the wake budget kept from waking its thread."""

    owner_id: str
    conversation_id: str
    text: str
    source: MessageSource
    sender: str | None


@dataclass(frozen=True)
class _Outcome:
    status: str
    summary: str | None
    error: str | None


class SubagentWake:
    """Delivers reports and messages between the agents of a delegation tree."""

    def __init__(self, ctx: HarnessContext, records: SubagentStore) -> None:
        self._ctx: HarnessContext = ctx
        self._records: SubagentStore = records
        self._conversations: ConversationStore = ctx.services.get(ConversationStore)
        self._models: ModelRegistry = ctx.services.get(ModelRegistry)
        self._runs: RunRegistry = ctx.services.get(RunRegistry)
        self._settings: SettingsStore = ctx.services.get(SettingsStore)
        self._offline: OfflineModeService = ctx.services.get(OfflineModeService)
        self._uploads: UploadStore = ctx.services.get(UploadStore)
        self._agents: AgentCatalog = ctx.services.get(AgentCatalog)
        # One lock per parent thread, bounded like every other per-conversation cache a
        # long-lived process keeps.
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        # Consecutive unattended turns per tree root, cleared whenever the operator sends
        # anything into the tree. In memory on purpose: a rail against a runaway loop
        # rather than a fact about the conversation.
        self._chain: OrderedDict[str, int] = OrderedDict()
        self._roots: OrderedDict[str, str] = OrderedDict()
        # Reports filed by runs still going, by run id, and deliveries the budget held back,
        # by tree root. In memory like the runs themselves, which a restart ends anyway.
        self._filed: OrderedDict[str, FiledReport] = OrderedDict()
        self._held: dict[str, list[_Held]] = {}

    def file(self, run_id: str, report: FiledReport) -> None:
        """Hold a spawned agent's report until its run ends. A second one replaces it."""
        self._filed[run_id] = report
        self._filed.move_to_end(run_id)
        while len(self._filed) > _FILED_CACHED:
            _ = self._filed.popitem(last=False)

    def remember_root(self, conversation_id: str, root: str) -> None:
        """Note which tree a spawned agent's thread belongs to."""
        if not root:
            return
        self._roots[conversation_id] = root
        self._roots.move_to_end(conversation_id)
        while len(self._roots) > _ROOTS_CACHED:
            _ = self._roots.popitem(last=False)

    async def _root_of(self, conversation_id: str) -> str:
        cached = self._roots.get(conversation_id)
        if cached is not None:
            return cached
        row = await self._records.by_child(conversation_id)
        root = (row.root_conversation_id or row.parent_conversation_id) if row else ""
        root = root or conversation_id
        self.remember_root(conversation_id, root)
        return root

    # -- the run-terminal seam ------------------------------------------------------

    def observed(self, run: Run) -> None:
        """The sync half: note that the operator is driving this tree again, which clears
        its wake budget before anything reacts to the turn ending."""
        if run.kind == "chat" and run.conversation_id:
            root = self._roots.get(run.conversation_id, run.conversation_id)
            _ = self._chain.pop(root, None)

    def unattended_turns(self, root: str) -> int:
        """Consecutive unattended turns this tree has taken."""
        return self._chain.get(root, 0)

    def spend_budget(self, root: str) -> None:
        """Mark the tree's whole unattended-turn budget as used."""
        self._chain[root] = WAKE_CHAIN_LIMIT

    async def settled(self, run: Run, _watched: bool) -> None:
        """The async half: a run reached terminal. If it was a spawned agent's turn, report
        it back; either way, rescue anything still queued for it."""
        try:
            await self._report(run)
            await self._rescue(run)
            if run.kind == "chat" and run.conversation_id:
                await self._release(await self._root_of(run.conversation_id))
        except Exception:
            # Never let this escape: it runs as a background task, and a feature that
            # failed to deliver one report must not take the dispatcher's other hooks or
            # the run's own teardown with it.
            logger.exception("subagents: could not settle run %s", run.id)

    # -- a sub-agent finished -------------------------------------------------------

    async def _report(self, run: Run) -> None:
        """Every turn a spawned agent finishes — its first, one a message woke, or one the
        operator sent after opening it — is reported to the thread that spawned it."""
        if run.conversation_id is None or run.kind not in CHAT_TURN_KINDS:
            return
        row = await self._records.by_child(run.conversation_id)
        if row is None or row.owner_id != run.owner_id:
            return  # an ordinary thread, not one of ours
        outcome = await self._outcome(run, row.child_conversation_id)
        summary = outcome.summary
        parked = await self._park(row)
        try:
            await self._records.settle(
                row.id,
                status=outcome.status,
                summary=None if summary is None else _joined(summary, parked),
                error=outcome.error,
                context_used=(getattr(run.metrics, "context_used", None) if run.metrics else None),
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
            return  # spawned from a run with no thread of its own
        _ = await self.deliver(
            row.owner_id,
            row.parent_conversation_id,
            report_text(
                outcome.status,
                row.handle or row.spec_name,
                row.spec_name,
                summary,
                outcome.error,
                parked,
                callsign=row.callsign,
                agent_class=row.agent_class,
            ),
        )

    async def _outcome(self, run: Run, conversation_id: str) -> _Outcome:
        """How the sub-agent's turn ended, and what it has to say for itself.

        A filed report decides it, unless the agent was stopped. Without one, a sub-agent
        that errored, was stopped, or ended an unattended turn without reporting still did
        work, and its transcript still holds it, so its last answer is handed over beside
        the failure rather than thrown away.
        """
        filed = self._filed.pop(run.id, None)
        if filed is not None and run.status is not RunStatus.cancelled:
            return _Outcome(filed.status, filed.text, None)
        answer = await self._latest_answer(conversation_id)
        if run.status is RunStatus.done:
            if is_attended(run.kind):
                # The operator was talking to it in its own thread, so its answer is the
                # report they meant to send up.
                return _Outcome("done", answer or "(it answered with nothing)", None)
            return _Outcome("failed", answer, NO_REPORT)
        reason = run.error or run.detail or run.status.value
        return _Outcome(status_of(run), answer, f"it did not finish: {reason}")

    async def _latest_answer(self, conversation_id: str) -> str | None:
        try:
            turns = await self._conversations.messages_view(conversation_id)
        except NotFoundError:
            return None
        return next(
            (t.content for t in reversed(turns) if t.role == "assistant" and t.content), None
        )

    async def _park(self, row: SubagentRecord) -> str | None:
        """Commit the worktree a sub-agent was spawned into and say where it is, for its
        parent to review and merge. None for one that worked in its parent's files."""
        holder, delegation = split_workspace_key(row.workspace_key or "")
        worktrees = self._ctx.capabilities.get_optional(WorktreeManager)
        projects = self._ctx.capabilities.get_optional(ProjectStore)
        if (
            delegation is None
            or delegation != row.delegation_id
            or not row.project_id
            or worktrees is None
            or projects is None
        ):
            return None
        # What it left running writes into the worktree being committed, so it stops first.
        shells = self._ctx.capabilities.get_optional(ThreadShells)
        if shells is not None:
            await shells.close(row.workspace_key or "")
        try:
            changed = await worktrees.child_changes(
                project_id=row.project_id,
                conversation_id=holder,
                delegation_id=delegation,
                base_ref=row.base_ref or branch_for(holder),
            )
        except WorktreeError:
            return None
        handle = row.handle or row.spec_name
        slug = (await projects.slugs(row.owner_id)).get(row.project_id, "")
        path = Place("worktrees", child_worktree_name(slug, handle)).path
        return parked_note(handle, path, child_branch_for(holder, delegation), changed)

    # -- delivering it --------------------------------------------------------------

    async def deliver(
        self,
        owner_id: str,
        conversation_id: str,
        text: str,
        *,
        source: MessageSource = "subagent",
        sender: str | None = None,
    ) -> bool:
        """Put a report (``source="subagent"``) or another agent's message
        (``source="agent"``, from ``sender``) in front of a thread. False when the
        recipient is an agent that was stopped, which nothing wakes."""
        async with self._lock_for(conversation_id):
            run = self._runs.active_run_for(conversation_id, owner_id)
            if run is not None and not run.is_terminal:
                # Running, or parked on an approval: handed over at its next request.
                _ = run.enqueue_message(text, source=source, sender=sender)
                return True
            row = await self._records.by_child(conversation_id)
            if row is not None and row.status == "cancelled":
                return False
            await self._wake(owner_id, conversation_id, text, source, sender, row)
            return True

    async def _wake(
        self,
        owner_id: str,
        conversation_id: str,
        text: str,
        source: MessageSource,
        sender: str | None,
        row: SubagentRecord | None,
    ) -> None:
        """Start a turn for a thread that has nothing running, carrying the delivery."""
        root = await self._root_of(conversation_id)
        spent = self._chain.get(root, 0)
        if spent >= WAKE_CHAIN_LIMIT:
            await self._hold(root, _Held(owner_id, conversation_id, text, source, sender))
            return
        try:
            applied = await resolve_thread_agent(
                self._agents,
                owner_id,
                await self._conversations.binding(conversation_id),
                conversation_id,
                projects=self._ctx.services.get_optional(ProjectStore),
                worktrees=self._ctx.services.get_optional(WorktreeManager),
            )
            models = await resolve_turn_models(
                self._models, applied.agent.endpoint_id, applied.agent.model, owner_id=owner_id
            )
        except Exception:
            # The thread is gone, or nothing can run it. What was sent survives in the
            # sender's transcript; there is simply nobody to hand it to.
            logger.warning("subagents: cannot wake %s", conversation_id, exc_info=True)
            return
        disabled = await effective_disabled_tools(
            self._settings,
            self._offline,
            owner_id,
            vision=models[4],
            # Nobody is in front of a wake turn, so it is not offered the tools that stop
            # and wait for someone.
            kind=WAKE_KIND,
            availability=self._ctx.category_availability,
            caps=self._ctx.capabilities,
        )
        disabled |= applied.withheld
        depth = row.depth if row is not None else 0
        if not may_delegate(applied.agent, depth, self._ctx.settings.agent_max_depth):
            disabled |= DELEGATION_TOOLS
        # A spawned agent's turn gets the wall clock its first one had: nobody is watching.
        timeouts: WallClockOverride = {}
        if row is not None:
            timeouts["wall_clock_timeout_s"] = lower_of(
                applied.agent.wall_clock_timeout_s,
                self._ctx.settings.subagent_wall_clock_timeout_s,
            )
        try:
            created = compose_turn(
                # The envelope a delivery queued into a running turn arrives in, so the
                # model cannot tell which road it took, nor read it as the operator.
                prompt=_framed(text, source, sender),
                conversation_id=conversation_id,
                models=models,
                capabilities=self._ctx.capabilities,
                categories=self._ctx.tool_categories,
                dormant=DormantCategory.summaries(self._ctx.dormant_categories),
                instruction_providers=(*self._ctx.instruction_providers, *applied.instructions),
                prompt_context_providers=self._ctx.prompt_context_providers,
                registry=self._runs,
                store=self._conversations,
                uploads=self._uploads,
                disabled_tools=disabled,
                binding=applied.binding,
                owner_id=owner_id,
                kind=WAKE_KIND,
                request_limit=lower_of(
                    applied.agent.request_limit,
                    await get_agent_request_limit_override(self._settings, owner_id),
                ),
                context_thresholds=await get_context_thresholds(self._settings, owner_id),
                auto_compact=await resolve_auto_compact_policy(self._settings, owner_id),
                ephemeral=row is not None,
                **timeouts,
            )
        except ConversationBusyError:
            # A turn started between the check above and here; its inbox is the right
            # place after all.
            run = self._runs.active_run_for(conversation_id, owner_id)
            if run is not None and not run.is_terminal:
                _ = run.enqueue_message(text, source=source, sender=sender)
                return
            logger.warning("subagents: lost the race to wake %s", conversation_id)
            return
        if row is not None:
            await self._records.reopen(row.id, created.run_id)
        self._chain[root] = spent + 1
        self._chain.move_to_end(root)
        while len(self._chain) > 64:
            _ = self._chain.popitem(last=False)

    # -- the budget's held deliveries ------------------------------------------------

    async def _hold(self, root: str, held: _Held) -> None:
        """Keep a delivery the budget refused, telling the operator the first time a tree
        stops so they know it is waiting on them."""
        waiting = self._held.setdefault(root, [])
        waiting.append(held)
        logger.warning(
            "subagents: the tree under %s took %s turns unattended; holding a delivery to %s",
            root,
            WAKE_CHAIN_LIMIT,
            held.conversation_id,
        )
        if len(waiting) > 1:
            return
        notifications = self._ctx.capabilities.get_optional(NotificationService)
        if notifications is None:
            return
        try:
            _ = await notifications.notify(
                held.owner_id,
                "system",
                "Agents paused for you",
                body=(
                    f"The agents of this thread took {WAKE_CHAIN_LIMIT} turns without you. "
                    + "Their reports are held; send anything in this thread to release them."
                ),
                conversation_id=root,
            )
        except Exception:
            logger.exception("subagents: could not say the tree under %s paused", root)

    async def _release(self, root: str) -> None:
        """Deliver what the budget held for this tree, now the operator has cleared it."""
        held = self._held.pop(root, [])
        reports: dict[str, list[_Held]] = {}
        for each in held:
            if each.source == "subagent":
                reports.setdefault(each.conversation_id, []).append(each)
            else:
                _ = await self.deliver(
                    each.owner_id,
                    each.conversation_id,
                    each.text,
                    source=each.source,
                    sender=each.sender,
                )
        # Reports bound for one thread go together, so they land as one turn.
        for conversation_id, batch in reports.items():
            _ = await self.deliver(
                batch[0].owner_id, conversation_id, "\n\n".join(each.text for each in batch)
            )

    # -- the delivery that nearly got dropped ---------------------------------------

    async def _rescue(self, run: Run) -> None:
        """Re-deliver reports and messages the substrate is about to drop: ones queued
        during the run's final stream, which no further request will carry.

        After :meth:`_report`, so a sibling finishing in the same instant has already had
        its say and both land in one turn.
        """
        if run.conversation_id is None:
            return
        stranded = [m for m in run.pending_messages if m.source in {"subagent", "agent"}]
        if not stranded:
            return
        run.pending_messages = [
            m for m in run.pending_messages if m.source not in {"subagent", "agent"}
        ]
        reports = [m.text for m in stranded if m.source == "subagent"]
        if reports:
            _ = await self.deliver(run.owner_id, run.conversation_id, "\n\n".join(reports))
        for message in stranded:
            if message.source == "agent":
                _ = await self.deliver(
                    run.owner_id,
                    run.conversation_id,
                    message.text,
                    source="agent",
                    sender=message.sender,
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
            _ = self._locks.pop(oldest)
        return lock


def _framed(text: str, source: MessageSource, sender: str | None) -> str:
    if source == "agent":
        return message_envelope(sender or "an agent", text)
    return report_envelope(text)


def status_of(run: Run) -> str:
    if run.status is RunStatus.done:
        return "done"
    return "cancelled" if run.status is RunStatus.cancelled else "failed"


def _joined(summary: str, parked: str | None) -> str:
    return summary if parked is None else f"{summary}\n\n{parked}"


def parked_note(handle: str, path: str, branch: str, changed: MergeReport) -> str:
    """Where a sub-agent in a worktree of its own left its work, and what to do with it."""
    files = [*changed.files, *(f"{name} (deleted)" for name in changed.deleted)]
    listed = ", ".join(files[:_FILES_LISTED]) if files else "nothing"
    if len(files) > _FILES_LISTED:
        listed += f", and {len(files) - _FILES_LISTED} more"
    return (
        f"Its work is on branch `{branch}` in {path}, not merged. Changed: {listed}.\n"
        + f"  help: review it there and fix what needs fixing, then agents_merge `{handle}` "
        + f"to land it where you work, or agents_discard `{handle}` to drop it"
    )


def _recovery(status: str, handle: str) -> list[str]:
    """What the parent can do next, for a report that did not end in finished work."""
    if status == "blocked":
        return [
            f"answer it with `agents_message` to `{handle}`; it picks up where it stopped",
            f"or `agents_stop` `{handle}` and take the work back",
        ]
    if status == "failed":
        return [
            "spawn a fresh agent with a narrower brief that avoids what broke",
            "or take the work back and do it yourself",
        ]
    return []


def report_text(
    status: str,
    handle: str,
    spec_name: str,
    summary: str | None,
    error: str | None,
    parked: str | None,
    *,
    callsign: str = "",
    agent_class: str = "",
) -> str:
    """A sub-agent's hand-back, as the launching model reads it: who, how it ended, what
    went wrong, its report, where its work is, and what to do next.

    Named by its **handle**, because a thread with three sub-agents out gets three of these
    and the spec alone does not tell them apart. Its callsign and class follow, since the
    operator may refer to it by either; the spec rides along only when it differs from the
    handle, since ``explorer (explorer)`` is noise.
    """
    extras = [name for name in (designation(callsign, agent_class),) if name]
    if spec_name != handle:
        extras.append(spec_name)
    named = f"`{handle}`" + (f" ({', '.join(extras)})" if extras else "")
    parts = [f"{named}: {status}."]
    if error:
        parts.append(f"error: {error}")
    if summary:
        parts.append(summary)
    if parked:
        parts.append(parked)
    hints = _recovery(status, handle)
    if hints:
        parts.append("\n".join(f"  help: {hint}" for hint in hints))
    return "\n\n".join(parts)
