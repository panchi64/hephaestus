"""Conversations surface — browse, read, rename, and delete chat threads.

Thin pass-throughs to the :class:`ConversationStore`. Creating a conversation is
a chat concern (``POST /chat`` does it as a side effect of starting a turn); this
router only reads and manages the threads that already exist. History is returned
as a render-ready projection — the durable record stays full-fidelity
``ModelMessage`` blobs; the frontend never sees those.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agent import AwaitingKind, awaiting_kind
from agent.engine import fold_now
from agent.naming import late_title_wait_s
from agent.summarize import FoldFailed, NothingToFold, resolve_auto_compact_policy
from agent.title import title_from_history
from core.compaction_sections import SummarySection
from core.config import get_settings
from core.envelopes import ContentPart
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationSummary(BaseModel):
    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int
    preview: str | None = None
    # Written by the deferred background sweep once the thread has been idle; shown in the
    # re-entry band. Null until a thread has earned one. It rides the listing beside
    # `preview` (same class and size of payload; a per-row fetch costs more), and has no SSE
    # event because the band only shows on a quiet thread, so the next listing read is soon enough.
    work_summary: str | None = None
    model: str | None = None  # the model the conversation last ran on
    # The live run's `RunStatus` value (`running`, `queued`, `awaiting_input`); None when
    # idle. Registry-derived, not persisted. Lets the list mark working threads and keep the
    # rail's busy-vs-needs-you distinction (`awaiting_input` is parked on an approval).
    activity: str | None = None
    # What an `awaiting_input` thread is parked on; None for any other activity.
    awaiting_kind: AwaitingKind | None = None
    # How the most recent finished run ended (`done`, `error`, `blocked`, `cancelled`).
    # Separate from `activity` because both can hold (running now, errored last time); the
    # live value wins on screen and this is the row's resting state, so a failed thread
    # differs from one that never ran. Registry-derived and best-effort: the registry is
    # in-memory and bounded, so null means "nothing known", never "nothing happened".
    last_outcome: str | None = None
    # The agent the thread runs, by name. Null is the default agent.
    agent_name: str | None = None
    # The thread that spawned this one, for an agent's thread the operator opened.
    parent_id: str | None = None
    # Only the basename of the project directory, never the path: the rail shows it
    # permanently and a full path would expose the operator's clients. Null for an unfiled
    # thread or a deleted project.
    workspace: str | None = None
    # The rail joins on this id, not the basename: it lists directories with no threads, and
    # two can share a basename, which would merge two repositories into one section.
    project_id: str | None = None


class ToolCallImageOut(BaseModel):
    """An image the call handed back, base64 without scheme; mirrors ``tool.completed``'s images."""

    media_type: str
    data: str


class ToolCallAnswerOut(BaseModel):
    """One ``ask_user`` question and its answer; mirrors ``question.answered``'s items."""

    question: str
    selections: list[str] = Field(default_factory=list)
    text: str | None = None


class ToolCallOut(BaseModel):
    id: str
    name: str
    args: dict[str, object]
    status: str
    result: object = None
    result_parts: list[ContentPart] | None = None
    error: str | None = None
    images: list[ToolCallImageOut] = Field(default_factory=list)
    answers: list[ToolCallAnswerOut] = Field(default_factory=list)
    # The `run_code` call that made this one, as on the live `tool.*` frames.
    parent_tool_call_id: str | None = None
    # Same as `tool.failed`'s: the failure was the platform's own fault.
    platform: bool = False


class ViewVersionRefOut(BaseModel):
    """An inline View chip re-attached to the message whose ``view`` tool result embeds its
    version id. It only labels and opens the version; the files come from the snapshot."""

    snapshot_id: str
    title: str | None
    preview_kind: str | None  # "html" | "image" | "text" | "other" | None — the chip icon
    # The ``show`` call that minted it, so a reload places the chip right after that call.
    tool_call_id: str | None = None


class ViewSnapshotRefOut(BaseModel):
    """A workspace snapshot for the conversation's View, mirroring the live
    ``view.snapshot`` event. Conversation-scoped, not tied to one message."""

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
    # The reporting agent's handle, on a sub-agent's report.
    reporter: str | None = None
    # `role == "notice"` only — the notice's kind; `content` is its body.
    notice: str | None = None
    # User turns whose text carried untrusted fences — those fences in order; `content`
    # keeps the surrounding text. None elsewhere.
    parts: list[ContentPart] | None = None
    tools: list[ToolCallOut] = []
    versions: list[ViewVersionRefOut] = []
    created_at: datetime | None = None
    model: str | None = None  # the model that produced this assistant turn
    # Position among this turn's siblings; version_count > 1 ⇒ the operator can cycle them.
    version_index: int = 0
    version_count: int = 1
    pinned: bool = False  # the operator's durable bookmark on this turn
    # Upload ids the operator attached to this (user) turn — rendered as file chips.
    attachment_ids: list[str] = []
    # Workspace-relative paths named with `@` on this (user) turn.
    file_refs: list[str] = []
    # The reason an assistant turn's run ended `outcome: "blocked"` (usage/loop/context/time
    # bound), rendered as a persistent stop marker. None otherwise.
    blocked_reason: str | None = None
    # `role == "compaction"` only (0 elsewhere): what the fold cost, for the divider. Token
    # figures are the same coarse text-only proxy as the live `conversation.compacted`
    # event; render them as approximate.
    messages_compacted: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    # `role == "compaction"` only: the trigger (`threshold`/`overflow`/`manual`), matching
    # the live event's `reason`. None elsewhere and on checkpoints folded before it was recorded.
    compaction_reason: str | None = None
    # `role == "compaction"` only: the summary split by the same `summary_sections` the live
    # event uses, without fence markers or the model-facing preamble. Empty elsewhere and
    # when the text parses into nothing (the client falls back to `content`).
    sections: list[SummarySection] = Field(default_factory=list)
    # `role == "compaction"` only: what the fold re-read after the briefing, as on the live
    # event. Empty elsewhere.
    restored: list[str] = Field(default_factory=list)
    # Assistant turns only: the order the model emitted reasoning, calls and answer, which
    # `tools`/`content` flatten into lanes. Empty elsewhere.
    segments: list[SegmentOut] = Field(default_factory=list)


class ActiveRun(BaseModel):
    """The in-flight run driving this conversation, when one exists.

    A streaming answer isn't persisted until its turn ends, so a cold read mid-stream shows
    no answer; this points the client at the run to replay and resume from ``last_seq``.
    ``kind`` because a hand-started fold is a run too, and a client reattaching to one must
    not seed an assistant turn. ``prompt_message_id`` is the persisted row the run answers
    (the prompt, or on a regenerate the re-answered request); the streaming answer belongs
    directly after it. ``None`` for a fold, and briefly for a run still queued or in setup."""

    id: str
    kind: str
    status: str
    last_seq: int
    prompt_message_id: str | None = None


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut]
    # The thread's permission level, so a reload restores the control as left. On the detail
    # only; the sidebar row doesn't need it. A thread predating the column reads as the
    # level it effectively ran at.
    permission_level: str = DEFAULT_PERMISSION
    # Workspace snapshots captured per file-changing turn, newest last; the frontend merges
    # them into the View timeline beside the per-message versions.
    snapshots: list[ViewSnapshotRefOut] = []
    # Context-window state rebuilt from the last turn's stored usage. Null when usage or a
    # window is unavailable.
    context: ContextWindow | None = None
    # The cumulative readout rebuilt from stored messages, in the same `RunMetrics` shape
    # the run stream emits (as `context` shares `ContextWindow`) so the client has one
    # mapper. Null for a thread that has never run.
    stats: RunMetrics | None = None
    # Present only while a turn is streaming server-side, so a client can reattach.
    active_run: ActiveRun | None = None


class TitleUpdate(BaseModel):
    title: str | None = None


class RetitleRequest(BaseModel):
    """The picker selection to name the thread with.

    No per-turn endpoint is persisted, so a manual re-title resolves the title model as a
    chat turn does, through the current pick, since a picker-driven operator may never have
    bound the default ``main``/``utility`` roles. Both optional; absent falls back to them."""

    endpoint_id: str | None = None
    model: str | None = None


class CompactRequest(RetitleRequest):
    """The picker selection to fold with, and what the operator wants the briefing to keep."""

    focus: str | None = Field(default=None, max_length=2000)


class VersionSwitch(BaseModel):
    index: int  # which sibling version to make active (0-based)


class PinUpdate(BaseModel):
    pinned: bool


def _live_run(request: Request, conversation_id: str) -> Run | None:
    """The live run driving ``conversation_id``, or None when idle.

    An in-flight turn isn't in the store until it finishes, so only the registry can tell.
    """
    return deps.registry(request).active_run_for(conversation_id, OPERATOR_ID)


def _outcomes(request: Request) -> dict[str, str]:
    """conversation id → how its most recent finished run ended.

    Built in one pass for a whole listing, since the rail re-reads it on a timer and the
    registry holds every run, not one thread's. Terminal runs only: a live run is
    `activity`'s business, and a thread with one in flight still reports the previous one.
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
    """When a run stopped, else when it was created, so it still sorts."""
    return run.ended_at or run.created_at


def _summary(
    view: ConversationSummaryView,
    run: Run | None = None,
    workspaces: Mapping[str, str] | None = None,
    last_outcome: str | None = None,
) -> ConversationSummary:
    """One listing row. ``run`` is the live run, if any; ``workspaces`` maps project id →
    directory basename, none ⇒ no workspace."""
    return ConversationSummary(
        id=view.id,
        title=view.title,
        created_at=view.created_at,
        updated_at=view.updated_at,
        message_count=view.message_count,
        preview=view.preview,
        work_summary=view.work_summary,
        model=view.model,
        activity=run.status.value if run is not None else None,
        awaiting_kind=awaiting_kind(run) if run is not None else None,
        last_outcome=last_outcome,
        agent_name=view.agent_name,
        parent_id=view.parent_id,
        workspace=(workspaces or {}).get(view.project_id or ""),
        project_id=view.project_id,
    )


def message_versions(view: MessageView, by_id: dict[str, SnapshotView]) -> list[ViewVersionRefOut]:
    """The View versions this turn minted, recovered from its ``view`` tool results.

    Only static-preview versions get an inline chip (live/auto ones have a LIVE chip), so a
    cold read attaches the same chips the warm stream did."""
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


def message_out(view: MessageView, by_id: dict[str, SnapshotView]) -> MessageOut:
    return MessageOut(
        id=view.id,
        role=view.role,
        content=view.content,
        author=view.author,
        reporter=view.reporter,
        notice=view.notice,
        parts=view.parts,
        tools=[
            ToolCallOut(
                id=t.id,
                name=t.name,
                args=t.args,
                status=t.status,
                result=t.result,
                result_parts=t.result_parts,
                error=t.error,
                images=[ToolCallImageOut(media_type=i.media_type, data=i.data) for i in t.images],
                answers=[
                    ToolCallAnswerOut(question=a.question, selections=a.selections, text=a.text)
                    for a in t.answers
                ],
                parent_tool_call_id=t.parent_tool_call_id,
                platform=t.platform,
            )
            for t in view.tools
        ],
        versions=message_versions(view, by_id),
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
        restored=view.restored,
        segments=[
            SegmentOut(kind=s.kind, text=s.text, tool_call_id=s.tool_call_id) for s in view.segments
        ],
    )


async def _detail(
    request: Request, conversation_id: str, summary: ConversationSummaryView
) -> ConversationDetail:
    """A conversation's render-ready detail; shared by the read and navigation endpoints."""
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
    # Wall-clock comes from stored per-response timings, which the messages don't carry. A
    # thread with no responses sends null rather than a row of zeroes.
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
    snapshots = await deps.workspace_history(request).list(OPERATOR_ID, conversation_id)
    by_id = {s.id: s for s in snapshots}
    base: dict[str, object] = _summary(
        summary,
        run=run,
        workspaces=await deps.projects(request).workspace_names(OPERATOR_ID),
        last_outcome=_outcomes(request).get(conversation_id),
    ).model_dump()
    return ConversationDetail.model_validate(
        {
            **base,
            "messages": [message_out(m, by_id) for m in messages],
            "snapshots": [
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
            "context": context,
            "stats": stats,
            "active_run": active_run,
            # Resolved through the registry, so a row from an older build or with an unknown
            # value opens at the least-permissive level.
            "permission_level": (await store.binding(conversation_id)).permission,
        }
    )


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(request: Request) -> list[ConversationSummary]:
    views = await deps.store(request).list_conversations(
        OPERATOR_ID, visible_projects=await deps.project_scope(request)
    )
    # Both resolved once per listing: the rail refreshes on a timer, and a per-row decrypt
    # would repeat the same few directories.
    workspaces = await deps.projects(request).workspace_names(OPERATOR_ID)
    outcomes = _outcomes(request)
    return [
        _summary(
            v,
            run=_live_run(request, v.id),
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
        run=_live_run(request, conversation_id),
        last_outcome=_outcomes(request).get(conversation_id),
    )


class TitleOut(BaseModel):
    title: str | None


@router.get("/{conversation_id}/title", response_model=TitleOut)
async def await_auto_title(conversation_id: str, request: Request) -> TitleOut:
    """The thread's title, once an auto-title still generating after the run ended settles.

    The run stream is closed by then, so a client holds this request open instead of polling."""
    await deps.registry(request).settle_afterwork(conversation_id, OPERATOR_ID, late_title_wait_s())
    summary = await _require_owned(request, conversation_id)
    return TitleOut(title=summary.title)


@router.post("/{conversation_id}/retitle", response_model=ConversationSummary)
async def retitle_conversation(
    conversation_id: str, request: Request, body: RetitleRequest | None = None
) -> ConversationSummary:
    """Regenerate a thread's title from every operator question, overwriting unconditionally.

    Only operator turns are fed in, never assistant or tool output, keeping the title model
    off injectable content. The model resolves as a chat turn's background work does
    (``utility`` → the picked ``main``) with reasoning off; the wider ``retitle_*`` budgets
    leave room for a ``<think>`` block from runtimes that ignore the lever."""
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
        run=_live_run(request, conversation_id),
        last_outcome=_outcomes(request).get(conversation_id),
    )


async def _require_owned(request: Request, conversation_id: str) -> ConversationSummaryView:
    summary = await deps.store(request).get_summary(conversation_id, OPERATOR_ID)
    if summary is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return summary


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    request: Request,
    discard_branch: Annotated[bool, Query(alias="discardBranch")] = False,
) -> None:
    """Delete a conversation, with the image uploads it leaves unreferenced.

    A worktree thread with unmerged commits is refused (409) unless ``discardBranch=true``:
    destroying a branch must be as deliberate as merging one.
    """
    await deps.after_restore(request)
    try:
        _ = await deps.conversation_teardown(request).delete(
            OPERATOR_ID, conversation_id, discard_branch=discard_branch
        )
    except ConversationBusyError as exc:
        raise HTTPException(status_code=409, detail=deps.CONVERSATION_BUSY_DETAIL) from exc


@router.delete("/{conversation_id}/messages/{message_id}", response_model=ConversationDetail)
async def delete_message(
    conversation_id: str,
    message_id: str,
    request: Request,
) -> ConversationDetail:
    """Remove a turn and its subtree on every branch, with the image uploads it leaves
    unreferenced. The active path falls back to the turn's parent. Returns the resulting
    thread so the client reseats in one round-trip."""
    store = deps.store(request)
    teardown = deps.conversation_teardown(request)
    summary = await _require_owned(request, conversation_id)
    await deps.after_restore(request)
    # Claimed before the orphan lookup so a /chat submission can't register a run between
    # the lookup and the tree mutation; released before the purge, which doesn't move the leaf.
    deps.claim_conversation(request, conversation_id)
    try:
        orphans = await teardown.message_image_orphans(OPERATOR_ID, conversation_id, message_id)
        if not await store.delete_message(conversation_id, message_id):
            raise HTTPException(status_code=404, detail="message not found")
    finally:
        deps.release_conversation(request, conversation_id)
    await teardown.purge_uploads(OPERATOR_ID, orphans)
    return await _detail(request, conversation_id, summary)


@router.post("/{conversation_id}/messages/{message_id}/version", response_model=ConversationDetail)
async def switch_version(
    conversation_id: str, message_id: str, body: VersionSwitch, request: Request
) -> ConversationDetail:
    """Cycle a turn to one of its sibling versions (a prior regeneration/edit) and
    return the resulting thread."""
    store = deps.store(request)
    summary = await _require_owned(request, conversation_id)
    await deps.after_restore(request)
    # Moves the active leaf, so it must not race an in-flight turn or another claim.
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
    _ = await _require_owned(request, conversation_id)
    if not await store.set_pin(conversation_id, message_id, body.pinned):
        raise HTTPException(status_code=404, detail="message not found")


@router.post("/{conversation_id}/messages/{message_id}/rewind", response_model=ConversationDetail)
async def rewind(conversation_id: str, message_id: str, request: Request) -> ConversationDetail:
    """Move the active tip back to this turn so the thread ends there; the next
    message branches from it (the later turns stay reachable as a sibling version)."""
    store = deps.store(request)
    summary = await _require_owned(request, conversation_id)
    await deps.after_restore(request)
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

    Leaves the source untouched and returns the new conversation. The source is claimed
    during the copy: a run appending mid-walk would fork a history that never existed.
    """
    _ = await _require_owned(request, conversation_id)
    await deps.after_restore(request)
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
    """Give a forked code thread a branch cut from the source's, not the project's `base_ref`.

    The copied transcript describes the source branch's files, so branching from `base_ref`
    would give a tree that doesn't match it. Best-effort: with no source branch, the fork
    cuts its own on its first code turn.
    """
    store = deps.store(request)
    source = await store.binding(source_id)
    if not source.worktree or not source.project_id:
        return
    _ = await store.open_worktree(forked_id)
    binding = await store.binding(forked_id)
    if not binding.project_id:
        return
    try:
        project = await deps.projects(request).get(OPERATOR_ID, binding.project_id)
        await deps.worktrees(request).branch_from(
            Path(project.root_path), source_id=source_id, conversation_id=forked_id
        )
    except Exception:  # no source branch yet; the fork cuts its own later
        logger.debug("fork %s: no source branch to base on", forked_id, exc_info=True)


class ApprovalGrantOut(BaseModel):
    """A live conversation-scoped tool auto-approval grant, for the operator's
    visible + revocable list."""

    tool_name: str
    #: The leading words the grant covers (`["uv", "run", "pytest"]`); empty for a whole-tool
    #: grant. Kept as words, not a joined string, because it identifies the grant to the
    #: `DELETE` below and splitting a joined form is only unambiguous by a distant invariant.
    command_prefix: list[str] = []
    #: The operator's wider pick for the whole tool, which the reviewing level takes as
    #: authorization rather than weighing. Needed because an empty `command_prefix` also
    #: fits an ordinary non-command grant and a scheduled task's seed.
    decisive: bool = False
    #: Whether the tool's grants are command-scoped at all, so the chip says "any command"
    #: only where that is a real contrast.
    command_scoped: bool = False
    expires_at: datetime


@router.get("/{conversation_id}/grants", response_model=list[ApprovalGrantOut])
async def list_grants(conversation_id: str, request: Request) -> list[ApprovalGrantOut]:
    """What the operator allowed to auto-approve for the rest of this conversation."""
    _ = await _require_owned(request, conversation_id)
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
    """The agent's task list; the backfill for ``tasks.updated``, which a reload can't replay."""
    _ = await _require_owned(request, conversation_id)
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
    """The written plan, or ``null``; the backfill for ``plan.updated``.

    ``null`` rather than 404 because having no plan is the ordinary state.
    """
    _ = await _require_owned(request, conversation_id)
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

    ``command_prefix`` picks which grant on a command-running tool (it can hold several),
    repeated once per word exactly as the listing returned it. Absent revokes the whole-tool
    grant; a scope matching nothing is a no-op.
    """
    _ = await _require_owned(request, conversation_id)
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
    """A hand-started fold, accepted; the same fields ``POST /chat`` answers with."""

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
    _ = await _require_owned(request, conversation_id)
    return await _compaction_state(request, conversation_id)


@router.put("/{conversation_id}/auto-compact", response_model=CompactionOverrideOut)
async def set_auto_compact_override(
    conversation_id: str, body: CompactionOverrideUpdate, request: Request
) -> CompactionOverrideOut:
    """Force conversation compaction on/off for this thread, or clear it (``null``) to
    inherit the operator's global setting."""
    _ = await _require_owned(request, conversation_id)
    await deps.store(request).set_compaction_override(conversation_id, body.override)
    return await _compaction_state(request, conversation_id)


_NOTHING_TO_FOLD = "Nothing to fold — there are no new turns since the last compaction."


async def _run_manual_fold(run: Run, request: Request, pick: CompactRequest) -> None:
    """The operator's own fold, as a run.

    It is a run because :func:`agent.folding.fold` announces itself by emitting onto a
    run's stream. The threshold and on/off switch are ignored (the operator asked), and
    everything downstream is the automatic path's. Failure is reported, not swallowed: an
    asked-for button that fails like success is worse than one that doesn't work.

    The summary is written by the thread's own main model (the picker's selection, resolved
    as a chat turn does) on the agent a turn would run, never the utility model, so the
    model that served the last turn serves it from the prefix it holds."""
    conversation_id = run.conversation_id
    assert conversation_id is not None  # set by the submit below
    try:
        model, _, _, context_window, vision, _ = await thread_models(
            request, conversation_id, pick.endpoint_id, pick.model
        )
    except HTTPException as exc:
        # The resolve raises HTTP errors for its route callers; here the response has gone,
        # so the reason becomes the run's stop detail.
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
        focus=pick.focus,
    )
    if isinstance(result, NothingToFold):
        run.block(_NOTHING_TO_FOLD)
    elif isinstance(result, FoldFailed):
        run.block(result.message)


@router.post("/{conversation_id}/compact", response_model=FoldStarted, status_code=202)
async def compact_conversation_now(
    conversation_id: str, request: Request, body: CompactRequest | None = None
) -> FoldStarted:
    """Fold this thread's older turns into a summary now, before the automatic threshold.

    Answered ``202`` with a run id like ``POST /chat``; the run's stream carries the frames
    an automatic fold emits.

    The claim is still taken: ``submit`` refuses only a live run, while a claim also covers
    a regenerate or rewind mid-request, and a fold appends to the tree. It is released once
    the run exists, when ``submit``'s guard takes over.

    Why a fold declined is on the run's terminal ``detail``; ``404`` means only no such thread."""
    store = deps.store(request)
    if await store.get_summary(conversation_id, OPERATOR_ID) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    pick = body or CompactRequest()
    await deps.after_restore(request)

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
