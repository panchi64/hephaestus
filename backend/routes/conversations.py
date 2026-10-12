"""Conversations surface — browse, read, rename, and delete chat threads.

Thin pass-throughs to the :class:`ConversationStore`. Creating a conversation is
a chat concern (``POST /chat`` does it as a side effect of starting a turn); this
router only reads and manages the threads that already exist. History is returned
as a render-ready projection — the durable record stays full-fidelity
``ModelMessage`` blobs; the frontend never sees those.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agent.engine import fold_now
from agent.summarize import FoldFailed, NothingToFold, resolve_auto_compact_policy
from agent.title import title_from_history
from core.compaction_sections import SummarySection
from core.config import get_settings
from core.exceptions import DegradedCapabilityError, NotFoundError
from routes import deps
from routes.chat import thread_models, turn_scope
from routes.deps import OPERATOR_ID
from runs import ContextWindow, ConversationBusyError, Run, RunMetrics
from services.approval_grants import COMMAND_SCOPED_TOOLS
from services.context_meter import read_context_meter
from services.conversation_view import MessageView, SegmentKind
from services.conversations import (
    ConversationSummaryView,
    conversation_totals,
    last_request_usage,
)
from services.permissions import DEFAULT_PERMISSION
from services.settings_store import (
    get_auto_compact,
    get_context_thresholds,
    resolve_compaction_enabled,
)
from services.task_list import tasks_payload
from services.workspace_history import SnapshotView, snapshot_id_from_result
from services.workspace_kind import DEFAULT_WORKSPACE_KIND, WorkspaceKind

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationSummary(BaseModel):
    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int
    preview: str | None = None
    # Two or three sentences on what the agent did in this thread, written by the deferred
    # background sweep once the thread has been idle a while and shown in the re-entry band
    # when the operator comes back to it. Null until a thread has earned one.
    #
    # It rides the **listing** payload beside `preview`, deliberately: the band is drawn
    # over a row the session list already loaded, and a short summary is the same class and
    # size of payload as the excerpt already sitting next to it — a per-row fetch would be
    # the same bytes over one round trip each. There is no SSE event for it either, for the
    # matching reason: the band only shows on a thread that has gone quiet, so the next
    # listing read is soon enough and a live push would arrive at a client with nothing to
    # put it on.
    work_summary: str | None = None
    model: str | None = None  # the model the conversation last ran on
    # The live run driving this thread, as its `RunStatus` value (`running`,
    # `queued`, `awaiting_input`); None when nothing is in flight. Derived from the
    # run registry rather than persisted — it lets the thread list mark which
    # conversations are working without opening each one, and keeps the
    # busy-vs-needs-you distinction the nav rail already draws (an `awaiting_input`
    # run is parked on the operator's approval decision, not merely streaming).
    activity: str | None = None
    # How the thread's most recent *finished* run ended, as its terminal `RunStatus`
    # value (`done`, `error`, `blocked`, `cancelled`); None when nothing has finished
    # that this process still remembers. `activity` covers only the three live statuses,
    # so without this a thread that failed and a thread that never ran are the same row:
    # both quiet, both null, and the failure is only discoverable by opening it.
    #
    # A sibling rather than a widening of `activity`, because they answer different
    # questions and can both be true — a thread can be running *now* having errored last
    # time. The live value is the one that wins on screen; this is what the row falls
    # back to at rest.
    #
    # Registry-derived like `activity`, and therefore **best-effort**: the run registry
    # is in memory and bounded, so a restart, or enough traffic to age the run out,
    # leaves this null. Null means "nothing known", never "nothing happened".
    last_outcome: str | None = None
    # Where its file work happens: "sandbox", "checkout" or "worktree". On the listing so
    # the rail can mark worktree threads without opening each one.
    workspace_kind: WorkspaceKind = DEFAULT_WORKSPACE_KIND
    # The agent the thread runs, by name. Null is the default agent.
    agent_name: str | None = None
    # The thread that spawned this one, for an agent's thread the operator opened.
    parent_id: str | None = None
    # The **basename** of the project directory a thread is filed under, and nothing else
    # about it. Null for an unfiled thread, and for one whose project has since been
    # deleted. Deliberately not the path: the rail groups project threads under this, it is
    # permanently on screen, and a full path across it would spell out the operator's
    # clients to anyone standing behind them.
    workspace: str | None = None
    # The project the workspace above belongs to. On the wire beside the name because the
    # rail *joins* on it: it lists every directory the operator works in, not only the
    # ones that already hold threads, and two directories can share a basename. Joining on
    # the name instead would merge two repositories into one section and point that
    # section's controls at whichever won. An opaque id names nothing — it is the path
    # that stays behind.
    project_id: str | None = None


class ToolCallImageOut(BaseModel):
    """An image the call handed back — base64, scheme added by the renderer. The wire
    twin of the live stream's ``tool.completed`` images, so a screenshot renders in the
    work log identically whether the operator watched it happen or reloaded into it."""

    media_type: str
    data: str


class ToolCallAnswerOut(BaseModel):
    """One question an ``ask_user`` call asked, and what the operator said to it. The wire
    twin of the live stream's ``question.answered`` items, so the card renders identically
    whether the operator watched themselves answer or reloaded into it."""

    question: str
    selections: list[str] = Field(default_factory=list)
    text: str | None = None


class ToolCallOut(BaseModel):
    id: str
    name: str
    args: dict[str, Any]
    status: str
    result: Any = None
    error: str | None = None
    images: list[ToolCallImageOut] = Field(default_factory=list)
    answers: list[ToolCallAnswerOut] = Field(default_factory=list)
    # The `run_code` call this one was made from, when a script made it — the same field
    # the live `tool.*` frames carry, so a reload nests the row where the stream did.
    parent_tool_call_id: str | None = None


class ViewVersionRefOut(BaseModel):
    """An inline View **chip** re-attached to the message that minted it: a ``show``
    produced a version mid-turn, recoverable from the message's ``view`` tool result
    (which embeds the version id). The chip just labels + opens the version — its bytes
    and files come from the conversation-scoped snapshot the panel reads."""

    snapshot_id: str
    title: str | None
    preview_kind: str | None  # "html" | "image" | "text" | "other" | None — the chip icon
    # The ``show`` call that minted it, so a reload can place the chip right after that
    # call rather than at the end of the turn.
    tool_call_id: str | None = None


class ViewSnapshotRefOut(BaseModel):
    """A View **version** (workspace snapshot) for the conversation's View, mirroring
    the live ``view.snapshot`` event so a cold read rebuilds the timeline like a warm
    one. Conversation-scoped (it captures the whole workspace), not tied to one
    message; carries how it previews."""

    snapshot_id: str
    title: str | None
    created_at: datetime
    files_changed: int
    summary: str
    preview_kind: str | None
    preview_artifact_id: str | None
    keeper: bool = False  # the operator's durable bookmark on this version


class SegmentOut(BaseModel):
    """One step of an assistant turn: a passage (``thinking``/``text``) with its text,
    or a ``tool`` call naming the ``tools`` entry it refers to."""

    kind: SegmentKind
    text: str = ""
    tool_call_id: str | None = None


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    # The sending agent's handle, on a message one agent sent another.
    author: str | None = None
    tools: list[ToolCallOut] = []
    versions: list[ViewVersionRefOut] = []
    created_at: datetime | None = None
    model: str | None = None  # the model that produced this assistant turn
    # Version navigation: position among this turn's siblings and how many exist.
    # version_count > 1 ⇒ the operator can cycle ‹ k/n › between regenerations/edits.
    version_index: int = 0
    version_count: int = 1
    pinned: bool = False  # the operator's durable bookmark on this turn
    # Upload ids the operator attached to this (user) turn — rendered as file chips.
    attachment_ids: list[str] = []
    # Workspace-relative paths the operator named with `@` on this (user) turn — chips
    # beside the attachments. Empty everywhere else.
    file_refs: list[str] = []
    # Set when the run behind this assistant turn ended `outcome: "blocked"` (a
    # usage/loop/context/time bound) — the human-readable reason, rendered as a
    # persistent stop marker. None for every other turn.
    blocked_reason: str | None = None
    # `role == "compaction"` only — what the fold cost, so the divider can read
    # "14 messages folded, ~62k → ~4k" with no client-side counting or estimating.
    # The token figures are the same coarse text-only proxy the live
    # `conversation.compacted` event carries (render them as approximate). 0 on every
    # other role.
    messages_compacted: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    # `role == "compaction"` only — what triggered the fold (`threshold`/`overflow`/
    # `manual`), matching the live `conversation.compacted` event's `reason`, so a divider
    # says the same thing after a reload as it did when the operator watched it appear.
    # `None` on every other role, and on a checkpoint folded before the reason was
    # recorded — the divider states it as an extra segment and reads correctly without it.
    compaction_reason: str | None = None
    # `role == "compaction"` only — the summary split into the sections the divider
    # renders, parsed by the same `summary_sections` the live event uses so a reload draws
    # the divider the operator watched arrive. Carries no fence markers and no model-facing
    # preamble: those are addressed to the model, not to whoever is reading the thread.
    # Empty on every other role, and on a checkpoint whose text parses into nothing —
    # the client falls back to `content` then.
    sections: list[SummarySection] = Field(default_factory=list)
    # Assistant turns only — the order the model emitted its reasoning, calls and answer
    # in, which `tools`/`content` flatten into lanes. A reload walks this so the
    # transcript reads in the order the operator watched it stream. Empty elsewhere.
    segments: list[SegmentOut] = Field(default_factory=list)


class ActiveRun(BaseModel):
    """The in-flight run driving this conversation, when one exists. A streaming
    answer isn't persisted until its turn finishes, so on a cold read (e.g. a page reload
    mid-stream) the messages alone show no answer — this points the client at the
    run whose events it can replay and resume from ``last_seq``.

    ``kind`` because not every run is an answer being written: a hand-started fold is a
    run too, and a client reattaching to one must not seed an assistant turn for it.

    ``prompt_message_id`` names the persisted row the run is answering — the operator's
    prompt, which is written down as the turn begins, or on a regenerate the request being
    re-answered. The streaming answer belongs directly after that row, so a reattaching
    client places it there instead of inventing a prompt. ``None`` for a run that answers
    nothing (a fold), and briefly for a chat run still queued or in its setup."""

    id: str
    kind: str
    status: str
    last_seq: int
    prompt_message_id: str | None = None


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut]
    # How far the model may go in this thread, so a reload restores the control at the
    # level the operator left it rather than at the default. On the detail and not the
    # listing: it is a fact about the thread you have open, and the sidebar row has no
    # use for it. Always populated — a thread that predates the column reads as the
    # level it was effectively running at.
    permission_level: str = DEFAULT_PERMISSION
    # The View's git-style history — workspace snapshots captured per file-changing
    # turn, newest last. Conversation-scoped; the frontend merges them into the View
    # timeline alongside the per-message versions.
    snapshots: list[ViewSnapshotRefOut] = []
    # The context-window state reconstructed from the last turn's stored usage, so
    # an existing thread shows its fullness on load — not just after the next turn.
    # Null when usage or a window is unavailable.
    context: ContextWindow | None = None
    # The thread's cumulative readout — turns, steps, tokens, cache, wall-clock —
    # rebuilt from the stored messages so the line under the composer says the same
    # thing on a cold load as it did live. Deliberately the **same** `RunMetrics` shape
    # the run stream emits, for the reason `context` above shares `ContextWindow`: one
    # shape from either source means the client has one mapper and the two can't drift.
    # Null for a thread that has never run.
    stats: RunMetrics | None = None
    # Present only while a turn is still streaming server-side — lets a reattaching
    # client resume the live run instead of rendering a reply-less thread.
    active_run: ActiveRun | None = None


class TitleUpdate(BaseModel):
    title: str | None = None


class RetitleRequest(BaseModel):
    """The picker selection to name the thread with. The conversation does not
    persist a per-turn endpoint, so a manual re-title resolves the title model the
    same way a chat turn does — through the operator's current pick — rather than the
    bare default ``main``/``utility`` roles (which a picker-driven operator may never
    have bound). Both optional: an absent pick falls back to those defaults."""

    endpoint_id: str | None = None
    model: str | None = None


class VersionSwitch(BaseModel):
    index: int  # which sibling version to make active (0-based)


class PinUpdate(BaseModel):
    pinned: bool


def _activity(request: Request, conversation_id: str) -> str | None:
    """The status of the live run driving ``conversation_id``, or None when idle.

    Reuses `RunRegistry.active_run_for` so the "which run drives this conversation"
    rule (non-terminal, most recent) lives in exactly one place. Registry-derived,
    not persisted: an in-flight turn isn't written to the store until it finishes,
    so the conversation read alone can't tell a working thread from an idle one.
    """
    run = deps.registry(request).active_run_for(conversation_id, OPERATOR_ID)
    return run.status.value if run is not None else None


def _outcomes(request: Request) -> dict[str, str]:
    """conversation id → how its most recent finished run ended.

    Built in one pass over the registry and handed to a whole listing, rather than
    scanned per row: the rail re-reads this list on a timer while anything is running,
    and the registry holds every run this process remembers, not this thread's.

    Terminal runs only. A live run is `activity`'s business, and a conversation with one
    in flight still reports what the previous one did — the two fields are read together.
    """
    latest: dict[str, Run] = {}
    for run in deps.registry(request).list(OPERATOR_ID):
        if not run.is_terminal or run.conversation_id is None:
            continue
        previous = latest.get(run.conversation_id)
        if previous is None or _ended(run) > _ended(previous):
            latest[run.conversation_id] = run
    return {conversation_id: run.status.value for conversation_id, run in latest.items()}


def _ended(run: Run) -> datetime:
    """When a run stopped, falling back to when it started — a terminal run that
    somehow carries no end time still has to sort against its siblings."""
    return run.ended_at or run.created_at


def _summary(
    view: ConversationSummaryView,
    activity: str | None = None,
    workspaces: Mapping[str, str] | None = None,
    last_outcome: str | None = None,
) -> ConversationSummary:
    """One listing row. ``workspaces`` maps project id → directory basename; a caller
    with nothing to look up (a single-thread read, where the group heading is not being
    drawn) passes none and the row simply carries no workspace."""
    return ConversationSummary(
        id=view.id,
        title=view.title,
        created_at=view.created_at,
        updated_at=view.updated_at,
        message_count=view.message_count,
        preview=view.preview,
        work_summary=view.work_summary,
        model=view.model,
        activity=activity,
        last_outcome=last_outcome,
        workspace_kind=view.workspace_kind,
        agent_name=view.agent_name,
        parent_id=view.parent_id,
        workspace=(workspaces or {}).get(view.project_id or ""),
        project_id=view.project_id,
    )


def _message_versions(view: MessageView, by_id: dict[str, SnapshotView]) -> list[ViewVersionRefOut]:
    """The View versions this turn minted, recovered from its ``view`` tool results
    (each ``show(file=…)`` embeds the version id). Only a static-preview version folds an
    inline chip — a live/auto version is already marked by its LIVE chip, matching the
    warm stream — so the cold read attaches exactly the chips that warmly streamed."""
    refs: list[ViewVersionRefOut] = []
    for tool in view.tools:
        if not tool.name.endswith("view_show") or not isinstance(tool.result, str):
            continue
        snapshot_id = snapshot_id_from_result(tool.result)
        snapshot = by_id.get(snapshot_id) if snapshot_id else None
        if snapshot is not None and snapshot.preview_kind is not None:
            refs.append(
                ViewVersionRefOut(
                    snapshot_id=snapshot.id,
                    title=snapshot.title,
                    preview_kind=snapshot.preview_kind,
                    tool_call_id=tool.id,
                )
            )
    return refs


def _message(view: MessageView, by_id: dict[str, SnapshotView]) -> MessageOut:
    return MessageOut(
        id=view.id,
        role=view.role,
        content=view.content,
        author=view.author,
        tools=[
            ToolCallOut(
                id=t.id,
                name=t.name,
                args=t.args,
                status=t.status,
                result=t.result,
                error=t.error,
                images=[ToolCallImageOut(media_type=i.media_type, data=i.data) for i in t.images],
                answers=[
                    ToolCallAnswerOut(question=a.question, selections=a.selections, text=a.text)
                    for a in t.answers
                ],
                parent_tool_call_id=t.parent_tool_call_id,
            )
            for t in view.tools
        ],
        versions=_message_versions(view, by_id),
        created_at=view.timestamp,
        model=view.model,
        version_index=view.version_index,
        version_count=view.version_count,
        pinned=view.pinned,
        attachment_ids=view.attachment_ids,
        file_refs=view.file_refs,
        blocked_reason=view.blocked_reason,
        messages_compacted=view.messages_compacted,
        tokens_before=view.tokens_before,
        tokens_after=view.tokens_after,
        compaction_reason=view.compaction_reason,
        sections=view.sections,
        segments=[
            SegmentOut(kind=s.kind, text=s.text, tool_call_id=s.tool_call_id) for s in view.segments
        ],
    )


async def _detail(
    request: Request, conversation_id: str, summary: ConversationSummaryView
) -> ConversationDetail:
    """Assemble a conversation's full render-ready detail (active path + the View's
    static versions + reconstructed context-window state). Shared by the read
    endpoint and the navigation endpoints that return the post-move thread (version
    switch, rewind)."""
    store = deps.store(request)
    messages = await store.messages_view(conversation_id)
    history = await store.history(conversation_id)
    meter = await read_context_meter(
        conversation_id,
        owner_id=OPERATOR_ID,
        store=store,
        models=deps.models(request),
        settings_store=deps.settings_store(request),
    )
    used, window, context = meter.used, meter.window, meter.context
    # The same figures the live stream reports, rebuilt from the same messages by the
    # same function — the counts and tokens off the active path, the wall-clock off the
    # stored per-response timings (the one thing the messages don't carry). A thread
    # that has never produced a response has nothing to report and sends null, so the
    # readout stays absent rather than rendering a row of zeroes.
    totals = conversation_totals(history)
    stats: RunMetrics | None = None
    if totals.steps:
        timings = await store.timings(conversation_id)
        stats = RunMetrics(
            steps=totals.steps,
            tool_calls=totals.tool_calls,
            turns=totals.turns,
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            cache_read_tokens=totals.cache_read_tokens,
            llm_ms=timings.llm_ms or None,
            tool_ms=timings.tool_ms or None,
            ttft_ms_total=timings.ttft_ms_total or None,
            ttft_samples=timings.ttft_samples,
            context_window=window,
            context_used=used,
            last_request=last_request_usage(history),
        )
    run = deps.registry(request).active_run_for(conversation_id, OPERATOR_ID)
    active_run = (
        ActiveRun(
            id=run.id,
            kind=run.kind,
            status=run.status.value,
            last_seq=run.stream.last_seq,
            prompt_message_id=run.prompt_message_id,
        )
        if run is not None
        else None
    )
    # The conversation's versions, fetched once: the timeline (snapshots[]) and the
    # by-id map the cold-read uses to re-attach each turn's inline chips.
    snapshots = await deps.workspace_history(request).list(OPERATOR_ID, conversation_id)
    by_id = {s.id: s for s in snapshots}
    return ConversationDetail(
        **_summary(
            summary,
            activity=active_run.status if active_run else None,
            workspaces=await deps.projects(request).workspace_names(OPERATOR_ID),
            last_outcome=_outcomes(request).get(conversation_id),
        ).model_dump(),
        messages=[_message(m, by_id) for m in messages],
        snapshots=[
            ViewSnapshotRefOut(
                snapshot_id=s.id,
                title=s.title,
                created_at=s.created_at,
                files_changed=s.files_changed,
                summary=s.summary,
                preview_kind=s.preview_kind,
                preview_artifact_id=s.preview_artifact_id,
                keeper=s.keeper,
            )
            for s in snapshots
        ],
        context=context,
        stats=stats,
        active_run=active_run,
        # Off the thread's own binding, resolved through the registry — so a row written
        # by an older build, or carrying a value this one has no rule for, opens at the
        # level that does the least rather than at whatever the string happens to say.
        permission_level=(await store.binding(conversation_id)).permission,
    )


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(request: Request) -> list[ConversationSummary]:
    views = await deps.store(request).list_conversations(
        OPERATOR_ID, visible_projects=await deps.project_scope(request)
    )
    # Resolved once for the whole listing rather than per row: the rail refreshes on a
    # timer while anything is running, and a decrypt per thread would pay for the same
    # handful of directories over and over.
    workspaces = await deps.projects(request).workspace_names(OPERATOR_ID)
    # Same reasoning for the outcomes: one pass over the registry for the whole listing.
    outcomes = _outcomes(request)
    return [
        _summary(
            v,
            activity=_activity(request, v.id),
            workspaces=workspaces,
            last_outcome=outcomes.get(v.id),
        )
        for v in views
    ]


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: str, request: Request) -> ConversationDetail:
    summary = await deps.store(request).get_summary(conversation_id, OPERATOR_ID)
    if summary is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return await _detail(request, conversation_id, summary)


@router.patch("/{conversation_id}", response_model=ConversationSummary)
async def rename_conversation(
    conversation_id: str, body: TitleUpdate, request: Request
) -> ConversationSummary:
    store = deps.store(request)
    if await store.get_summary(conversation_id, OPERATOR_ID) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    await store.set_title(conversation_id, body.title)
    summary = await store.get_summary(conversation_id, OPERATOR_ID)
    if summary is None:  # pragma: no cover — just confirmed it exists
        raise HTTPException(status_code=404, detail="conversation not found")
    return _summary(
        summary,
        activity=_activity(request, conversation_id),
        last_outcome=_outcomes(request).get(conversation_id),
    )


@router.post("/{conversation_id}/retitle", response_model=ConversationSummary)
async def retitle_conversation(
    conversation_id: str, request: Request, body: RetitleRequest | None = None
) -> ConversationSummary:
    """Regenerate a thread's title on demand, from every question the operator asked
    across the whole conversation — not just its opening line (the first-turn
    auto-titler's input). A manual re-name exists to fix a title that the opening
    missed or that went stale as the thread drifted, so feeding the full arc is what
    makes it meaningful. Only the operator's turns are fed in, never assistant or
    tool output, keeping the small title model off injectable content. Unlike the
    fill-only-if-blank auto-titler, this overwrites unconditionally — it is a
    deliberate operator action.

    The title model is resolved exactly as a chat turn resolves its background work
    (``utility`` → the picked ``main``), requesting reasoning **off** — and it works
    for a picker-driven operator who has no default role bound. The full-arc input here
    is longer than the auto-titler's opening-message excerpt, so a model whose runtime
    ignores the reasoning-off lever (e.g. LM Studio + Qwen) produces a longer ``<think>``
    block; the wider ``retitle_max_tokens`` budget and ``retitle_timeout_s`` give it room
    to think *and* emit the title, which :func:`agent.title` then strips clean."""
    store = deps.store(request)
    if await store.get_summary(conversation_id, OPERATOR_ID) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    pick = body or RetitleRequest()
    try:
        title = await deps.models(request).resolve_background(
            owner_id=OPERATOR_ID,
            override_endpoint_id=pick.endpoint_id,
            override_model=pick.model,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="model endpoint not found") from None
    except DegradedCapabilityError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    name = await title_from_history(
        title.model,
        await store.history(conversation_id),
        full=True,
        reasoning_off=title.reasoning_off,
        timeout_s=get_settings().retitle_timeout_s,
        max_tokens=get_settings().retitle_max_tokens,
    )
    if name is None:
        raise HTTPException(status_code=503, detail="could not generate a title")
    await store.set_title(conversation_id, name)
    summary = await store.get_summary(conversation_id, OPERATOR_ID)
    if summary is None:  # pragma: no cover — just confirmed it exists
        raise HTTPException(status_code=404, detail="conversation not found")
    return _summary(
        summary,
        activity=_activity(request, conversation_id),
        last_outcome=_outcomes(request).get(conversation_id),
    )


class OrphanImageAttachments(BaseModel):
    """Image uploads that *this* delete would leave referenced by nothing surviving — the
    set the operator is asked to keep or purge. Empty ⇒ delete straight through (no prompt
    needed). Only images are listed; other attachments (e.g. PDFs) are never auto-purged."""

    upload_ids: list[str]


async def _require_owned(request: Request, conversation_id: str) -> ConversationSummaryView:
    summary = await deps.store(request).get_summary(conversation_id, OPERATOR_ID)
    if summary is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return summary


async def _image_orphans(
    request: Request, conversation_id: str, *, message_id: str | None
) -> list[str]:
    """The image uploads that the proposed delete would orphan — computed before the delete
    runs (the doomed turns must still be in the tree) and filtered to ``image/*``. The store's
    check spares any image still referenced by a surviving branch or another chat."""
    candidates = await deps.store(request).orphaned_attachments_for_delete(
        OPERATOR_ID, conversation_id, message_id=message_id
    )
    return await deps.uploads(request).image_ids(OPERATOR_ID, candidates)


#: How long a cancelled sub-agent is given to actually stop before its thread is deleted
#: anyway. Long enough for a cooperative cancel to reach the next step boundary, short
#: enough that a wedged child never holds up a delete the operator is waiting on.
_SUBAGENT_STOP_S = 5.0


async def _discard_subagents(request: Request, conversation_id: str) -> None:
    """Stop and forget every agent below a thread, at any depth, because the thread is going.

    Cancelling first is the half that is not bookkeeping: a sub-agent is a live Run, and
    one whose parent has been deleted goes on making model requests to produce a report
    that will find no thread to reach, while still counting against the operator's cap.
    Its own conversation goes with it — it is hidden from the session list, so the cards
    this delete is destroying were the only way in.

    Best-effort throughout, per sub-agent, for the reason every teardown below is: the
    conversation row is already gone, and a container that would not stop must not turn a
    delete the operator has already seen succeed into a 500.
    """
    records = getattr(request.app.state, "subagent_records", None)
    if records is None:
        return  # sub-agents are not wired in this deployment
    try:
        rows = await records.descendants(conversation_id, OPERATOR_ID)
    except Exception:  # noqa: BLE001 — best-effort; the DB delete already succeeded
        logger.warning("could not list sub-agents of %s", conversation_id, exc_info=True)
        return
    registry = deps.registry(request)
    store = deps.store(request)
    for row in rows:
        try:
            await registry.cancel(row.run_id)
            run = registry.get(row.run_id)
            if run is not None:
                # Before its thread is deleted, not after: a run finalising into a
                # conversation that has already gone re-creates a cache entry nothing
                # ever evicts — the same hazard the claim above protects the parent
                # from. Bounded, because "it would not stop" is not a reason to leave
                # the operator's delete hanging.
                await asyncio.wait_for(run.wait(), timeout=_SUBAGENT_STOP_S)
        except Exception:  # noqa: BLE001 — one stuck child mustn't strand its siblings
            logger.warning("could not stop sub-agent %s", row.id, exc_info=True)
    for row in rows:
        try:
            await store.delete_conversation(row.child_conversation_id)
        except Exception:  # noqa: BLE001 — one stuck child mustn't strand its siblings
            logger.warning("could not discard sub-agent %s", row.id, exc_info=True)
    try:
        await records.forget([row.id for row in rows])
    except Exception:  # noqa: BLE001 — best-effort; the DB delete already succeeded
        logger.warning("could not forget sub-agents of %s", conversation_id, exc_info=True)


async def _purge_uploads(request: Request, upload_ids: list[str]) -> None:
    """Hard-delete the chosen image uploads (bytes + corpus chunks cascade).
    Best-effort per id, run after the conversation/message is already deleted: one
    already gone (a race) or otherwise failing to delete is logged and skipped, so it never
    aborts the remaining purges or 500s a delete the operator already saw succeed."""
    uploads = deps.uploads(request)
    for upload_id in upload_ids:
        try:
            await uploads.delete(OPERATOR_ID, upload_id)
        except NotFoundError:
            pass
        except Exception:  # noqa: BLE001 — one bad purge mustn't strand the others
            logger.exception("failed to purge orphaned image upload %s", upload_id)


@router.get("/{conversation_id}/orphan-image-attachments", response_model=OrphanImageAttachments)
async def orphan_image_attachments(
    conversation_id: str,
    request: Request,
    message_id: str | None = Query(default=None, alias="messageId"),
) -> OrphanImageAttachments:
    """Pre-delete probe: which image attachments would lose their last reference if this
    message (``message_id``) or the whole conversation (omit it) were deleted. The frontend
    asks this first and only prompts keep-or-delete when the list is non-empty."""
    await _require_owned(request, conversation_id)
    upload_ids = await _image_orphans(request, conversation_id, message_id=message_id)
    return OrphanImageAttachments(upload_ids=upload_ids)


async def _settle_code_branch(request: Request, conversation_id: str, *, discard: bool) -> None:
    """Refuse to delete a worktree thread that still holds unmerged commits, or throw its
    branch away when the operator said to.

    Best-effort about *everything except the refusal*: a project that has since been
    deleted, or a repository that has moved, leaves nothing to protect and must not block
    the delete. Only a real, countable diff stops it.
    """
    binding = await deps.store(request).binding(conversation_id)
    if binding.workspace_kind != "worktree" or not binding.project_id:
        return
    try:
        project = await deps.projects(request).get(OPERATOR_ID, binding.project_id)
        root = Path(project.root_path)
        diff = await deps.worktrees(request).diff(
            root,
            base_ref=project.base_ref,
            conversation_id=conversation_id,
            project_id=project.id,
        )
    except Exception:  # noqa: BLE001 — no branch, no project, no repo: nothing to lose
        logger.debug("no code branch to settle for %s", conversation_id, exc_info=True)
        return
    if diff.files_changed and not discard:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This conversation has unmerged work on {diff.branch}: "
                f"{diff.files_changed} file(s), +{diff.insertions} −{diff.deletions}. "
                "Merge it first, or delete with discardBranch=true to throw it away."
            ),
        )
    await deps.worktrees(request).discard(
        root, project_id=binding.project_id, conversation_id=conversation_id
    )


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    request: Request,
    purge_images: bool = Query(default=False, alias="purgeImages"),
    discard_branch: bool = Query(default=False, alias="discardBranch"),
) -> None:
    """Delete a conversation. With ``purgeImages=true`` the operator chose to also delete
    the image attachments this would orphan; the default keeps them.

    A **worktree** thread with unmerged commits is refused (409) unless
    ``discardBranch=true``. Merging a branch is a deliberate act the operator has to take;
    destroying one must be at least as deliberate, and deleting the thread would otherwise
    be the quiet way to lose work that the merge gate exists to protect.
    """
    store = deps.store(request)
    # The purging delete `routes/deps.claim_conversation` names, and the one mutator here
    # that was not taking the claim. Deleting under a live run tears the tree, the sandbox
    # and the turn's attachments out from beneath it: the run keeps going, its own
    # `finalize` re-creates a ghost cache entry nothing ever evicts, and the turn is
    # discarded with no error. 409 instead, like every sibling mutation.
    deps.claim_conversation(request, conversation_id)
    try:
        if await store.get_summary(conversation_id, OPERATOR_ID) is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        await _settle_code_branch(request, conversation_id, discard=discard_branch)
        orphans = (
            await _image_orphans(request, conversation_id, message_id=None) if purge_images else []
        )
        await store.delete_conversation(conversation_id)
        await _purge_uploads(request, orphans)
        # Drop the conversation's View history (snapshots + any blob no other snapshot
        # needs), so the work doesn't linger encrypted on disk after the thread is gone.
        await deps.workspace_history(request).delete_for_conversation(OPERATOR_ID, conversation_id)
        # Same reasoning for the agent's task list and the plan it was working from: both
        # restate what was asked for, so neither must outlive the thread.
        await deps.conversation_tasks(request).delete_for_conversation(OPERATOR_ID, conversation_id)
        await deps.plan_mode(request).delete_for_conversation(OPERATOR_ID, conversation_id)
        # And the sub-agents it launched. Unlike everything else here they are not merely
        # stored state: a live one is a *running* model, spending the operator's money on
        # work for a thread that no longer exists and holding a slot against the cap, with
        # no card left anywhere to show it or stop it. Their own threads go too — hidden
        # ones, reachable only through the cards this delete just destroyed.
        await _discard_subagents(request, conversation_id)
        # Delete the conversation's sandbox too (its workspace + sealed archive),
        # otherwise it lingers on disk keyed to a thread that no longer exists. The DB
        # delete above is the authoritative action, so a purge failure must not fail it.
        sandbox = deps.sandbox_sessions(request)
        if sandbox is not None:
            try:
                await sandbox.purge(conversation_id)
            except Exception:  # noqa: BLE001 — best-effort; the DB delete already succeeded
                logger.warning("sandbox purge failed for %s", conversation_id, exc_info=True)
        # The domains the operator opened for this thread, and the allowlist file the
        # fences read them from. Same reasoning as the workspace above, and best-effort for
        # the same reason — but unconditional, because the policy exists whether or not a
        # container runtime does.
        try:
            await deps.egress(request).forget(conversation_id)
        except Exception:  # noqa: BLE001 — best-effort; the DB delete already succeeded
            logger.warning("egress purge failed for %s", conversation_id, exc_info=True)
        # And the browser: closing its window is the small half — the saved login is the
        # point. It holds the cookies for every site this thread signed into, so leaving it
        # behind would keep those sessions alive on disk after the thread is gone.
        browser = deps.browser_sessions(request)
        if browser is not None:
            try:
                await browser.purge(conversation_id)
            except Exception:  # noqa: BLE001 — best-effort; the DB delete already succeeded
                logger.warning("browser purge failed for %s", conversation_id, exc_info=True)
    finally:
        deps.release_conversation(request, conversation_id)


@router.delete("/{conversation_id}/messages/{message_id}", response_model=ConversationDetail)
async def delete_message(
    conversation_id: str,
    message_id: str,
    request: Request,
    purge_images: bool = Query(default=False, alias="purgeImages"),
) -> ConversationDetail:
    """Remove a turn and everything after it on every branch (its subtree). The
    active path falls back to the deleted turn's parent. With ``purgeImages=true`` the
    image attachments this orphans are deleted too (default keeps them). Returns the
    resulting thread so the client reseats in one round-trip (like version switch / rewind)."""
    store = deps.store(request)
    summary = await _require_owned(request, conversation_id)
    # Claim before the orphan-attachment lookup (real DB awaits, only when
    # purge_images) and the leaf-moving `delete_message` call — a concurrent /chat
    # submission could otherwise land and register its run mid-lookup (nothing has
    # mutated, and no run exists yet, until this call actually deletes), then this
    # delete would proceed to mutate the tree while that new run is live. Released as
    # soon as the tree mutation itself is done — the purge/detail below read but don't
    # move the leaf, so they don't need to stay inside the claim.
    deps.claim_conversation(request, conversation_id)
    try:
        orphans = (
            await _image_orphans(request, conversation_id, message_id=message_id)
            if purge_images
            else []
        )
        if not await store.delete_message(conversation_id, message_id):
            raise HTTPException(status_code=404, detail="message not found")
    finally:
        deps.release_conversation(request, conversation_id)
    await _purge_uploads(request, orphans)
    return await _detail(request, conversation_id, summary)


@router.post("/{conversation_id}/messages/{message_id}/version", response_model=ConversationDetail)
async def switch_version(
    conversation_id: str, message_id: str, body: VersionSwitch, request: Request
) -> ConversationDetail:
    """Cycle a turn to one of its sibling versions (a prior regeneration/edit) and
    return the resulting thread."""
    store = deps.store(request)
    summary = await _require_owned(request, conversation_id)
    # Moves the active leaf — must not race an in-flight turn built from the leaf
    # this would move away from (or another in-flight claim on this conversation).
    deps.claim_conversation(request, conversation_id)
    try:
        if not await store.switch_version(conversation_id, message_id, body.index):
            raise HTTPException(status_code=404, detail="version not found")
    finally:
        deps.release_conversation(request, conversation_id)
    return await _detail(request, conversation_id, summary)


@router.post("/{conversation_id}/messages/{message_id}/pin", status_code=204)
async def pin_message(
    conversation_id: str, message_id: str, body: PinUpdate, request: Request
) -> None:
    """Pin or unpin a turn — a durable bookmark surfaced in the projection."""
    store = deps.store(request)
    await _require_owned(request, conversation_id)
    # A tree mutation like the others here — rejected while a run is live on this
    # conversation, same as switch_version/rewind/delete_message.
    deps.claim_conversation(request, conversation_id)
    try:
        if not await store.set_pin(conversation_id, message_id, body.pinned):
            raise HTTPException(status_code=404, detail="message not found")
    finally:
        deps.release_conversation(request, conversation_id)


@router.post("/{conversation_id}/messages/{message_id}/rewind", response_model=ConversationDetail)
async def rewind(conversation_id: str, message_id: str, request: Request) -> ConversationDetail:
    """Move the active tip back to this turn so the thread ends there; the next
    message branches from it (the later turns stay reachable as a sibling version)."""
    store = deps.store(request)
    summary = await _require_owned(request, conversation_id)
    # Moves the active leaf — see switch_version's guard note above.
    deps.claim_conversation(request, conversation_id)
    try:
        if not await store.rewind(conversation_id, message_id):
            raise HTTPException(status_code=404, detail="message not found")
    finally:
        deps.release_conversation(request, conversation_id)
    return await _detail(request, conversation_id, summary)


@router.post("/{conversation_id}/messages/{message_id}/fork", response_model=ConversationDetail)
async def fork(conversation_id: str, message_id: str, request: Request) -> ConversationDetail:
    """Start a **new** conversation carrying this thread's history up to this turn.

    Unlike rewind/regenerate/edit — which move the active tip inside one thread's tree —
    this leaves the source thread untouched and returns the *new* conversation, so the
    client navigates to it in one round-trip.

    The source is claimed for the duration of the walk: a run appending to it mid-copy
    would produce a fork of a history that never existed.
    """
    await _require_owned(request, conversation_id)
    store = deps.store(request)
    deps.claim_conversation(request, conversation_id)
    try:
        forked_id = await store.fork(conversation_id, message_id, OPERATOR_ID)
    finally:
        deps.release_conversation(request, conversation_id)
    if forked_id is None:
        raise HTTPException(status_code=404, detail="message not found")
    await _branch_the_fork(request, conversation_id, forked_id)
    summary = await store.get_summary(forked_id, OPERATOR_ID)
    if summary is None:  # pragma: no cover — just created above
        raise HTTPException(status_code=404, detail="conversation not found")
    return await _detail(request, forked_id, summary)


async def _branch_the_fork(request: Request, source_id: str, forked_id: str) -> None:
    """Give a forked **code** thread a branch cut from the source's, not from the
    project's base ref.

    The copied transcript describes files as they are on the source conversation's branch.
    Branching the fork from `base_ref` — what a first code turn would otherwise do —
    would hand it a tree that does not match the history it was given, which is precisely
    what forking is supposed to preserve. Best-effort: a source that never cut a branch
    leaves the fork to create one normally on its first code turn.
    """
    binding = await deps.store(request).binding(forked_id)
    if binding.workspace_kind != "worktree" or not binding.project_id:
        return
    try:
        project = await deps.projects(request).get(OPERATOR_ID, binding.project_id)
        await deps.worktrees(request).branch_from(
            Path(project.root_path), source_id=source_id, conversation_id=forked_id
        )
    except Exception:  # noqa: BLE001 — no source branch yet; the fork cuts its own later
        logger.debug("fork %s: no source branch to base on", forked_id, exc_info=True)


class ApprovalGrantOut(BaseModel):
    """A live conversation-scoped tool auto-approval grant, for the operator's
    visible + revocable list."""

    tool_name: str
    #: The command this grant covers, as the leading words it was read as
    #: (`["uv", "run", "pytest"]`); empty when the grant covers the whole tool. The words,
    #: not a joined string: this is what identifies the grant to `DELETE` below, and a
    #: joined form has to be split again by whoever sends it back — which is only
    #: unambiguous while an invariant two layers down holds. Joining it for a label is the
    #: client's job, and a label is all a joined form is good for.
    command_prefix: list[str] = []
    #: Whether this is the operator's wider pick — their answer for the whole tool, which
    #: the reviewing level takes as the authorization rather than merely weighing. Sent
    #: because the chip has to be able to say which of the two it is: an empty
    #: `command_prefix` alone cannot, since it is also the shape a non-command tool's
    #: ordinary grant and a scheduled task's seed both take.
    decisive: bool = False
    #: Whether this tool is one whose grants are scoped to a command at all. Sent so the
    #: chip can say "any command" where that is a real contrast and stay silent where it
    #: is not: a tool that runs no command has only one width, and labelling its grant
    #: against commands it never runs would describe something that does not exist.
    command_scoped: bool = False
    expires_at: datetime


@router.get("/{conversation_id}/grants", response_model=list[ApprovalGrantOut])
async def list_grants(conversation_id: str, request: Request) -> list[ApprovalGrantOut]:
    """What the operator allowed to auto-approve for the rest of this conversation."""
    await _require_owned(request, conversation_id)
    grants = await deps.approval_grants(request).list(OPERATOR_ID, conversation_id)
    return [
        ApprovalGrantOut(
            tool_name=g.tool_name,
            command_prefix=list(g.command_prefix),
            decisive=g.decisive,
            command_scoped=g.tool_name in COMMAND_SCOPED_TOOLS,
            expires_at=g.expires_at,
        )
        for g in grants
    ]


class TaskOut(BaseModel):
    id: str
    content: str
    status: str
    active_form: str | None = None


@router.get("/{conversation_id}/tasks", response_model=list[TaskOut])
async def read_tasks(conversation_id: str, request: Request) -> list[TaskOut]:
    """The agent's current task list for this thread.

    The list is streamed as it changes (``tasks.updated``), but a client that opens or
    reloads a conversation has no stream to replay — this is how it starts from the
    truth rather than from an empty panel that only fills on the next mutation.
    """
    await _require_owned(request, conversation_id)
    items = await deps.conversation_tasks(request).items(OPERATOR_ID, conversation_id)
    return [TaskOut(**row) for row in tasks_payload(items)]


class PlanOut(BaseModel):
    """The plan this thread produced, if it has produced one."""

    title: str
    body: str
    steps: list[str]
    status: str
    revision: int


@router.get("/{conversation_id}/plan", response_model=PlanOut | None)
async def read_plan(conversation_id: str, request: Request) -> PlanOut | None:
    """The written plan for this thread, or ``null`` where there is none.

    The backfill half of ``plan.updated``, and the same reasoning as the task list above:
    a reload has no stream to replay, and a plan awaiting approval is the last thing that
    should disappear because the operator refreshed the page. ``null`` rather than a 404 —
    "this thread has no plan" is the ordinary state of nearly every thread, not a miss.
    """
    await _require_owned(request, conversation_id)
    plan = await deps.plan_mode(request).current(OPERATOR_ID, conversation_id)
    return PlanOut(**plan.payload()) if plan is not None else None


@router.delete("/{conversation_id}/grants/{tool_name}", status_code=204)
async def revoke_grant(
    conversation_id: str,
    tool_name: str,
    request: Request,
    command_prefix: Annotated[list[str] | None, Query()] = None,
) -> None:
    """Revoke one conversation auto-approval — the next call it covered asks again.

    ``command_prefix`` identifies *which* grant on that tool, since a command-running tool
    can hold several at once (`uv run pytest` and `git commit` are two separate standing
    yeses). It is repeated once per word — exactly the list the listing handed out, so what
    the operator sees and what this deletes cannot come apart. Absent revokes the whole-tool
    grant; a scope that matches nothing is a no-op, like revoking a grant that already
    lapsed.
    """
    await _require_owned(request, conversation_id)
    await deps.approval_grants(request).revoke(
        OPERATOR_ID, conversation_id, tool_name, tuple(command_prefix or ())
    )


class CompactionOverrideUpdate(BaseModel):
    """Set a conversation's compaction override: ``null`` inherits the operator's global
    setting; ``true``/``false`` forces compaction on/off for this thread."""

    override: bool | None = None


class CompactionOverrideOut(BaseModel):
    """The thread's compaction state: the stored ``override`` (``null`` = inherit) plus the
    ``effective`` on/off after resolving it against the operator's global setting — so the UI
    renders the real state without re-deriving it."""

    override: bool | None
    effective: bool


class FoldStarted(BaseModel):
    """A hand-started fold, accepted. Deliberately the same two fields ``POST /chat``
    answers with, because the client does the same thing with them: attach to
    ``/runs/{id}/events`` and render what arrives."""

    run_id: str
    conversation_id: str


async def _compaction_state(request: Request, conversation_id: str) -> CompactionOverrideOut:
    """This thread's auto-compaction state: the stored override plus the effective on/off
    after resolving it against the operator's global default."""
    override = await deps.store(request).get_compaction_override(conversation_id)
    global_cfg = await get_auto_compact(deps.settings_store(request), OPERATOR_ID)
    return CompactionOverrideOut(
        override=override,
        effective=resolve_compaction_enabled(override, global_cfg.enabled),
    )


@router.get("/{conversation_id}/auto-compact", response_model=CompactionOverrideOut)
async def get_auto_compact_override(
    conversation_id: str, request: Request
) -> CompactionOverrideOut:
    """This thread's conversation-compaction state (its override + the effective on/off) —
    whether its older turns fold into a summary once the context window fills."""
    await _require_owned(request, conversation_id)
    return await _compaction_state(request, conversation_id)


@router.put("/{conversation_id}/auto-compact", response_model=CompactionOverrideOut)
async def set_auto_compact_override(
    conversation_id: str, body: CompactionOverrideUpdate, request: Request
) -> CompactionOverrideOut:
    """Force conversation compaction on/off for this thread, or clear it (``null``) to
    inherit the operator's global setting."""
    await _require_owned(request, conversation_id)
    await deps.store(request).set_compaction_override(conversation_id, body.override)
    return await _compaction_state(request, conversation_id)


#: Why this conversation had nothing to fold. A fold takes everything since the newest
#: checkpoint, so the only cause is that nothing has been said since the thread began or
#: since its last fold.
_NOTHING_TO_FOLD = "Nothing to fold — there are no new turns since the last compaction."




async def _run_manual_fold(run: Run, request: Request, pick: RetitleRequest) -> None:
    """The operator's own fold, as a run.

    It is a run for one reason and it is not tidiness: :func:`agent.folding.fold` is where
    a fold announces itself, and announcing means emitting onto a run's stream. Without one
    this path had nowhere to emit, so it reached past ``fold`` to the summarizer directly —
    and an operator who pressed the button watched a spinner with nothing behind it for as
    long as the summary took to write.

    The threshold and the on/off switch are deliberately ignored, and that is the **only**
    thing this trigger does differently from the automatic one: the operator asked for it
    explicitly, so there is nothing left for a trigger to decide. Everything downstream —
    what gets folded, the model that writes it, the never-reach-past-an-earlier-checkpoint rule,
    the events, the checkpoint that gets written — is the shared path's.

    Failure is reported rather than swallowed, which is the other half of that split. An
    automatic fold that fails lets the turn carry on, because nobody asked for it; this one
    was asked for, and a button whose failure looks like success is worse than one that
    does not work.

    **The summary is written by the thread's own main model** — the picker's selection,
    resolved exactly as a chat turn resolves it — on the agent a turn would run, composed
    from the same :class:`~routes.chat.TurnScope`. Never the utility model: the request is
    the thread's replay plus one message, so the model that served the last turn serves it
    from the prefix it already holds."""
    conversation_id = run.conversation_id
    assert conversation_id is not None  # noqa: S101 — set by the submit below
    try:
        model, _, _, context_window, vision = await thread_models(
            request, conversation_id, pick.endpoint_id, pick.model
        )
    except HTTPException as exc:
        # The resolve answers in HTTP because its other callers are routes; here the
        # response has already gone, so the reason travels as the run's stop detail.
        run.block(str(exc.detail))
        return
    scope = await turn_scope(request, conversation_id, vision=vision)
    store = deps.store(request)
    settings_store = deps.settings_store(request)
    result = await fold_now(
        run,
        model=model,
        store=store,
        conversation_id=conversation_id,
        context_window=context_window,
        context_thresholds=await get_context_thresholds(settings_store, OPERATOR_ID),
        auto_compact=await resolve_auto_compact_policy(
            settings_store,
            OPERATOR_ID,
            override=await store.get_compaction_override(conversation_id),
        ),
        categories=scope.categories,
        dormant=scope.dormant,
        instruction_providers=scope.instruction_providers,
        capabilities=scope.capabilities,
        disabled_tools=scope.disabled_tools,
        binding=scope.binding,
    )
    if isinstance(result, NothingToFold):
        run.block(_NOTHING_TO_FOLD)
    elif isinstance(result, FoldFailed):
        run.block(result.message)


@router.post("/{conversation_id}/compact", response_model=FoldStarted, status_code=202)
async def compact_conversation_now(
    conversation_id: str, request: Request, body: RetitleRequest | None = None
) -> FoldStarted:
    """Fold this thread's older turns into a summary now, without waiting for it to reach
    the automatic threshold — for a thread the operator knows is about to need the room.

    **A fold is a run, and answering ``202`` with its id is the whole point**: the
    summarizer is a model call of its own, tens of seconds on a local endpoint, and a
    request that blocks for its duration has nowhere to put what is happening meanwhile.
    Attached to the run's stream the client gets exactly what an automatic fold has always
    produced — ``compaction.started``, the summary as it is written, then
    ``conversation.compacted`` — because it is now literally the same code path.

    Shaped like ``POST /chat`` for that reason, rather than like ``retitle`` (whose shape
    it used to follow).

    **It still takes the claim**, and the claim is not the same guarantee ``submit``
    gives: ``submit`` refuses a conversation with a live *run*, while a claim also covers a
    request that is between its check and its act — a regenerate or a rewind repositioning
    the leaf with awaits still ahead of it. A fold appends to the tree, so it is exactly
    the thing that must not interleave with one. The claim is released as soon as the run
    exists, which is the point at which ``submit``'s own guard takes over.

    Why the fold declined is on the run's terminal ``detail``, not on this response — it is
    not known yet when this returns. ``404`` here means only that there is no such thread."""
    store = deps.store(request)
    if await store.get_summary(conversation_id, OPERATOR_ID) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    pick = body or RetitleRequest()

    async def orchestrator(run: Run) -> None:
        await _run_manual_fold(run, request, pick)

    deps.claim_conversation(request, conversation_id)
    try:
        run = deps.registry(request).submit(
            kind="compaction",
            owner_id=OPERATOR_ID,
            orchestrator=orchestrator,
            conversation_id=conversation_id,
        )
    except ConversationBusyError as exc:  # pragma: no cover — the claim above catches it
        raise HTTPException(
            status_code=409,
            detail="A response is already in progress in this conversation.",
        ) from exc
    finally:
        deps.release_conversation(request, conversation_id)
    return FoldStarted(run_id=run.id, conversation_id=conversation_id)
