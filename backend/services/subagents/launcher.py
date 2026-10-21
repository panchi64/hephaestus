"""The seam that lets an agent spawn, message and stop other agents.

Composing a chat turn happens *above* ``tools/`` in the dependency order, so a tool cannot
import it.
What a tool can do is resolve a capability by type out of the run's bag, and this is the
type it resolves — declared down here in ``services/``, implemented at the wiring layer
(``harness/manifests/_subagents.py``), and registered under this abstract type so the
concrete implementation is never named below its own layer.

**A spawned agent is an ordinary conversation**, composed by the same ``compose_turn`` an
operator's message goes through. What sets it apart: it starts hidden from the session list
(until the operator opens it), it belongs to its spawner's delegation tree, and every turn
it finishes is reported back to the thread that spawned it.

No call here waits on another run. Spawning and messaging return at once, and results
arrive as wakes, so a parent and its child can never deadlock on one another.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from services.agents.definition import AgentDefinition


class SubagentUnavailableError(Exception):
    """A sub-agent cannot be launched right now, with an agent-legible reason.

    Distinct from a bug: a misconfigured model registry, a workspace that cannot be forked,
    a cap the operator set — these are states the system can be in, and the launch tool
    turns this into a result the model can act on (do the work itself, wait for a running
    sub-agent, tell the operator what to switch on) rather than a failed turn.
    """


@dataclass(frozen=True)
class SubagentParent:
    """The thread doing the launching, as far as launching needs to know it.

    The child inherits the project, so it works on the same code; the permission, as a
    ceiling it can never exceed; and the workspace key, so its files are the parent's.
    """

    conversation_id: str | None = None
    project_id: str | None = None
    #: Whether the parent works in a worktree, which a sub-agent sharing its files works
    #: in too, and which a sub-agent given a worktree of its own is cut from.
    worktree: bool = False
    #: How far the *parent* may go. A sub-agent runs at the stricter of this and what its
    #: definition asks for. None means the caller had no level and the default stands.
    permission: str | None = None
    #: The parent's workspace key. The child's own is derived from it: the same key to
    #: share its files, or ``<owner>/<delegation>`` for a worktree of its own.
    workspace_key: str | None = None
    #: The run doing the launching. Only its id is kept, to tie a card back to the turn
    #: that asked for it.
    run_id: str | None = None
    #: The launching turn's prompt, under which the child's file changes are filed in the
    #: launcher's ledger (``tools.home.ledger_prompt``).
    ledger_prompt_message_id: str | None = None


@dataclass(frozen=True)
class LaunchedSubagent:
    """What launching produced. The **report is not here**: a sub-agent takes minutes, and
    the launch returns as soon as its first turn is submitted."""

    subagent_id: str
    #: The child's own conversation — where its transcript lives, and what the panel opens.
    conversation_id: str
    run_id: str
    name: str
    #: The handle this sub-agent actually ended up with, which is **not** necessarily the
    #: one the caller asked for: handles are unique within the launching thread, so a
    #: second ``explorer`` comes back as ``explorer-2``. Returned rather than assumed for
    #: exactly that reason — a caller that echoed its own request back to the model would
    #: hand it a name that addresses the *first* one.
    handle: str
    task: str
    #: The operator's name for it, and its class's key. Empty for a class with no pool.
    callsign: str = ""
    agent_class: str = ""


@dataclass(frozen=True)
class SubagentView:
    """A spawned agent as another agent reads it back, and as a card renders it.

    ``status`` is read off the agent's live Run where it has one, and off its stored
    record otherwise.
    """

    subagent_id: str
    conversation_id: str
    run_id: str
    #: The agent definition it runs.
    name: str
    #: What the spawning agent called this one, unique within its tree.
    handle: str
    task: str
    #: ``running`` | ``blocked`` | ``done`` | ``failed`` | ``cancelled`` | ``stopped``
    #: (ended at a time or turn limit).
    status: str
    #: The operator's name for it, and its class's key. Empty on rows from before either.
    callsign: str = ""
    agent_class: str = ""
    #: The thread that spawned it, and how far below the tree's root it runs.
    parent_conversation_id: str = ""
    depth: int = 1
    #: Its report, once it has one; its most recent answer while it is still working.
    summary: str | None = None
    error: str | None = None
    #: How full its own context window is. Null while unknown.
    context_used: int | None = None
    context_window: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True)
class ChildWorktree:
    """A worktree a sub-agent was spawned into, still open: its branch waits on its parent
    to merge or discard it."""

    handle: str
    conversation_id: str
    project_id: str
    path: Path
    branch: str
    #: The commit it was cut at; the sub-agent's own work is everything after it.
    base_ref: str
    #: The thread whose worktrees it is named under, and its own delegation id there.
    holder_id: str
    delegation_id: str


@dataclass(frozen=True)
class Recipient:
    """Who a message from one agent of a tree reaches."""

    conversation_id: str
    #: The handle the sender is told it reached.
    label: str
    #: The agent definition it runs; None for the thread at the top of the tree.
    agent_name: str | None
    #: Whether it is the sender's parent or any agent above that: a message up the tree
    #: carries what a report would, and directs nothing.
    upward: bool


#: How a spawned agent says its work ended: finished, waiting on its parent, or given up.
ReportStatus = Literal["done", "blocked", "failed"]


@dataclass(frozen=True)
class FiledReport:
    """What a spawned agent handed back with ``subagent_report``."""

    status: ReportStatus
    text: str


#: How the thread at the top of a tree is named to the agents in it.
ROOT_HANDLE = "main"

#: What ``to`` names to reach the agent that spawned the sender.
PARENT_ADDRESS = "parent"


class SubagentLauncher(ABC):
    """Spawn agents, message them, read them back, and stop them.

    Every address is relative to the calling thread's delegation tree: a handle names an
    agent anywhere in it, and nothing outside it is reachable.
    """

    @abstractmethod
    async def launch(
        self,
        owner_id: str,
        agent: AgentDefinition,
        task: str,
        *,
        handle: str,
        parent: SubagentParent | None = None,
        worktree: bool = False,
    ) -> LaunchedSubagent:
        """Spawn ``agent`` on ``task`` and return as soon as its first turn is submitted.

        ``handle`` is normalised and made unique within the tree, so the one to use is the
        returned one. ``worktree`` puts it on a branch and worktree of its own, cut from
        where the parent works, which only the parent merges; a definition that declares
        ``isolated`` gets one either way.

        Raises :class:`SubagentUnavailableError` for every state that prevents a launch:
        the parent may not delegate, the tree is at its depth or concurrency limit, no
        model can serve it, or its workspace cannot be given.
        """

    @abstractmethod
    async def recipient(self, owner_id: str, conversation_id: str, to: str) -> Recipient:
        """Who ``to`` names, from the agent of ``conversation_id``.

        Raises :class:`SubagentUnavailableError` when nothing in the tree answers to ``to``.
        """

    @abstractmethod
    async def message(
        self,
        owner_id: str,
        conversation_id: str,
        to: str,
        text: str,
        *,
        ledger_prompt_message_id: str | None = None,
        ledger_thread: str | None = None,
    ) -> str:
        """Send ``text`` from the agent of ``conversation_id`` to ``to``: a handle in the
        same tree, or :data:`PARENT_ADDRESS`. Queued onto the recipient's running turn, or
        delivered by waking it. Returns the recipient's handle.

        ``ledger_prompt_message_id`` is the prompt the sender files its changes under in
        ``ledger_thread``'s ledger; a wake the message causes files under it too, but only
        when the recipient's ledger is that same thread, else under the recipient's stored one.

        Raises :class:`SubagentUnavailableError` when nothing in the tree answers to ``to``.
        """

    @abstractmethod
    async def file_report(
        self, owner_id: str, conversation_id: str, run_id: str, report: FiledReport
    ) -> str:
        """Hold ``report`` as what the run ``run_id`` of the agent of ``conversation_id``
        hands back when it ends. Returns the handle of the agent it reaches.

        Raises :class:`SubagentUnavailableError` when no agent spawned that thread.
        """

    @abstractmethod
    async def read(self, owner_id: str, conversation_id: str, handle: str) -> SubagentView:
        """One agent of the caller's tree, with its report if it has finished."""

    @abstractmethod
    async def stop(self, owner_id: str, conversation_id: str, handle: str) -> list[str]:
        """Stop an agent the caller's thread spawned, directly or further down, and every
        agent below it. Returns the handles of the agents that were stopped."""

    @abstractmethod
    async def tree(self, owner_id: str, conversation_id: str) -> list[SubagentView]:
        """Every agent of the caller's delegation tree, oldest first."""

    @abstractmethod
    async def below(self, owner_id: str, conversation_id: str) -> list[SubagentView]:
        """Every agent below a thread, at any depth, newest first — the panel's read."""

    @abstractmethod
    async def awaiting_reports(self, owner_id: str, conversation_id: str) -> bool:
        """Whether an agent this thread spawned itself still has a turn under way, so a
        report to the thread is still to come."""

    @abstractmethod
    async def worktrees(self, owner_id: str, conversation_id: str) -> list[ChildWorktree]:
        """The open worktrees of the agents this thread spawned itself."""

    @abstractmethod
    async def worktree(self, owner_id: str, conversation_id: str, handle: str) -> ChildWorktree:
        """The open worktree of the agent called ``handle`` that this thread spawned.

        Raises :class:`SubagentUnavailableError` when there is no such agent, it is not
        this thread's own, or it has no worktree open.
        """
