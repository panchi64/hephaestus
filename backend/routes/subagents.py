"""The sub-agents a thread has launched: what the panel draws its cards from.

One read, deliberately. Everything else a card needs is already a route: the transcript is
``GET /conversations/{child_id}/messages`` (a sub-agent is a conversation), the live tail
``GET /runs/{run_id}/events``, approval ``POST /runs/{run_id}/approve``, and stopping
``POST /runs/{run_id}/cancel``. Copies of those would be a second surface that goes stale.

A list rather than a feed: the watched thing outlives every stream to hang it on (a
sub-agent's run stream ends with it, the launching thread's with its turn, usually earlier),
so a feed would be a third stream fanning in the others per conversation, holding
subscriptions to runs nobody reads. This list, re-read while anything is live, carries the
same information and survives a restart, when the operator most wants it.

Each card carries its sub-agent's own task list, the most legible account of what a
long-running one is doing (``GET /conversations/{child_id}/tasks``). A route of their own
would cost a request per card per poll, and a stream would face the same lifetime problem
while changing more often than status, so they ride this read at the cadence they need.

Mounted under ``/conversations`` because it is a property of a thread, reached with the
grant that reads the thread.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes import deps
from routes.conversations import TaskOut
from routes.deps import OPERATOR_ID
from services.subagents import SubagentLauncher, SubagentView, SubtreeStopper
from services.subagents.callsigns import class_display
from services.subagents.report import ReportStructure, report_prose, report_structure
from services.task_list import tasks_payload

router = APIRouter(prefix="/conversations", tags=["subagents"])


class Subagent(BaseModel):
    """One card, named for what the operator sees rather than the row behind it."""

    id: str
    #: The sub-agent's own thread, where its transcript is read.
    conversation_id: str
    #: Its run, for live tail, approvals and cancelling (all run routes).
    run_id: str
    #: The roster spec it was launched from.
    name: str
    #: What the launching agent called it, the only name it knows it by. Unique within the
    #: thread, unlike ``name``: three ``explorer`` cards are three handles.
    handle: str
    #: The operator's name for it (``BRAX``) and its class's display name (``Tripodes``).
    #: Both empty on a sub-agent launched before classes existed.
    callsign: str
    agent_class: str
    task: str
    #: ``running`` | ``blocked`` | ``done`` | ``failed`` | ``cancelled``. The first two are
    #: live; blocked waits on the operator.
    status: str
    #: Its report once filed; while it runs, its latest answer (a current best, not a
    #: report). This is the prose: the machine-readable block is served as `findings`,
    #: since a card ending in forty lines of JSON goes unread.
    summary: str | None = None
    #: The report as data (findings with sources, conflicts with sides, per-topic coverage
    #: and gaps) when emitted and parsed. Null for non-reporting sub-agents and for a
    #: missing or unreadable block: it is additive, and its absence costs a panel, not a report.
    findings: ReportStructure | None = None
    error: str | None = None
    #: Context fullness; null until its run makes a request, and a finished sub-agent keeps
    #: its last figures.
    context_used: int | None = None
    context_window: int | None = None
    #: The thread that spawned it: the viewed thread, or another card's conversation.
    parent_conversation_id: str
    #: How far below the tree's root it runs; the root's own agents are 1.
    depth: int
    #: Whether the operator opened it as a thread of its own.
    opened: bool = False
    #: The tasks it wrote for itself in its own thread, in the shape
    #: ``GET /conversations/{id}/tasks`` serves. Empty for most short sub-agents.
    tasks: list[TaskOut] = []
    started_at: datetime
    ended_at: datetime | None = None


class SubagentList(BaseModel):
    subagents: list[Subagent]


def _card(view: SubagentView, tasks: list[TaskOut], opened: bool) -> Subagent:
    # Split here, not by the client: what is prose and what is structure is a fact about the
    # format. `report_structure` never raises, so an unreadable block degrades to prose.
    assert view.started_at is not None
    report = view.summary
    structure = report_structure(report) if report else None
    return Subagent(
        id=view.subagent_id,
        conversation_id=view.conversation_id,
        run_id=view.run_id,
        name=view.name,
        handle=view.handle,
        callsign=view.callsign,
        agent_class=class_display(view.agent_class),
        task=view.task,
        parent_conversation_id=view.parent_conversation_id,
        depth=view.depth,
        opened=opened,
        tasks=tasks,
        status=view.status,
        summary=report_prose(report) if report else None,
        findings=structure,
        error=view.error,
        context_used=view.context_used,
        context_window=view.context_window,
        started_at=view.started_at,
        ended_at=view.ended_at,
    )


@router.get("/{conversation_id}/subagents", response_model=SubagentList)
async def list_subagents(conversation_id: str, request: Request) -> SubagentList:
    """Every agent below this thread at any depth, newest first, live and finished.

    Finished ones are kept for the thread's life: the operator usually looks because
    something went wrong, by which time the card is no longer live.

    Each card carries its task list, read here (see the module header). Gathered, not
    awaited in series: this is the panel's repeating poll, and five sub-agents would
    otherwise pay five serial database round trips inside one polling interval.
    """
    launcher = deps.capabilities(request).get_optional(SubagentLauncher)
    if launcher is None:
        raise HTTPException(status_code=404, detail="sub-agents are not available")
    views = await launcher.below(OPERATOR_ID, conversation_id)
    tasks = await asyncio.gather(*(_tasks_of(request, view) for view in views))
    store = deps.store(request)
    hidden = await asyncio.gather(*(store.is_hidden(view.conversation_id) for view in views))
    return SubagentList(
        subagents=[
            _card(view, rows, not is_hidden)
            for view, rows, is_hidden in zip(views, tasks, hidden, strict=True)
        ]
    )


class StoppedAgents(BaseModel):
    #: The handles whose runs were stopped, children and everything below them.
    stopped: list[str]


@router.post("/{conversation_id}/agents/stop", response_model=StoppedAgents)
async def stop_agents(conversation_id: str, request: Request) -> StoppedAgents:
    """Stop every live agent below a thread that has no run of its own to cancel.

    The Stop button on a running thread stops its agents through ``POST /runs/{id}/cancel``;
    this is the same stop for an idle one. The reports the agents file go to the held
    inbox, so nothing wakes the thread.
    """
    stopper = deps.capabilities(request).get_optional(SubtreeStopper)
    if stopper is None:
        raise HTTPException(status_code=404, detail="sub-agents are not available")
    if not await deps.store(request).exists(conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    await deps.after_restore(request)
    registry = deps.registry(request)
    if registry.active_run_for(conversation_id, OPERATOR_ID) is not None:
        raise HTTPException(
            status_code=409, detail="the thread has a run of its own; cancel that run instead"
        )
    inbox = registry.inbox(conversation_id, OPERATOR_ID)
    mark = inbox.stop()
    try:
        stopped = await stopper.stop_below(OPERATOR_ID, conversation_id)
    except BaseException:
        inbox.undo_stop(mark)
        raise
    if not stopped:
        inbox.undo_stop(mark)
    return StoppedAgents(stopped=stopped)


class OpenedThread(BaseModel):
    id: str
    parent_id: str


@router.post("/{conversation_id}/open", response_model=OpenedThread)
async def open_thread(conversation_id: str, request: Request) -> OpenedThread:
    """List a spawned agent's thread in the sidebar under its spawner so the operator can read
    and message it directly. Its finished turns still report to its parent."""
    parent_id = await deps.store(request).open_child(conversation_id, OPERATOR_ID)
    return OpenedThread(id=conversation_id, parent_id=parent_id)


async def _tasks_of(request: Request, view: SubagentView) -> list[TaskOut]:
    """One sub-agent's task list in the tasks route's shape, via ``tasks_payload``: the client
    renders both with one component, and two shapes would add a branch only one exercises."""
    items = await deps.conversation_tasks(request).items(OPERATOR_ID, view.conversation_id)
    return [TaskOut(**row) for row in tasks_payload(items)]
