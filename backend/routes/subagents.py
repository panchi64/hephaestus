"""The sub-agents a thread has launched — what the panel draws its cards from.

One read, and deliberately only one. Everything else a card needs is already a route:
a sub-agent's transcript is ``GET /conversations/{child_id}/messages`` because a sub-agent
*is* a conversation, its live tail is ``GET /runs/{run_id}/events`` because it is a run,
its approval is ``POST /runs/{run_id}/approve``, and stopping it is
``POST /runs/{run_id}/cancel``. Adding sub-agent-shaped copies of any of those would be a
second surface over the same objects, and the second one is the one that goes stale.

**Why a list rather than a feed.** The obvious shape is a stream — but the thing being
watched outlives every stream there is to hang it on. A sub-agent's own run stream ends
when *it* does, and the launching thread's ends when its turn does, which is usually long
before. A feed would therefore have to be a third stream that fans the others in, kept per
conversation for as long as anyone might look, holding subscriptions to runs nobody is
reading. This list, re-read while anything is live, is the same information without that
machine — and it is also the only shape that survives a restart, which is precisely when
the operator most wants to know what happened.

**And why each card carries its sub-agent's task list.** A sub-agent writes tasks for
itself in its own thread, through the same task tool an ordinary turn uses, and those are
the most legible account of what a long-running one is actually *doing* — a card showing
"running" and a context ring says only that it has not stopped. Reaching them needs no new
machine either: they are already ``GET /conversations/{child_id}/tasks``.

What a route of their own would cost is a request per card, on every poll, for as long as
anything is live — a thread with five sub-agents out turning one poll into six. And a
stream of their own would cost exactly what the section above says a feed costs, for a
smaller prize. The argument there is that the thing being watched outlives every stream
there is to hang it on; the same is true of these, and they change *more* often than a
card's status does, not less. So they ride the read that is already happening at the
cadence they need, which is the same reasoning that put the list here rather than on a
surface of its own.

Mounted under ``/conversations`` rather than at a surface of its own, because that is what
this is: a property of a thread, reached with the same grant that reads the thread.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes import deps
from routes.conversations import TaskOut
from routes.deps import OPERATOR_ID
from services.subagents import SubagentLauncher, SubagentView
from services.subagents.report import ReportStructure, report_prose, report_structure
from services.task_list import tasks_payload

router = APIRouter(prefix="/conversations", tags=["subagents"])


class Subagent(BaseModel):
    """One card. Named for what the operator sees rather than for the row behind it."""

    id: str
    #: The sub-agent's own thread, which is where its transcript is read from.
    conversation_id: str
    #: Its run — live tail, approvals, and cancelling, all of which are run routes.
    run_id: str
    #: Which sub-agent this is — the roster spec it was launched from.
    name: str
    #: What the launching agent called this one, and the only name it knows it by. Unique
    #: within the thread, which ``name`` is not: three ``explorer`` cards are three handles.
    handle: str
    task: str
    #: ``running`` | ``blocked`` | ``done`` | ``failed`` | ``cancelled``. The first two are
    #: live; a blocked one is waiting on the operator rather than on the machine.
    status: str
    #: What it reported, once it has. While it is still going this is its latest answer
    #: instead — a current best, explicitly not a report. This is the **prose**: the
    #: machine-readable block a reporting sub-agent ends with is taken out and served as
    #: `findings` below, because a card ending in forty lines of JSON is a card nobody
    #: reads to the end.
    summary: str | None = None
    #: The same report as data, when the sub-agent emitted one and it parsed — findings
    #: with their sources, conflicts with their sides, and per-topic coverage with its
    #: gaps. Null for every sub-agent whose job is not to report, and for one whose block
    #: was missing or unreadable: the structure is additive, and its absence costs a panel
    #: rather than a report.
    findings: ReportStructure | None = None
    error: str | None = None
    #: How full its context window is. Both are null until its run has made a request, and
    #: a finished sub-agent keeps the last figures it had.
    context_used: int | None = None
    context_window: int | None = None
    #: The thread that spawned it — the viewed thread, or another card's conversation.
    parent_conversation_id: str
    #: How far below the tree's root it runs; the root's own agents are 1.
    depth: int
    #: Whether the operator opened it as a thread of its own.
    opened: bool = False
    #: The tasks the sub-agent wrote for *itself*, in its own thread — the same list and the
    #: same shape ``GET /conversations/{id}/tasks`` serves, because it is that list. Empty
    #: for one that never wrote any, which most short sub-agents do not.
    tasks: list[TaskOut] = []
    started_at: datetime
    ended_at: datetime | None = None


class SubagentList(BaseModel):
    subagents: list[Subagent]


def _card(view: SubagentView, tasks: list[TaskOut], opened: bool) -> Subagent:
    # Split once, here, rather than letting the client do it: which part of a report is
    # prose and which is structure is a fact about the format, and a frontend deriving it
    # would be a frontend deciding. `report_structure` never raises, so an unreadable
    # block degrades to the prose alone.
    assert view.started_at is not None
    report = view.summary
    structure = report_structure(report) if report else None
    return Subagent(
        id=view.subagent_id,
        conversation_id=view.conversation_id,
        run_id=view.run_id,
        name=view.name,
        handle=view.handle,
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
    """Every agent below this thread, at any depth, newest first — live ones and finished.

    Finished ones are kept for the whole life of the thread rather than dropped once their
    report has landed: the operator's reason for looking is usually that something went
    wrong, and that is exactly the case where the card has already stopped being live.

    Each card carries its sub-agent's own task list, read here rather than fetched per card
    by the client — see this module's header for why that rides this read instead of
    getting a route or a stream of its own. Gathered rather than awaited in sequence: this
    is the poll the panel repeats for as long as anything is live, and a thread with five
    sub-agents out would otherwise pay five round trips of database latency in series for a
    frame that has to land inside one polling interval.
    """
    launcher = request.app.state.capabilities.get_optional(SubagentLauncher)
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


class OpenedThread(BaseModel):
    id: str
    parent_id: str


@router.post("/{conversation_id}/open", response_model=OpenedThread)
async def open_thread(conversation_id: str, request: Request) -> OpenedThread:
    """List a spawned agent's thread in the sidebar under the thread that spawned it, so the
    operator can read and message it directly. Its finished turns still report to its
    parent."""
    parent_id = await deps.store(request).open_child(conversation_id, OPERATOR_ID)
    return OpenedThread(id=conversation_id, parent_id=parent_id)


async def _tasks_of(request: Request, view: SubagentView) -> list[TaskOut]:
    """One sub-agent's own task list, in the shape the tasks route already serves.

    Through ``tasks_payload`` rather than built from the items directly, because the client
    renders these with the same component it renders the thread's own list with — and two
    shapes for one list is how that component grows a second branch that only one of them
    ever exercises.
    """
    items = await deps.conversation_tasks(request).items(OPERATOR_ID, view.conversation_id)
    return [TaskOut(**row) for row in tasks_payload(items)]
