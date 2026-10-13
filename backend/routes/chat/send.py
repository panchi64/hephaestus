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
from routes.chat.schemas import ChatCreate, ChatCreated, EditCreate, RegenerateCreate
from routes.chat.submit import resolve_models, submit_turn, thread_agent, thread_models
from routes.deps import OPERATOR_ID
from runs import CHAT_TURN_KINDS, ConversationBusyError, RunRegistry
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


def _enqueue_steering(
    registry: RunRegistry, conversation_id: str, body: ChatCreate
) -> ChatCreated | None:
    """Queue a mid-run message into the conversation's live chat run, or None when
    the busy conversation can't take one (the claim is held by a regenerate/edit
    with no run registered yet, the run isn't a chat turn, or the send carries
    attachments or a command — steering is text-only). Synchronous end to end: no
    ``await`` between finding the run and enqueueing, so the run can't reach terminal
    (or drain) in between.

    **A command is refused rather than queued**, and that is not an oversight. A queued
    message is injected verbatim: there is no prelude around it to resolve a command
    through, so `/deploy prod` would reach the model as the literal token. Expanding
    before enqueueing is the alternative and is worse — the operator can edit a queued
    message (`PATCH /runs/{id}/messages/{mid}`), and they would then be editing text
    they never wrote. The 409 sends it down the ordinary path instead, where the whole
    composition exists.

    "Is a chat turn" is membership in the composed kinds, not equality with ``chat``: a
    scheduled task's run and a sub-agent's or woken turn both drive a conversation
    the operator can open and both drain the queued-message inbox, so both take steering.
    Only a run some other orchestrator submitted — which would never read the queue — is
    refused."""
    if body.attachment_ids or body.command or body.file_refs:
        return None
    run = registry.active_run_for(conversation_id, OPERATOR_ID)
    if run is None or run.kind not in CHAT_TURN_KINDS:
        return None
    message = run.enqueue_message(body.prompt)
    return ChatCreated(
        run_id=run.id,
        conversation_id=conversation_id,
        queued_message_id=message.id,
    )


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
    await store.clear_blocked_reason(conversation_id, body.continues_message_id)


async def _validate_new_thread_binding(request: Request, body: ChatCreate) -> None:
    """Reject a thread filed under a project that does not exist, before anything is
    created: 404 rather than a silent unfiled thread, which would put the work somewhere
    the operator will never look for it."""
    if body.project_id is None:
        return
    try:
        await deps.projects(request).get(OPERATOR_ID, body.project_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="project not found") from None


@router.post("", status_code=202, response_model=ChatCreated)
async def create_chat(body: ChatCreate, request: Request) -> ChatCreated:
    # A turn needs *something* to act on: text, or at least one attached file ("here,
    # look at this").
    if not body.prompt.strip() and not body.attachment_ids:
        raise HTTPException(status_code=422, detail="prompt must not be empty")
    await _validate_attachments(request, body.attachment_ids)
    if body.conversation_id is None:
        await _validate_new_thread_binding(request, body)

    store = deps.store(request)
    agent_name = None if body.agent == DEFAULT_AGENT else body.agent
    if body.conversation_id is not None:
        # Continue an existing conversation, but only one the operator owns —
        # an unknown id must not silently spawn orphan messages.
        if not await store.exists(body.conversation_id, OPERATOR_ID):
            raise HTTPException(status_code=404, detail="conversation not found")
        binding = await store.binding(body.conversation_id)
        if body.agent is not None:
            binding = replace(binding, agent_name=agent_name)
    else:
        # A thread files itself under whatever the request scope resolved: the explicit
        # `project_id` when the client sent one, else the operator's active project, else
        # unfiled. Filed under a project, it works in that project's folder; a throwaway
        # compare pane stays in a sandbox of its own.
        project_id = body.project_id or await deps.active_project(request)
        binding = ConversationBinding(
            workspace_kind="checkout" if project_id and not body.ephemeral else "sandbox",
            project_id=project_id,
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
            workspace_kind=binding.workspace_kind,
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
    # A busy conversation is not always a rejection: when the claim is held by a live
    # *chat* run (not a regenerate/edit claim) and the send is plain text, the message
    # queues into that run for injection at its next model-request boundary. The
    # check-and-enqueue is synchronous, so the run can't go terminal in between; and a
    # run that went terminal just before the POST simply lets the claim succeed — the
    # client never has to pick between "queue" and "new turn".
    registry = deps.registry(request)
    try:
        registry.claim(conversation_id, OPERATOR_ID)
    except ConversationBusyError:
        queued = _enqueue_steering(registry, conversation_id, body)
        if queued is not None:
            await _retire_stop_marker(store, conversation_id, body)
            return queued
        raise HTTPException(status_code=409, detail=deps.CONVERSATION_BUSY_DETAIL) from None
    try:
        # The one binding fact a later turn may change, written now that this request owns
        # the thread and *before* `submit_turn` reads the binding — so this send runs at
        # the level it asked for, and the next one inherits it. Deliberately after the
        # claim: a send that is rejected as busy, or diverted into the live run as
        # steering, never gets a turn of its own to run at the new level, and moving the
        # stored one anyway would leave the operator's control reading a level nothing is
        # actually running at.
        if body.conversation_id is not None and body.permission_level is not None:
            await store.set_permission_level(conversation_id, body.permission_level)
        if body.conversation_id is not None and body.agent is not None:
            await store.set_agent_name(conversation_id, agent_name)
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
        # Only once the run is submitted — a rejected send leaves the operator with the
        # same unfinished turn, so it must leave them the marker too. The claim above is
        # not enough to know that: `submit` has its own atomic check-and-claim, and a
        # race it catches becomes a 409 from in here.
        await _retire_stop_marker(store, conversation_id, body)
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
        if not await store.regenerate_point(body.conversation_id, body.message_id):
            raise HTTPException(status_code=404, detail="message not found")
        return await submit_turn(
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
        if not await store.edit_point(body.conversation_id, body.message_id):
            raise HTTPException(status_code=404, detail="message not found")
        return await submit_turn(
            request,
            prompt=body.prompt,
            conversation_id=body.conversation_id,
            models=models,
            attachment_ids=body.attachment_ids,
            file_refs=stamps.file_refs,
            command=stamps.command,
        )
    finally:
        deps.release_conversation(request, body.conversation_id)
