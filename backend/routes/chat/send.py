"""The turn-starting routes: send a message, regenerate an answer, edit a request.

All three create a Run and hand the client its id (and the conversation id); the client
then streams it from ``/runs/{id}/events``. Regenerate and edit are the two history-rewriting
turns — they differ from a send only in how the conversation store repositions the active
leaf first; the launch is identical (``submit_turn``).
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


async def _validate_attachments(request: Request, attachment_ids: list[str]) -> None:
    """Every attached id must name an upload the operator owns — reject foreign/unknown
    ids with a clear 404 rather than silently dropping them at run time."""
    if not attachment_ids:
        return
    # Cheap ownership check — decrypts nothing (resolve_attachments opens the bytes/text
    # later, only for ids that survive to run time) — and one query for the whole list,
    # since a turn with several files would otherwise pay a thread hop per id.
    owned = await deps.uploads(request).owned_ids(OPERATOR_ID, attachment_ids)
    for upload_id in attachment_ids:
        if upload_id not in owned:
            raise HTTPException(status_code=404, detail=f"attachment {upload_id!r} not found")


def _enqueue(registry: RunRegistry, conversation_id: str, body: ChatCreate) -> ChatQueued:
    """Queue a send into a busy conversation's inbox. Never refused: a plain message joins
    the running turn at its next model request, and one carrying attachments, file
    references or a command waits to open a turn of its own, composed through the prelude
    like any send. Synchronous end to end, so nothing drains between the lookup and the
    enqueue.

    A command is queued unexpanded. The operator can edit a queued message, and expanding
    it now would have them editing text they never wrote. A turn item keeps the model,
    level and agent picked for it; a plain message joins a turn already running on its
    own."""
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
    """The operator started a turn, so whatever waited since they stopped one flows again.
    Called once the run is submitted and before it makes its first request, which then
    takes the inbox behind the operator's own message."""
    deps.registry(request).inbox(conversation_id, OPERATOR_ID).resume()


async def _retire_stop_marker(
    store: ConversationStore, conversation_id: str, body: ChatCreate
) -> None:
    """Clear the stop marker this turn resumes, if it says it resumes one.

    Best-effort, and deliberately loose about *which* marker: the store resolves an id
    it doesn't recognize to the newest stop on the thread, because a live client names
    a turn that stopped before anything persisted by its own optimistic id. The cost of
    that latitude is a stale client — one whose turn was since deleted or regenerated —
    retiring a newer stop instead of the one it meant. That is a marker the operator
    loses, never a turn: the resume they asked for still runs either way."""
    if body.continues_message_id is None:
        return
    _ = await store.clear_blocked_reason(conversation_id, body.continues_message_id)


async def _validate_new_thread_binding(request: Request, body: ChatCreate) -> None:
    """Reject a thread filed under a project that does not exist, before anything is
    created: 404 rather than a silent unfiled thread, which would put the work somewhere
    the operator will never look for it."""
    if body.project_id is None:
        return
    try:
        _ = await deps.projects(request).get(OPERATOR_ID, body.project_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="project not found") from None


@router.post("", status_code=202, response_model=ChatCreated | ChatQueued)
async def create_chat(body: ChatCreate, request: Request) -> ChatCreated | ChatQueued:
    # A turn needs *something* to act on: text, or at least one attached file ("here,
    # look at this").
    if not body.prompt.strip() and not body.attachment_ids:
        raise HTTPException(status_code=422, detail="prompt must not be empty")
    await _validate_attachments(request, body.attachment_ids)
    if body.conversation_id is None:
        await _validate_new_thread_binding(request, body)

    store = deps.store(request)
    agent_name = None if body.agent == DEFAULT_AGENT else body.agent
    stored: ConversationBinding | None = None
    if body.conversation_id is not None:
        # Continue an existing conversation, but only one the operator owns —
        # an unknown id must not silently spawn orphan messages.
        if not await store.exists(body.conversation_id, OPERATOR_ID):
            raise HTTPException(status_code=404, detail="conversation not found")
        binding = stored = await store.binding(body.conversation_id)
        if body.agent is not None:
            binding = replace(binding, agent_name=agent_name)
    else:
        # A thread files itself under whatever the request scope resolved: the explicit
        # `project_id` when the client sent one, else the operator's active project, else
        # unfiled. Filed under a project, it may write that project's folder; a throwaway
        # compare pane is filed under nothing, so it writes only its own scratch.
        project_id = body.project_id or await deps.active_project(request)
        binding = ConversationBinding(
            project_id=None if body.ephemeral else project_id,
            agent_name=agent_name,
        )
    applied = await thread_agent(request, binding, body.conversation_id)
    if agent_name is not None and applied.agent.name != agent_name:
        raise HTTPException(status_code=422, detail=f"no published agent named {agent_name!r}")

    # Resolve before creating/continuing — a model failure shouldn't leave an
    # empty orphan conversation behind.
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

    # Claim now, before the remaining awaits (the auto-compaction policy resolve) and
    # the eventual `submit` below — a plain "is there a live run" check alone leaves a
    # gap a concurrent regenerate/edit/delete could occupy without ever registering a
    # run for `active_run_for` to see. A brand-new conversation's id is unknown to
    # anyone else yet, so this always succeeds for it.
    #
    # A busy conversation never refuses a send: it queues into the inbox, and whatever
    # holds the thread (a turn, a fold, a regenerate about to submit) takes it next.
    registry = deps.registry(request)
    try:
        registry.claim(conversation_id, OPERATOR_ID)
    except ConversationBusyError:
        return await _queued_after_losing(request, registry, conversation_id, body)
    try:
        # The one binding fact a later turn may change, written now that this request owns
        # the thread and *before* `submit_turn` reads the binding — so this send runs at
        # the level it asked for, and the next one inherits it. After the claim on purpose:
        # a send that only queues gets no turn at the new level, and moving the stored one
        # would leave the control reading a level nothing runs at.
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
            # Another turn took the thread after the binding moved: put it back, since this
            # send only queues (a turn item carries its own picks to its turn).
            if stored is not None and body.permission_level is not None:
                _ = await store.set_permission_level(conversation_id, stored.permission)
            if stored is not None and body.agent is not None:
                await store.set_agent_name(conversation_id, stored.agent_name)
            return await _queued_after_losing(request, registry, conversation_id, body)
        # Only once the run is submitted — a rejected send leaves the operator with the
        # same unfinished turn, so it must leave them the marker too. The claim above is
        # not enough to know that: `submit` has its own atomic check-and-claim, and a
        # race it catches becomes a 409 from in here.
        await _retire_stop_marker(store, conversation_id, body)
        _resumed(request, conversation_id)
        return created
    finally:
        deps.release_conversation(request, conversation_id)


@router.post("/regenerate", status_code=202, response_model=ChatCreated)
async def regenerate(body: RegenerateCreate, request: Request) -> ChatCreated:
    """Re-answer a turn: drop back to the user request that produced ``message_id``
    and run again (no new prompt), recording the answer as a new version alongside
    the old one. An optional model override regenerates with a different model.

    A turn sent with a slash command is re-answered **with that command**, resolved again
    rather than replayed. The history a regenerate re-runs has only the operator's literal
    `/reviewer …` in it — the expansion was tail context and left with the turn — so
    without this the second answer would be to a different question than the first."""
    store = deps.store(request)
    if not await store.exists(body.conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    # Claim before the model resolve and the leaf-moving `regenerate_point` call —
    # both are real `await`s a second near-simultaneous regenerate/edit could
    # otherwise slip through during: neither request has registered a run yet at
    # that point, so a plain "is there a live run" check can't see the other one
    # coming. Released once this request's own submit/mutation is settled, win or
    # lose (a failed resolve, a 404 message id, or a successful submit).
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
            # Read *after* `regenerate_point`, which is what puts the turn being
            # re-answered at the active leaf — before it, this would report whichever
            # turn the thread happened to end on. The `@` references need no such
            # rescue: their marker persisted with the prompt and is already in the
            # history this replays.
            command=(await store.turn_stamps(body.conversation_id)).command,
        )
        _resumed(request, body.conversation_id)
        return created
    finally:
        deps.release_conversation(request, body.conversation_id)


@router.post("/edit", status_code=202, response_model=ChatCreated)
async def edit(body: EditCreate, request: Request) -> ChatCreated:
    """Re-ask a changed request: branch from the edited user turn's parent and run
    with the new prompt, recording a new version of that turn (and a fresh answer)
    beside the original.

    **The turn's command and its `@` references are carried forward, as far as the new text
    still names them.** The inline edit box is a plain textarea — there is no picker in it —
    so a client cannot re-send either, and dropping them would mean fixing a typo in
    `/reviewer chekc the auth route` silently turned it into an ordinary message. The rule
    applied is the composer's own: the **text is the turn of record**, so a stamp survives
    only while the edited prompt still carries its token. Deleting the `/name` is how the
    operator says they no longer meant it."""
    if not body.prompt.strip() and not body.attachment_ids:
        raise HTTPException(status_code=422, detail="prompt must not be empty")
    await _validate_attachments(request, body.attachment_ids)
    store = deps.store(request)
    if not await store.exists(body.conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    # Same claim as regenerate — before the model resolve and before the
    # leaf-moving `edit_point` call.
    deps.claim_conversation(request, body.conversation_id)
    try:
        models = await thread_models(request, body.conversation_id, body.endpoint_id, body.model)
        # Read before `edit_point`, which reseats the leaf to this request's *parent* —
        # after it, the trailing request is the previous turn.
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
