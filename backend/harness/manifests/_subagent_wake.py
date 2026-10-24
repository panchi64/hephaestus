"""Carrying words between the agents of a delegation tree: a finished agent's report up to
the thread that spawned it, and a message from one agent to another.

Both go into the recipient's inbox (``runs/inbox.py``), as the operator's mid-turn messages
do. A running recipient takes them at its next model request; an idle one is started by
the inbox dispatcher (``_inbox_dispatch.py``), which keeps the tree's unattended-turn budget.

Every report says how the work ended. A spawned agent files one with ``subagent_report``
(``tools/report.py``), held here by run until it ends. An unattended turn ending without
one is reported failed, so the parent always hears done, blocked or failed.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

from pydantic_ai import ModelResponse

from core.exceptions import NotFoundError
from core.fork import MergeReport
from core.text import elide_middle
from harness.manifest import HarnessContext
from harness.manifests._inbox_dispatch import WAKE_CHAIN_LIMIT, WAKE_KIND, InboxDispatcher
from models.subagent import SubagentRecord
from runs import Run, RunRegistry, RunStatus
from runs.events import MessageSource
from runs.lanes import CHAT_TURN_KINDS, is_attended
from runs.run import StopCause
from services.conversations import ConversationStore
from services.home.paths import Place
from services.home.table import child_worktree_name
from services.projects import ProjectStore, WorktreeManager
from services.projects.repo import WorktreeError, branch_for, child_branch_for
from services.sandbox.thread_shells import ThreadShells
from services.subagent_store import SubagentStore
from services.subagents import FiledReport
from services.subagents.callsigns import designation
from services.subagents.report import report_heading
from services.workspace import split_workspace_key

logger = logging.getLogger(__name__)

__all__ = [
    "NO_REPORT",
    "OPERATOR_STOPPED",
    "RESPAWN_HINT",
    "WAKE_CHAIN_LIMIT",
    "WAKE_KIND",
    "SubagentWake",
    "report_text",
    "stopped_phrase",
]

#: How many stop causes are remembered, by agent thread, for a later refusal to name.
_STOPS_CACHED = 256

#: What a parent is told about an agent the operator stopped.
OPERATOR_STOPPED = (
    "The operator stopped this agent. Don't relaunch it or redo its task unless the operator asks."
)

#: What a parent is told about an agent it stopped itself.
RESPAWN_HINT = "Spawn a new agent if the work still needs doing."

#: How many changed files a parked sub-agent's note names before counting the rest.
_FILES_LISTED = 20

#: How many filed reports wait for their run to end. One per running sub-agent at most.
_FILED_CACHED = 256

#: What an unattended turn that ended without filing a report is settled with.
NO_REPORT = "it ended without reporting"


@dataclass(frozen=True)
class _Outcome:
    status: str
    summary: str | None
    error: str | None
    cause: StopCause | None = None


class SubagentWake:
    """Delivers reports and messages between the agents of a delegation tree."""

    def __init__(
        self, ctx: HarnessContext, records: SubagentStore, dispatcher: InboxDispatcher
    ) -> None:
        self._ctx: HarnessContext = ctx
        self._records: SubagentStore = records
        self._dispatcher: InboxDispatcher = dispatcher
        self._conversations: ConversationStore = ctx.services.get(ConversationStore)
        self._runs: RunRegistry = ctx.services.get(RunRegistry)
        # Reports filed by runs still going, by run id. In memory like the runs themselves,
        # which a restart ends anyway.
        self._filed: OrderedDict[str, FiledReport] = OrderedDict()
        self._stops: OrderedDict[str, StopCause] = OrderedDict()

    def note_stop(self, conversation_id: str, cause: StopCause) -> None:
        """Remember who stopped the agent in this thread, for a refusal to name."""
        self._stops[conversation_id] = cause
        self._stops.move_to_end(conversation_id)
        while len(self._stops) > _STOPS_CACHED:
            _ = self._stops.popitem(last=False)

    def stop_cause(self, conversation_id: str) -> StopCause | None:
        """Who stopped the agent in this thread, when this process saw it happen."""
        return self._stops.get(conversation_id)

    def file(self, run_id: str, report: FiledReport) -> None:
        """Hold a spawned agent's report until its run ends. A second one replaces it."""
        self._filed[run_id] = report
        self._filed.move_to_end(run_id)
        while len(self._filed) > _FILED_CACHED:
            _ = self._filed.popitem(last=False)

    def remember_root(self, conversation_id: str, root: str) -> None:
        """Note which tree a spawned agent's thread belongs to."""
        self._dispatcher.remember_root(conversation_id, root)

    def unattended_turns(self, root: str) -> int:
        """Consecutive unattended turns this tree has taken."""
        return self._dispatcher.unattended_turns(root)

    def spend_budget(self, root: str) -> None:
        """Mark the tree's whole unattended-turn budget as used."""
        self._dispatcher.spend_budget(root)

    # -- the run-terminal seam ------------------------------------------------------

    async def settled(self, run: Run, _watched: bool) -> None:
        """A run reached terminal. If it was a spawned agent's turn, report it back."""
        try:
            await self._report(run)
        except Exception:
            # A background task: one undelivered report must not take the dispatcher's other
            # hooks or the run's teardown with it.
            logger.exception("subagents: could not settle run %s", run.id)

    # -- a sub-agent finished -------------------------------------------------------

    async def _report(self, run: Run) -> None:
        """Report every turn a spawned agent finishes (first, message-woken, or sent by the
        operator after opening it) to the thread that spawned it."""
        if run.conversation_id is None or run.kind not in CHAT_TURN_KINDS:
            return
        row = await self._records.by_child(run.conversation_id)
        if row is None or row.owner_id != run.owner_id:
            return  # an ordinary thread, not one of ours
        outcome = await self._outcome(run, row.child_conversation_id)
        summary = outcome.summary
        if outcome.status == "cancelled" and outcome.cause is not None:
            self.note_stop(row.child_conversation_id, outcome.cause)
        parked = await self._park(row)
        try:
            await self._records.settle(
                row.id,
                status=_stored_status(outcome),
                summary=None if summary is None else _joined(summary, parked),
                error=outcome.error,
                context_used=(getattr(run.metrics, "context_used", None) if run.metrics else None),
                context_window=run.context_window,
            )
        except Exception:
            # Guarded alone: closing the row is bookkeeping, delivery is the work. A failed
            # write (concurrent turns and drainers can hold SQLite's writer lock past its
            # retries) would otherwise cost the parent a report nothing else holds. The card
            # stays wrong until a restart reconciles it; the thread still hears.
            logger.exception("subagents: could not close out %s", row.id)
        if not row.parent_conversation_id:
            return  # spawned from a run with no thread of its own
        handle = row.handle or row.spec_name
        _ = await self.deliver(
            row.owner_id,
            row.parent_conversation_id,
            report_text(
                outcome.status,
                handle,
                row.spec_name,
                summary,
                outcome.error,
                parked,
                callsign=row.callsign,
                agent_class=row.agent_class,
                cause=outcome.cause,
                preview_chars=self._ctx.settings.tool_output_spill_chars,
            ),
            sender=handle,
        )

    async def _outcome(self, run: Run, conversation_id: str) -> _Outcome:
        """How the sub-agent's turn ended, and what it has to say.

        A filed report decides it, unless the agent was stopped. Without one, an agent that
        errored, was stopped or ended unreported still did work, so its last answer is handed
        over beside the failure.
        """
        filed = self._filed.pop(run.id, None)
        if filed is not None and run.status is not RunStatus.cancelled:
            return _Outcome(filed.status, filed.text, None)
        answer = await self._latest_answer(conversation_id)
        if run.status is RunStatus.done:
            if is_attended(run.kind):
                # The operator talked to it in its own thread; its answer is the report.
                return _Outcome("done", answer or "(it answered with nothing)", None)
            return _Outcome("failed", answer, NO_REPORT)
        reason = run.error or run.detail or run.status.value
        return _Outcome(
            status_of(run), answer, f"it did not finish: {reason}", cause=run.stop_cause
        )

    async def _latest_answer(self, conversation_id: str) -> str | None:
        """The newest response that said anything. One answering a retry is a second
        answer, not more of the first, so the rendered turn (which joins them) is not it."""
        try:
            messages = await self._conversations.history(conversation_id)
        except NotFoundError:
            return None
        return next(
            (m.text for m in reversed(messages) if isinstance(m, ModelResponse) and m.text), None
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
        ledger_prompt_message_id: str | None = None,
        ledger_thread: str | None = None,
    ) -> bool:
        """Put a report (``source="subagent"``) or another agent's message
        (``source="agent"``) from the agent ``sender`` names into a thread's inbox. False
        when the recipient is an agent that was stopped, which nothing wakes.

        ``ledger_prompt_message_id`` is the prompt of the sender's running turn in
        ``ledger_thread``'s ledger, which a wake this message causes files its changes under
        when the recipient files in that same thread."""
        row = await self._records.by_child(conversation_id)
        if row is not None and row.status == "cancelled":
            return False
        queued = self._runs.inbox(conversation_id, owner_id).enqueue(
            text, source=source, sender=sender
        )
        self._dispatcher.note_ledger_prompt(queued.id, ledger_prompt_message_id, ledger_thread)
        await self._dispatcher.dispatch(owner_id, conversation_id)
        return True


def status_of(run: Run) -> str:
    if run.status is RunStatus.done:
        return "done"
    if run.status is RunStatus.cancelled:
        return "cancelled"
    # A run a limit ended is unfinished work the parent can continue, not a failure.
    return "blocked" if run.status is RunStatus.blocked and run.stop_cause else "failed"


def _stored_status(outcome: _Outcome) -> str:
    """A limit ending is reported as blocked but stored as stopped: terminal, so it holds
    no slot and the panel does not offer an approval for it."""
    if outcome.status == "blocked" and outcome.cause in _LIMIT_LABELS:
        return "stopped"
    return outcome.status


def stopped_phrase(cause: StopCause | None) -> str:
    """How a stopped agent is described: who stopped it, when that is known."""
    match cause:
        case StopCause.operator:
            return "stopped by the operator"
        case StopCause.parent:
            return "stopped by its parent"
        case _:
            return "stopped"


_LIMIT_LABELS = {StopCause.time_limit: "time limit", StopCause.turn_limit: "turn limit"}


def _status_label(status: str, cause: StopCause | None) -> str:
    if status == "cancelled" and cause in (StopCause.operator, StopCause.parent):
        return f"cancelled ({stopped_phrase(cause)})"
    limit = _LIMIT_LABELS.get(cause) if cause is not None else None
    if status == "blocked" and limit is not None:
        return f"blocked ({limit})"
    return status


def _preview(summary: str, handle: str, limit: int) -> str:
    """A report over ``limit`` characters as its head and tail, with where to read it all."""
    shown = elide_middle(summary, limit)
    if shown == summary:
        return summary
    return (
        f"{shown}\n\n"
        + f"This report is long: read the full report with `agents_read {handle}`, "
        + "passing each page's `next_start` as `start` for the next."
    )


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


def _recovery(status: str, handle: str, cause: StopCause | None = None) -> list[str]:
    """What the parent can do next, for a report that did not end in finished work."""
    if status == "cancelled" and cause is StopCause.operator:
        return [OPERATOR_STOPPED]
    if status == "blocked" and cause in _LIMIT_LABELS:
        ran_out = "turns" if cause is StopCause.turn_limit else "time"
        return [
            f"its work is partial: it ran out of {ran_out} before finishing",
            f"continue it with `agents_message` to `{handle}`; it picks up where it stopped",
            f"or `agents_stop` `{handle}` and take the work back",
        ]
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
    cause: StopCause | None = None,
    preview_chars: int | None = None,
) -> str:
    """A sub-agent's hand-back as the launching model reads it: who, how it ended, what went
    wrong, its report, where its work is, and what to do next.

    Named by handle, since several sub-agents may share a spec. Callsign and class follow
    (the operator may use either); the spec appears only when it differs from the handle.
    """
    extras = [name for name in (designation(callsign, agent_class),) if name]
    if spec_name != handle:
        extras.append(spec_name)
    parts = [report_heading(handle, extras, _status_label(status, cause))]
    if error:
        parts.append(f"error: {error}")
    if summary:
        parts.append(summary if preview_chars is None else _preview(summary, handle, preview_chars))
    if parked:
        parts.append(parked)
    hints = _recovery(status, handle, cause)
    if hints:
        parts.append("\n".join(f"  help: {hint}" for hint in hints))
    return "\n\n".join(parts)
