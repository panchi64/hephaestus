"""Carrying words between the agents of a delegation tree: a finished agent's report up to
the thread that spawned it, and a message from one agent to another.

**Both go into the recipient's inbox** (``runs/inbox.py``), the same road the operator's
own mid-turn messages take. A running recipient takes them at its next model request; an
idle one is started by the inbox dispatcher (``_inbox_dispatch.py``), which also keeps the
tree's unattended-turn budget.

**Every report says how the work ended.** A spawned agent files one with
``subagent_report`` (``tools/report.py``), held here by run until that run ends. An
unattended turn that ends without one is reported as failed, so the parent always hears
which of done, blocked or failed it is dealing with.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

from core.exceptions import NotFoundError
from core.fork import MergeReport
from harness.manifest import HarnessContext
from harness.manifests._inbox_dispatch import WAKE_CHAIN_LIMIT, WAKE_KIND, InboxDispatcher
from models.subagent import SubagentRecord
from runs import Run, RunRegistry, RunStatus
from runs.events import MessageSource
from runs.lanes import CHAT_TURN_KINDS, is_attended
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

__all__ = ["NO_REPORT", "WAKE_CHAIN_LIMIT", "WAKE_KIND", "SubagentWake", "report_text"]

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
            ),
            sender=handle,
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
        ledger_prompt_message_id: str | None = None,
    ) -> bool:
        """Put a report (``source="subagent"``) or another agent's message
        (``source="agent"``) from the agent ``sender`` names into a thread's inbox. False
        when the recipient is an agent that was stopped, which nothing wakes.

        ``ledger_prompt_message_id`` is the prompt of the sender's running turn, which a wake
        this message causes files its changes under."""
        row = await self._records.by_child(conversation_id)
        if row is not None and row.status == "cancelled":
            return False
        queued = self._runs.inbox(conversation_id, owner_id).enqueue(
            text, source=source, sender=sender
        )
        self._dispatcher.note_ledger_prompt(queued.id, ledger_prompt_message_id)
        await self._dispatcher.dispatch(owner_id, conversation_id)
        return True


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
    parts = [report_heading(handle, extras, status)]
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
