"""A conversation's inbox: what waits for the model, and the operator's hand on it.

Addressed by conversation rather than by run because the inbox outlives every run on the
thread: a message queued during one turn is still the operator's to edit, hold or withdraw
after that turn has ended.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes import deps
from routes.deps import OPERATOR_ID
from runs import ConversationInbox, QueuedMessage
from runs.events import MessageSource

router = APIRouter(prefix="/conversations", tags=["conversations"])


class InboxMessageOut(BaseModel):
    id: str
    text: str
    source: MessageSource
    sender: str | None
    held: bool
    #: Opens a turn of its own rather than joining the running one.
    turn: bool
    notice: str | None
    queued_at: datetime


class InboxOut(BaseModel):
    messages: list[InboxMessageOut]
    #: The operator stopped a turn; nothing starts until they act again.
    waiting_for_operator: bool
    #: Deliveries wait for the operator's next turn: the delegation tree took its
    #: unattended turns, or no turn could be opened on the thread.
    agents_paused: bool
    #: The run on the thread, if one is live.
    run_id: str | None = None


class InboxEdit(BaseModel):
    text: str


class InboxHold(BaseModel):
    held: bool


def _out(message: QueuedMessage) -> InboxMessageOut:
    return InboxMessageOut(
        id=message.id,
        text=message.text,
        source=message.source,
        sender=message.sender,
        held=message.held,
        turn=message.is_turn,
        notice=message.notice,
        queued_at=message.queued_at,
    )


async def _inbox(request: Request, conversation_id: str) -> ConversationInbox:
    if not await deps.store(request).exists(conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    return deps.registry(request).inbox(conversation_id, OPERATOR_ID)


def _operators(inbox: ConversationInbox, message_id: str) -> QueuedMessage:
    """The operator's own queued message. A report or a notice is the sender's words, not
    the operator's to change."""
    message = inbox.get(message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="queued message not found")
    if message.source != "operator":
        raise HTTPException(status_code=403, detail="only your own messages can be changed")
    return message


@router.get("/{conversation_id}/inbox", response_model=InboxOut)
async def read_inbox(conversation_id: str, request: Request) -> InboxOut:
    inbox = await _inbox(request, conversation_id)
    return _inbox_out(request, conversation_id, inbox)


@router.post("/{conversation_id}/inbox/settle", response_model=InboxOut)
async def settle_inbox(conversation_id: str, request: Request) -> InboxOut:
    """The inbox once any turn it is about to start has started: waits out the thread's
    pending dispatch, so a client whose run just ended learns the run that follows it
    without polling for one."""
    inbox = await _inbox(request, conversation_id)
    dispatcher = deps.inbox_dispatcher(request)
    if dispatcher is not None:
        await dispatcher.settle(conversation_id)
    return _inbox_out(request, conversation_id, inbox)


def _inbox_out(request: Request, conversation_id: str, inbox: ConversationInbox) -> InboxOut:
    dispatcher = deps.inbox_dispatcher(request)
    run = deps.registry(request).active_run_for(conversation_id, OPERATOR_ID)
    return InboxOut(
        messages=[_out(m) for m in inbox.messages],
        waiting_for_operator=inbox.waiting_for_operator,
        agents_paused=dispatcher is not None and dispatcher.stalled(conversation_id),
        run_id=run.id if run is not None else None,
    )


@router.patch("/{conversation_id}/inbox/{message_id}", response_model=InboxMessageOut)
async def edit_queued(
    conversation_id: str, message_id: str, body: InboxEdit, request: Request
) -> InboxMessageOut:
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="text must not be empty")
    inbox = await _inbox(request, conversation_id)
    _ = _operators(inbox, message_id)
    _ = inbox.edit(message_id, body.text)
    edited = inbox.get(message_id)
    assert edited is not None
    return _out(edited)


@router.post("/{conversation_id}/inbox/{message_id}/hold", response_model=InboxMessageOut)
async def hold_queued(
    conversation_id: str, message_id: str, body: InboxHold, request: Request
) -> InboxMessageOut:
    """Hold an operator message back while they rewrite it, or release it. Holding it to the
    value it already has is a 200, so a client reconciling its own state need not know."""
    inbox = await _inbox(request, conversation_id)
    _ = _operators(inbox, message_id)
    _ = inbox.hold(message_id, body.held)
    held = inbox.get(message_id)
    assert held is not None
    if not body.held:
        await _dispatch(request, conversation_id)
    return _out(held)


@router.delete("/{conversation_id}/inbox/{message_id}", status_code=200)
async def withdraw_queued(
    conversation_id: str, message_id: str, request: Request
) -> dict[str, str]:
    inbox = await _inbox(request, conversation_id)
    _ = _operators(inbox, message_id)
    _ = inbox.withdraw(message_id)
    await _dispatch(request, conversation_id)
    return {"status": "withdrawn"}


@router.post("/{conversation_id}/inbox/resume", response_model=InboxOut)
async def resume_inbox(conversation_id: str, request: Request) -> InboxOut:
    """Release what waits since the operator stopped a turn, without writing anything."""
    inbox = await _inbox(request, conversation_id)
    inbox.resume()
    await _dispatch(request, conversation_id)
    return await read_inbox(conversation_id, request)


async def _dispatch(request: Request, conversation_id: str) -> None:
    dispatcher = deps.inbox_dispatcher(request)
    if dispatcher is not None:
        await dispatcher.dispatch(OPERATOR_ID, conversation_id)
