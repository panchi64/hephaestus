"""A turn the inbox starts for the operator: a message they queued while the thread was busy,
composed exactly as if they had sent it now."""

from __future__ import annotations

from fastapi import FastAPI

from routes import deps
from routes.chat.schemas import ChatCreated, CommandInvocation
from routes.chat.submit import submit_turn, thread_models
from routes.deps import OPERATOR_ID
from runs import QueuedMessage, TurnPayload
from services.agents import DEFAULT_AGENT


async def submit_queued(
    app: FastAPI, conversation_id: str, message: QueuedMessage
) -> ChatCreated | None:
    """Start ``message`` as a turn of its own, at the model, level and agent picked when it
    was sent. Raises ``ConversationBusyError`` when another turn took the thread first;
    that turn drains the inbox instead.

    The message leaves the inbox before the first await, so an edit or withdraw that lands
    meanwhile is told it is too late rather than silently ignored. Any failure puts it back
    in its place. Returns None when it was withdrawn before the claim."""
    registry = deps.registry(app)
    registry.claim(conversation_id, OPERATOR_ID)
    inbox = registry.inbox(conversation_id, OPERATOR_ID)
    taken = inbox.get(message.id)
    if taken is None:
        registry.release(conversation_id, OPERATOR_ID)
        return None
    inbox.take(taken)
    try:
        payload = taken.payload or TurnPayload()
        store = deps.store(app)
        # The same order a send applies them in: the binding first, so the models resolve
        # against the agent this turn runs.
        if payload.permission_level is not None:
            _ = await store.set_permission_level(conversation_id, payload.permission_level)
        if payload.agent is not None:
            await store.set_agent_name(
                conversation_id, None if payload.agent == DEFAULT_AGENT else payload.agent
            )
        models = await thread_models(app, conversation_id, payload.endpoint_id, payload.model)
        command = (
            None
            if payload.command is None
            else CommandInvocation.model_validate_json(payload.command).recorded()
        )
        return await submit_turn(
            app,
            prompt=taken.text,
            conversation_id=conversation_id,
            models=models,
            attachment_ids=list(payload.attachment_ids) or None,
            file_refs=list(payload.file_refs) or None,
            command=command,
        )
    except BaseException:
        inbox.restore([taken], waiting=False)
        raise
    finally:
        registry.release(conversation_id, OPERATOR_ID)
