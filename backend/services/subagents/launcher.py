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

    Everything here exists to *narrow* the child. The project it inherits so the child
    lands in the same scope; the permission it inherits as a ceiling it can never exceed;
    the workspace key it names so the child's own files are the parent's, or a delegated
    fork of the parent's, rather than a workspace re-derived down here that might not be
    the same one.
    """

    conversation_id: str | None = None
    #: The project the parent is filed under, inherited so a child works on the same code.
    project_id: str | None = None
    #: Whether the parent works in a worktree, which a sub-agent sharing or forking its
    #: files works in too.
    worktree: bool = False
    #: How much rope the *parent* has. A sub-agent runs at the stricter of this and what
    #: its spec asks for, so one approved launch cannot buy a level the operator never
    #: chose. None means the caller had no level to hand over and the default stands.
    permission: str | None = None
    #: The parent's workspace key. The child's own is derived from it — the same key to
    #: share those files, or ``<parent>/<delegation>`` to work in a fork of them — so this
    #: is the whole of what decides which filesystem a sub-agent ends up on. Deliberately
    #: the key and not a resolved root: resolving the parent's workspace here would open a
    #: sandbox or cut a checkout for an answer nothing reads, and the child resolves its
    #: own on its first file-tool call like every other run.
    workspace_key: str | None = None
    #: The run doing the launching. Only its id is kept, to tie a card back to the turn
    #: that asked for it.
    run_id: str | None = None


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
    #: ``running`` | ``blocked`` | ``done`` | ``failed`` | ``cancelled``.
    status: str
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
        isolate: bool = False,
    ) -> LaunchedSubagent:
        """Spawn ``agent`` on ``task`` and return as soon as its first turn is submitted.

        ``handle`` is normalised and made unique within the tree, so the one to use is the
        returned one. ``isolate`` asks for a merged-back copy of the parent's workspace; it
        can only ever add isolation to what the definition declares.

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
    async def message(self, owner_id: str, conversation_id: str, to: str, text: str) -> str:
        """Send ``text`` from the agent of ``conversation_id`` to ``to``: a handle in the
        same tree, or :data:`PARENT_ADDRESS`. Queued onto the recipient's running turn, or
        delivered by waking it. Returns the recipient's handle.

        Raises :class:`SubagentUnavailableError` when nothing in the tree answers to ``to``.
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
