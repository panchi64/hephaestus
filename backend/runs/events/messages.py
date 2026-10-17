"""Queued-message events: everything waiting in a conversation's inbox (``runs/inbox.py``)."""

from __future__ import annotations

from typing import Literal

from .base import Body

#: Who a queued message came from: the operator, a sub-agent reporting back to the thread
#: that launched it, another agent of the delegation tree, or the chassis itself (a notice
#: such as the open-tasks reminder). It is also the delivery order: the inbox hands the
#: model the operator's words first, then reports, then agent messages, then notices.
#:
#: This is the load-bearing answer to "who sent this". The envelope
#: (``services/subagents/report.py``) is a label anyone who can type angle brackets could
#: forge; this is structural and never arrives from outside.
MessageSource = Literal["operator", "subagent", "agent", "system"]


class MessageQueued(Body):
    """A message entered the conversation's inbox. ``text`` rides inline so a reattaching
    client can rebuild the pending row purely from replay. Additive to v1; no bump."""

    type: Literal["message.queued"] = "message.queued"
    message_id: str
    text: str
    source: MessageSource = "operator"
    #: For a message from another agent, the handle it was sent from.
    sender: str | None = None
    #: True for an operator message carrying attachments, file references or a command. It
    #: is never injected into a running turn; it opens a turn of its own after this one.
    turn: bool = False
    #: For a ``system`` message, which notice it is (``open-tasks``).
    notice: str | None = None


class MessageEdited(Body):
    """The operator rewrote a queued message's text before the run consumed it.
    ``text`` is the full replacement (not a delta), inline for the same reason as
    ``message.queued``'s: a reattaching client rebuilds the pending bubble purely
    from replay. Additive to v1; no bump."""

    type: Literal["message.edited"] = "message.edited"
    message_id: str
    text: str


class MessageWithdrawn(Body):
    """The operator withdrew a queued message before the run consumed it.
    Additive to v1; no bump."""

    type: Literal["message.withdrawn"] = "message.withdrawn"
    message_id: str


class MessageHeld(Body):
    """A queued message was held back from the drain, or released again.

    Held is not withdrawn: the message stays in the queue, in its place, and goes to the
    model the moment it is released. What it buys is the interval while the operator has
    the message open in an editor — the run would otherwise inject the draft they are
    still rewriting, and an edit landing a moment later would have nowhere to go.

    The state is the backend's, not the editor's, which is why it is a frame rather than a
    client-side flag: a second tab, a reload, or a client attaching mid-run all have to see
    the same queue the run does. Additive to v1; no bump."""

    type: Literal["message.held"] = "message.held"
    message_id: str
    #: True when the hold went on, False when it came off. Both directions ride one event
    #: rather than two, because a client renders the message's state, not the transition.
    held: bool


class InboxWaiting(Body):
    """The operator stopped the turn, so the inbox holds everything until they send again
    (``waiting`` True), or they did and it flows again (False). Additive to v1; no bump."""

    type: Literal["inbox.waiting"] = "inbox.waiting"
    waiting: bool


class MessageInjected(Body):
    """A queued message was handed to the model (emitted in drain order). From
    here on the message is part of the turn and will persist as a normal user
    message. Additive to v1; no bump."""

    type: Literal["message.injected"] = "message.injected"
    message_id: str
    #: Repeated from the queue frame rather than looked up, so a client attaching after the
    #: queue frame scrolled out of its replay window still knows whose message landed.
    source: MessageSource = "operator"
