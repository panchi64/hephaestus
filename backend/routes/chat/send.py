"""The turn-starting routes: send a message, regenerate an answer, edit a request.

All three create a Run and return its id; the client streams it from ``/runs/{id}/events``.
Regenerate and edit differ from a send only in how the store repositions the active leaf
first; the launch is identical (``submit_turn``).
"""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, HTTPException, Request

from core.exceptions import NotFoundError
from routes import deps
from routes.changes import rewind_files
from routes.chat.schemas import (
    ChatCreate,
    ChatCreated,
    ChatQueued,
    EditCreate,
    RegenerateCreate,
)
from routes.chat.submit import resolve_models, submit_turn, thread_agent, thread_models
from routes.deps import OPERATOR_ID
from runs import ConversationBusyError, RunRegistry, TurnPayload
from services.agents import DEFAULT_AGENT
from services.conversations import ConversationBinding, ConversationStore
from services.turn_stamps import still_named

router = APIRouter(prefix="/chat", tags=["chat"])

_after_restore = deps.after_restore


async def _validate_attachments(request: Request, attachment_ids: list[str]) -> None:
    """Every attached id must name an upload the operator owns; foreign or unknown ids get a
    404 rather than being silently dropped at run time."""
    if not attachment_ids:
        return
    # One decrypt-free query for the whole list, not a thread hop per id.
    owned = await deps.uploads(request).owned_ids(OPERATOR_ID, attachment_ids)
    for upload_id in attachment_ids:
        if upload_id not in owned:
            raise HTTPException(status_code=404, detail=f"attachment {upload_id!r} not found")


def _enqueue(registry: RunRegistry, conversation_id: str, body: ChatCreate) -> ChatQueued:
    """Queue a send into a busy conversation's inbox; never refused. A plain message joins
    the running turn at its next model request; one with attachments, file references or a
    command waits to open its own turn through the prelude. Synchronous, so nothing drains
    between the lookup and the enqueue.

    A command is queued unexpanded, since the operator can edit a queued message and an
    expansion would be text they never wrote. A turn item keeps its picked model, level and
    agent."""
    payload = (
        TurnPayload(
            attachment_ids=tuple(body.attachment_ids),
            file_refs=tuple(body.file_refs),
            command=body.command.model_dump_json() if body.command else None,
            endpoint_id=body.endpoint_id,
            model=body.model,
            permission_level=body.permission_level,
            agent=body.agent,
        )
        if body.attachment_ids or body.command or body.file_refs
        else None
    )
    inbox = registry.inbox(conversation_id, OPERATOR_ID)
    # After a Stop the operator's new word leads what waited behind the Stop.
    message = inbox.enqueue(body.prompt, payload=payload, ahead=inbox.waiting_for_operator)
    # The operator acting again clears a Stop's hold, or this would wait forever behind it.
    inbox.resume()
    run = registry.active_run_for(conversation_id, OPERATOR_ID)
    return ChatQueued(
        run_id=run.id if run is not None else None,
        conversation_id=conversation_id,
        queued_message_id=message.id,
    )


async def _queued_after_losing(
    request: Request, registry: RunRegistry, conversation_id: str, body: ChatCreate
) -> ChatQueued:
    """A turn started on the thread under this send's claim, so the send joins its inbox
    instead of being refused."""
    queued = _enqueue(registry, conversation_id, body)
    await _retire_stop_marker(deps.store(request), conversation_id, body)
    return queued


def _resumed(request: Request, conversation_id: str) -> None:
    """The operator started a turn, so what waited since they stopped one flows again. Called
    after submit and before the first request, which then takes the inbox behind their message."""
    deps.registry(request).inbox(conversation_id, OPERATOR_ID).resume()


async def _retire_stop_marker(
    store: ConversationStore, conversation_id: str, body: ChatCreate
) -> None:
    """Clear the stop marker this turn resumes, if it says it resumes one.

    Best-effort and loose about which marker: the store resolves an unrecognized id to the
    newest stop, since a live client names an unpersisted turn by its optimistic id. A stale
    client may thus retire a newer stop, losing a marker but never a turn; the resume runs
    regardless."""
    if body.continues_message_id is None:
        return
    _ = await store.clear_blocked_reason(conversation_id, body.continues_message_id)


async def _validate_new_thread_binding(request: Request, body: ChatCreate) -> None:
    """404 a thread filed under a nonexistent project before anything is created; a silent
    unfiled thread would put the work where the operator won't look."""
    if body.project_id is None:
        return
    try:
        _ = await deps.projects(request).get(OPERATOR_ID, body.project_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="project not found") from None


@router.post("", status_code=202, response_model=ChatCreated | ChatQueued)
async def create_chat(body: ChatCreate, request: Request) -> ChatCreated | ChatQueued:
    if not body.prompt.strip() and not body.attachment_ids:
        raise HTTPException(status_code=422, detail="prompt must not be empty")
    await _validate_attachments(request, body.attachment_ids)
    if body.conversation_id is None:
        await _validate_new_thread_binding(request, body)

    store = deps.store(request)
    agent_name = None if body.agent == DEFAULT_AGENT else body.agent
    stored: ConversationBinding | None = None
    if body.conversation_id is not None:
        # An unknown id must not silently spawn orphan messages.
        if not await store.exists(body.conversation_id, OPERATOR_ID):
            raise HTTPException(status_code=404, detail="conversation not found")
        await _after_restore(request)
        binding = stored = await store.binding(body.conversation_id)
        if body.agent is not None:
            binding = replace(binding, agent_name=agent_name)
    else:
        # Explicit `project_id`, else the active project, else unfiled. A filed thread may
        # write that project's folder; a throwaway compare pane writes only its own scratch.
        project_id = body.project_id or await deps.active_project(request)
        binding = ConversationBinding(
            project_id=None if body.ephemeral else project_id,
            agent_name=agent_name,
        )
    applied = await thread_agent(request, binding, body.conversation_id)
    if agent_name is not None and applied.agent.name != agent_name:
        raise HTTPException(status_code=422, detail=f"no published agent named {agent_name!r}")

    # Before creating, so a model failure leaves no empty orphan conversation.
    models = await resolve_models(request, body.endpoint_id, body.model, agent=applied.agent)

    if body.conversation_id is not None:
        conversation_id = body.conversation_id
    else:
        conversation_id = await store.create_conversation(
            OPERATOR_ID,
            ephemeral=body.ephemeral,
            permission=body.permission_level,
            project_id=binding.project_id,
            agent_name=agent_name,
        )

    # Claimed before the remaining awaits and the `submit`: a live-run check alone leaves a
    # gap a concurrent regenerate/edit/delete could occupy without registering a run. A new
    # conversation's id is unknown to others, so this always succeeds for it.
    #
    # A busy conversation queues the send into the inbox; whatever holds the thread takes it next.
    registry = deps.registry(request)
    try:
        registry.claim(conversation_id, OPERATOR_ID)
    except ConversationBusyError:
        return await _queued_after_losing(request, registry, conversation_id, body)
    try:
        # Written before `submit_turn` reads the binding so this send runs at its level and
        # the next inherits it. After the claim on purpose: a send that only queues gets no
        # turn at the new level, and moving the stored one would leave the control reading a
        # level nothing runs at.
        if stored is not None and body.permission_level is not None:
            _ = await store.set_permission_level(conversation_id, body.permission_level)
        if stored is not None and body.agent is not None:
            await store.set_agent_name(conversation_id, agent_name)
        try:
            created = await submit_turn(
                request,
                prompt=body.prompt,
                conversation_id=conversation_id,
                models=models,
                attachment_ids=body.attachment_ids,
                file_refs=body.file_refs,
                command=body.command.recorded() if body.command else None,
                ephemeral=body.ephemeral,
            )
        except (ConversationBusyError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and (
                exc.status_code != 409 or exc.detail != deps.CONVERSATION_BUSY_DETAIL
            ):
                raise
            # Another turn took the thread: restore the binding, as this send only queues
            # (a turn item carries its own picks).
            if stored is not None and body.permission_level is not None:
                _ = await store.set_permission_level(conversation_id, stored.permission)
            if stored is not None and body.agent is not None:
                await store.set_agent_name(conversation_id, stored.agent_name)
            return await _queued_after_losing(request, registry, conversation_id, body)
        # Only once submitted: a rejected send leaves the unfinished turn, so it keeps the
        # marker. The claim doesn't settle that; `submit` has its own check, and a race it
        # catches becomes a 409 from in here.
        await _retire_stop_marker(store, conversation_id, body)
        _resumed(request, conversation_id)
        return created
    finally:
        deps.release_conversation(request, conversation_id)


@router.post("/regenerate", status_code=202, response_model=ChatCreated)
async def regenerate(body: RegenerateCreate, request: Request) -> ChatCreated:
    """Re-answer a turn: drop back to the user request that produced ``message_id`` and run
    again with no new prompt, recording a new version beside the old. A model override
    regenerates with a different model.

    A slash-command turn is re-answered with the command resolved again: the replayed
    history has only the literal `/reviewer …` (the expansion was tail context), so
    otherwise the second answer would be to a different question."""
    store = deps.store(request)
    if not await store.exists(body.conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    await _after_restore(request)
    # Claimed before the model resolve and the leaf-moving `regenerate_point`, both real
    # `await`s a near-simultaneous regenerate/edit could slip through, since neither request
    # has registered a run. Released once this request settles, win or lose.
    deps.claim_conversation(request, body.conversation_id)
    try:
        models = await thread_models(request, body.conversation_id, body.endpoint_id, body.model)
        if body.restore_files is not None:
            # Planned against the leaf as it is now; `regenerate_point` moves it.
            _ = await rewind_files(
                request, body.conversation_id, body.message_id, body.restore_files.preview_token
            )
        if not await store.regenerate_point(body.conversation_id, body.message_id):
            raise HTTPException(status_code=404, detail="message not found")
        created = await submit_turn(
            request,
            prompt=None,
            conversation_id=body.conversation_id,
            models=models,
            # After `regenerate_point`, which puts the re-answered turn at the leaf; before
            # it this would report whichever turn the thread ended on. `@` references need
            # no such step: their marker persisted with the prompt.
            command=(await store.turn_stamps(body.conversation_id)).command,
        )
        _resumed(request, body.conversation_id)
        return created
    finally:
        deps.release_conversation(request, body.conversation_id)


@router.post("/edit", status_code=202, response_model=ChatCreated)
async def edit(body: EditCreate, request: Request) -> ChatCreated:
    """Re-ask a changed request: branch from the edited turn's parent and run the new prompt,
    recording a new version of the turn and a fresh answer beside the original.

    The turn's command and `@` references carry forward while the new text still names
    them. The inline edit box is a plain textarea with no picker, so a client can't re-send
    either, and dropping them would turn a typo fix in `/reviewer chekc …` into an ordinary
    message. As in the composer, the text is the turn of record: a stamp survives only
    while the prompt carries its token, and deleting `/name` means they no longer meant it."""
    if not body.prompt.strip() and not body.attachment_ids:
        raise HTTPException(status_code=422, detail="prompt must not be empty")
    await _validate_attachments(request, body.attachment_ids)
    store = deps.store(request)
    if not await store.exists(body.conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    await _after_restore(request)
    # Claimed as in regenerate, before the model resolve and `edit_point`.
    deps.claim_conversation(request, body.conversation_id)
    try:
        models = await thread_models(request, body.conversation_id, body.endpoint_id, body.model)
        # Before `edit_point`, which reseats the leaf to this request's parent.
        stamped = await store.turn_stamps(body.conversation_id, body.message_id)
        stamps = still_named(stamped, body.prompt)
        if body.restore_files is not None:
            _ = await rewind_files(
                request, body.conversation_id, body.message_id, body.restore_files.preview_token
            )
        if not await store.edit_point(body.conversation_id, body.message_id):
            raise HTTPException(status_code=404, detail="message not found")
        created = await submit_turn(
            request,
            prompt=body.prompt,
            conversation_id=body.conversation_id,
            models=models,
            attachment_ids=body.attachment_ids,
            file_refs=stamps.file_refs,
            command=stamps.command,
        )
        _resumed(request, body.conversation_id)
        return created
    finally:
        deps.release_conversation(request, body.conversation_id)
