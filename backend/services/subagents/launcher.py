"""The seam that lets the *agent* launch a sub-agent.

Composing a chat turn happens *above* ``tools/`` in the dependency order, so a tool cannot
import it.
What a tool can do is resolve a capability by type out of the run's bag, and this is the
type it resolves — declared down here in ``services/``, implemented at the wiring layer
(``harness/manifests/_subagents.py``), and registered under this abstract type so the
concrete implementation is never named below its own layer.

**A sub-agent is an ordinary conversation.** It is composed by the same ``compose_turn``
an operator's own message goes through, runs the same engine, persists the same message
rows, and is measured by the same context gauge. What makes it a sub-agent rather than a
thread is three things: it is hidden from the session list, nobody can type into it, and
when it finishes its report is delivered back to the thread that launched it. None of that
is a second engine, and the moment it starts to become one, something here is wrong.

The interface is three verbs. ``launch`` returns the instant the child's first turn is
submitted — it never waits, because waiting is the whole thing this replaced. ``read`` is
how a parent checks on one it is still expecting. ``live`` is how the cap is counted and
how the panel backfills.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from services.subagents.spec import SubagentSpec


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
    #: Where the parent works, which a sub-agent sharing or forking its files inherits.
    workspace_kind: str | None = None
    #: How much rope the *parent* has. A sub-agent runs at the stricter of this and what
    #: its spec asks for, so one approved launch cannot buy a level the operator never
    #: chose. None means the caller had no level to hand over and the default stands.
    permission: str | None = None
    #: The parent's workspace key. The child's own is derived from it — the same key to
    #: share those files, or ``<parent>/<delegation>`` to work in a fork of them — so this
    #: is the whole of what decides which filesystem a sub-agent ends up on. Deliberately
    #: the key and not a resolved root: resolving the parent's workspace here would open a
    #: container or cut a checkout for an answer nothing reads, and the child resolves its
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
    """A sub-agent as the parent reads it back, and as a card renders it.

    ``status`` is derived from the child's Run and its stored record rather than stored
    twice — "is it still working" is a fact about the run registry, and a second copy of it
    would be the one that goes stale.
    """

    subagent_id: str
    conversation_id: str
    run_id: str
    #: Which sub-agent this is — the roster spec it was launched from.
    name: str
    #: What the launching model called this one, and the only name it is ever shown. Beside
    #: ``name`` rather than instead of it: the spec says what kind of worker this is and the
    #: handle says which piece of work, and a card that dropped either would be a card the
    #: operator cannot match to what the agent said it was doing.
    handle: str
    task: str
    #: ``running`` | ``blocked`` | ``done`` | ``failed`` | ``cancelled``.
    status: str
    #: Its report, once it has one; its most recent answer while it is still working.
    summary: str | None = None
    error: str | None = None
    #: How full the child's own context window is — the card's ring. Null while unknown.
    context_used: int | None = None
    context_window: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class SubagentLauncher(ABC):
    """Launch a sub-agent, redirect one, read one back, and list the ones that exist."""

    @abstractmethod
    async def launch(
        self,
        owner_id: str,
        spec: SubagentSpec,
        task: str,
        *,
        handle: str,
        parent: SubagentParent | None = None,
        isolate: bool = False,
    ) -> LaunchedSubagent:
        """Open a sub-agent on ``task`` and return as soon as its first turn is submitted.

        ``handle`` is the name the *caller* gives this sub-agent, and afterwards the only
        one it is addressed by. Normalised and made unique within the launching thread here
        rather than by the caller — which is why the handle to use is the one on the
        returned :class:`LaunchedSubagent` and not the one that was passed in. Empty falls
        back to the spec's own name, the same thing every row that predates handles was
        backfilled to.

        ``isolate`` asks for the sub-agent to work in its *own* copy of the workspace,
        merged back when it finishes, rather than in the launching thread's files. The
        caller asks for it when it means to carry on working meanwhile — two agents in one
        tree is fine when one of them is waiting, and not otherwise. It can only ever add
        isolation: a spec that already works apart is never pulled back in by omitting it.

        **It does not wait.** The sub-agent runs as its own Run on the same substrate and
        takes minutes; blocking the launching turn on it would burn that turn's whole step
        budget watching a progress bar, which is exactly what the synchronous delegation
        this replaced did.

        ``task`` has to stand alone. The child never sees the launching conversation — it
        starts from an empty history with its spec's brief and this one message — so a task
        that says "fix the bug we discussed" describes nothing the sub-agent can act on.

        Raises :class:`SubagentUnavailableError` for every state the system can legitimately
        be in that prevents a launch.
        """

    @abstractmethod
    async def steer(
        self, owner_id: str, conversation_id: str, handle: str, message: str
    ) -> SubagentView:
        """Redirect a sub-agent that is still working, and return where it has got to.

        Addressed by ``(conversation_id, handle)`` — the thread that launched it and the
        name that thread gave it. The pair is the whole address: a handle is unique inside
        a thread and says nothing outside one, and scoping the lookup this way is what stops
        an agent reaching a *sibling* thread's sub-agent by guessing a plausible name.

        The message rides the same road the operator's own mid-turn message does: queued on
        the sub-agent's Run and handed to its *next, not-yet-sent* request, so it can never
        interrupt a model that is mid-stream. Nothing here waits for it to be read.

        **Delivery is not guaranteed, and callers must say so.** A sub-agent that is queued
        behind the lane gets it at its first request; one parked on an approval gets it when
        the operator answers and it resumes; and one that finishes before its next request
        never gets it at all, because what is still pending at a run's terminal is dropped —
        correctly, since a direction to a finished sub-agent is about work that is over.

        Raises :class:`SubagentUnavailableError` when that thread has no sub-agent by that
        name, or when the one it has already settled — the caller wants its report at that
        point, not this.
        """

    @abstractmethod
    async def read(self, owner_id: str, conversation_id: str, handle: str) -> SubagentView:
        """One sub-agent's current state, with its report if it has finished.

        Addressed the same way :meth:`steer` is, and scoped to the launching thread for the
        same reason. Raises :class:`SubagentUnavailableError` naming the handle when that
        thread never launched one by it — which is a thing the caller can act on (it
        misremembered a name, and the names it has are what ``live`` lists) rather than a
        failure of the machine.
        """

    @abstractmethod
    async def live(
        self, owner_id: str, *, conversation_id: str | None = None
    ) -> list[SubagentView]:
        """Every sub-agent still working — all of the owner's, or one parent's.

        Counted against the operator's cap, and what a parent lists when deciding whether
        it is waiting on anything. A sub-agent parked on an approval is *live*: it has not
        reported, and the thing it is waiting for is the operator, not a slot.
        """

    @abstractmethod
    async def for_parent(self, owner_id: str, conversation_id: str) -> list[SubagentView]:
        """Every sub-agent a thread has launched, newest first — live and finished.

        The panel's read, and the reason it is on this seam rather than on the register:
        a live sub-agent's run knows more than its stored row does (whether it has parked,
        how full its context has become), and folding the two together is this
        implementation's job rather than the route's.
        """
