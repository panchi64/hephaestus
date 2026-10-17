"""A conversation's inbox: every message bound for a thread, held until the model takes it.

The operator typing mid-turn, a sub-agent's report, another agent's message and a chassis
notice all land here. A message is never refused and never dropped: it leaves the inbox
only when it is handed to the model or when the operator withdraws it. The inbox outlives
any one run, so a run ending, failing or being stopped strands nothing; whatever is left
is picked up by the next turn on the thread (``harness/manifests/_inbox_dispatch.py``).

Delivery order is a hierarchy: the operator's words first, then sub-agent reports, then
agent messages, then notices, arrival order within each. Two things stall it:

- A **held** operator message (open in the operator's editor) stops the operator messages
  behind it, so their afterthought never reaches the model before their instruction.
  Everything else keeps flowing.
- An operator message carrying attachments, file references or a command is a **turn
  item**. It cannot be injected into a running request (its payload needs the turn's
  prelude), so it stops the operator messages behind it and opens a turn of its own.

Every mutation is synchronous (no ``await`` between the change and its event), so under
single-threaded asyncio a drain can never interleave with an enqueue and lose a message.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol, override
from uuid import uuid4

from .events import (
    EventBody,
    InboxWaiting,
    MessageEdited,
    MessageHeld,
    MessageInjected,
    MessageQueued,
    MessageSource,
    MessageWithdrawn,
    now_utc,
)

#: Delivery rank by source: lower goes first.
_RANK: dict[MessageSource, int] = {"operator": 0, "subagent": 1, "agent": 2, "system": 3}

#: What the model is told at the head of the first turn after the operator stopped one.
STOP_NOTICE = "The operator stopped your previous turn before it finished."


@dataclass(frozen=True)
class TurnPayload:
    """What an operator message carries beyond its text. ``command`` is the recorded
    invocation as JSON, which only the route layer reads back. The model, level and agent
    are what the operator picked for this send, applied when its turn opens."""

    attachment_ids: tuple[str, ...] = ()
    file_refs: tuple[str, ...] = ()
    command: str | None = None
    endpoint_id: str | None = None
    model: str | None = None
    permission_level: str | None = None
    agent: str | None = None


@dataclass(frozen=True)
class QueuedMessage:
    """One message waiting in a conversation's inbox."""

    id: str
    text: str
    queued_at: datetime
    source: MessageSource = "operator"
    #: For a message from another agent in the delegation tree, that agent's handle.
    sender: str | None = None
    #: Held back from the drain while the operator has this message open in an editor.
    held: bool = False
    payload: TurnPayload | None = None
    #: For a ``system`` message, which notice it is.
    notice: str | None = None
    #: Arrival order across the whole inbox; ties within a source rank break on it.
    seq: int = 0

    @property
    def is_turn(self) -> bool:
        return self.payload is not None


class InboxJournal(Protocol):
    """Where an inbox writes itself so a restart loses nothing. Fire-and-forget: the
    implementation queues the write and never blocks the caller."""

    def record(self, owner_id: str, conversation_id: str, message: QueuedMessage) -> None: ...

    def forget(self, owner_id: str, conversation_id: str, message_ids: Sequence[str]) -> None: ...

    def mark_waiting(
        self, owner_id: str, conversation_id: str, waiting: bool, stopped: bool
    ) -> None: ...


class InboxDispatch(Protocol):
    """What starts the next turn from an idle thread's inbox. Implemented above the
    substrate (``harness/manifests/_inbox_dispatch.py``), since starting a turn composes
    one."""

    async def dispatch(self, owner_id: str, conversation_id: str) -> None: ...

    def kick(self, owner_id: str, conversation_id: str) -> None:
        """Schedule :meth:`dispatch` from synchronous code."""
        ...

    def stalled(self, conversation_id: str) -> bool: ...

    async def settle(self, conversation_id: str) -> None:
        """Return once nothing is about to start a turn on this thread."""
        ...


class _NoJournal(InboxJournal):
    @override
    def record(self, owner_id: str, conversation_id: str, message: QueuedMessage) -> None:
        pass

    @override
    def forget(self, owner_id: str, conversation_id: str, message_ids: Sequence[str]) -> None:
        pass

    @override
    def mark_waiting(
        self, owner_id: str, conversation_id: str, waiting: bool, stopped: bool
    ) -> None:
        pass


NO_JOURNAL: InboxJournal = _NoJournal()

type EventSink = Callable[[EventBody], None]


def _discard(_body: EventBody) -> None:
    pass


@dataclass(frozen=True)
class StopMark:
    """The stop state an inbox had before an operator Stop (:meth:`ConversationInbox.stop`)."""

    waiting: bool
    stopped: bool


@dataclass
class ConversationInbox:
    """The inbox of one conversation (or of one stateless run, with no conversation)."""

    owner_id: str
    conversation_id: str | None = None
    #: Where the inbox's events go: the live run on the conversation, if there is one.
    sink: EventSink = _discard
    journal: InboxJournal = NO_JOURNAL
    _messages: list[QueuedMessage] = field(default_factory=list)
    _seq: int = 0
    #: Set by the operator's Stop: nothing starts a turn on its own until they act again.
    _waiting: bool = False
    #: Whether the next turn should open with :data:`STOP_NOTICE`.
    _stopped: bool = False

    # -- reading ------------------------------------------------------------------------

    @property
    def idle(self) -> bool:
        """Nothing queued and nothing pending on the operator: dropping it loses nothing."""
        return not self._messages and not self._waiting and not self._stopped

    @property
    def stop_notice_pending(self) -> bool:
        """Whether the next turn opens with :data:`STOP_NOTICE`; :meth:`take_stop_notice`
        consumes it once that turn is under way."""
        return self._stopped

    @property
    def messages(self) -> tuple[QueuedMessage, ...]:
        """Everything queued, in queue order."""
        return tuple(self._messages)

    @property
    def waiting_for_operator(self) -> bool:
        return self._waiting

    def get(self, message_id: str) -> QueuedMessage | None:
        return next((m for m in self._messages if m.id == message_id), None)

    def deliverable(self) -> list[QueuedMessage]:
        """What a model request may take now, in delivery order: the operator's messages up
        to the first held or turn item, and every message from anyone else."""
        operator: list[QueuedMessage] = []
        for message in self._messages:
            if message.source != "operator":
                continue
            if message.held or message.is_turn:
                break
            operator.append(message)
        others = [m for m in self._messages if m.source != "operator"]
        return sorted([*operator, *others], key=lambda m: (_RANK[m.source], m.seq))

    def has_deliverable(self) -> bool:
        return bool(self.deliverable())

    def next_turn_item(self) -> QueuedMessage | None:
        """The operator turn item at the front of the operator's queue, when nothing of
        theirs is ahead of it."""
        for message in self._messages:
            if message.source != "operator":
                continue
            return message if message.is_turn and not message.held else None
        return None

    def lead(self) -> QueuedMessage | None:
        """What a turn started from the inbox opens on: the front turn item, else the first
        message in delivery order. The rest is injected into that turn's first request."""
        turn = self.next_turn_item()
        if turn is not None:
            return turn
        deliverable = self.deliverable()
        return deliverable[0] if deliverable else None

    # -- writing ------------------------------------------------------------------------

    def restore(
        self,
        messages: Sequence[QueuedMessage],
        *,
        waiting: bool = False,
        stopped: bool = False,
        record: bool = True,
    ) -> None:
        """Put messages back in their original places, by their own ``seq``: what the
        journal held across a restart, or one a caller took and could not deliver. A
        message already present is skipped. Emits no events. ``record=False`` is for
        messages that came from the journal itself."""
        present = {m.id for m in self._messages}
        added = [m for m in messages if m.id not in present]
        self._messages = sorted([*self._messages, *added], key=lambda m: m.seq)
        self._seq = max([self._seq, *(m.seq for m in added)])
        if record:
            for message in added:
                self._record(message)
        self._waiting = self._waiting or waiting
        self._stopped = self._stopped or stopped

    def enqueue(
        self,
        text: str,
        *,
        source: MessageSource = "operator",
        sender: str | None = None,
        payload: TurnPayload | None = None,
        notice: str | None = None,
        ahead: bool = False,
    ) -> QueuedMessage:
        """Queue a message. ``ahead`` puts it before everything already queued: the
        operator's first word after a Stop leads what waited behind the Stop."""
        if ahead and self._messages:
            seq = min(m.seq for m in self._messages) - 1
        else:
            # Clock-seeded so a message sent before the stored ones are restored still
            # sorts after them.
            self._seq = max(self._seq + 1, time.time_ns())
            seq = self._seq
        message = QueuedMessage(
            id=uuid4().hex,
            text=text,
            queued_at=now_utc(),
            source=source,
            sender=sender,
            payload=payload,
            notice=notice,
            seq=seq,
        )
        self._messages.append(message)
        self._messages.sort(key=lambda m: m.seq)
        self._record(message)
        self.sink(
            MessageQueued(
                message_id=message.id,
                text=text,
                source=source,
                sender=sender,
                turn=message.is_turn,
                notice=notice,
            )
        )
        return message

    def edit(self, message_id: str, text: str) -> bool:
        """Rewrite a queued message's text, keeping its id and place. False when unknown."""
        message = self.get(message_id)
        if message is None:
            return False
        self._replace(replace(message, text=text))
        self.sink(MessageEdited(message_id=message_id, text=text))
        return True

    def hold(self, message_id: str, held: bool) -> bool:
        """Hold a queued message back from the drain, or release it. Setting the value it
        already has succeeds and emits nothing, since a client may be reconciling."""
        message = self.get(message_id)
        if message is None:
            return False
        if message.held != held:
            self._replace(replace(message, held=held))
            self.sink(MessageHeld(message_id=message_id, held=held))
        return True

    def withdraw(self, message_id: str) -> bool:
        message = self.get(message_id)
        if message is None:
            return False
        self._remove([message])
        self.sink(MessageWithdrawn(message_id=message_id))
        return True

    def drain(self) -> list[QueuedMessage]:
        """Take every deliverable message, in delivery order, emitting ``message.injected``
        for each: the caller is committing to hand them to the model."""
        drained = self.deliverable()
        self._take(drained)
        return drained

    def take(self, message: QueuedMessage) -> None:
        """Take one message a turn is about to open on."""
        if self.get(message.id) is not None:
            self._take([message])

    def stop(self) -> StopMark:
        """The operator stopped the turn: hold every delivery behind their next action, and
        tell the model about the stop when they take it. The mark undoes it."""
        mark = StopMark(self._waiting, self._stopped)
        self._apply(waiting=True, stopped=True)
        return mark

    def undo_stop(self, mark: StopMark) -> None:
        """Put the stop state back as it was before the :meth:`stop` that returned ``mark``,
        for a Stop that turned out to have nothing to stop."""
        self._apply(waiting=mark.waiting, stopped=mark.stopped)

    def clear(self) -> None:
        """Drop everything, for a thread that no longer exists. Emits no events."""
        self._remove(list(self._messages))
        self._waiting = False
        self._stopped = False
        self._journal_state()

    def resume(self) -> None:
        """The operator acted again: let the inbox flow."""
        self._set_waiting(False)

    def take_stop_notice(self) -> str | None:
        """:data:`STOP_NOTICE` once after a Stop, for the turn that follows it."""
        if not self._stopped:
            return None
        self._stopped = False
        self._journal_state()
        return STOP_NOTICE

    # -- internals ----------------------------------------------------------------------

    def _apply(self, *, waiting: bool, stopped: bool) -> None:
        self._stopped = stopped
        if self._waiting == waiting:
            self._journal_state()
        else:
            self._set_waiting(waiting)

    def _set_waiting(self, waiting: bool) -> None:
        if self._waiting == waiting:
            return
        self._waiting = waiting
        self._journal_state()
        self.sink(InboxWaiting(waiting=waiting))

    def _journal_state(self) -> None:
        if self.conversation_id is not None:
            self.journal.mark_waiting(
                self.owner_id, self.conversation_id, self._waiting, self._stopped
            )

    def _take(self, messages: Sequence[QueuedMessage]) -> None:
        if not messages:
            return
        self._remove(messages)
        for message in messages:
            self.sink(MessageInjected(message_id=message.id, source=message.source))

    def _replace(self, message: QueuedMessage) -> None:
        self._messages = [message if m.id == message.id else m for m in self._messages]
        self._record(message)

    def _remove(self, messages: Sequence[QueuedMessage]) -> None:
        ids = {m.id for m in messages}
        self._messages = [m for m in self._messages if m.id not in ids]
        if self.conversation_id is not None:
            self.journal.forget(self.owner_id, self.conversation_id, sorted(ids))

    def _record(self, message: QueuedMessage) -> None:
        if self.conversation_id is not None:
            self.journal.record(self.owner_id, self.conversation_id, message)
