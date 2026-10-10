"""Agent working-state events: the task list, the written plan, and permission-level changes."""

from __future__ import annotations

from typing import Literal

from .base import _Body


class TasksUpdated(_Body):
    """The agent's task list changed. Carries the **whole** list, not a delta: the stream
    is replayable from any seq, so full state is idempotent on replay and needs no ordering
    rules, and the list is small enough that the bytes don't matter. Each item is
    ``{id, content, status, active_form}`` with status one of pending/in_progress/
    completed/cancelled. Additive to v1; no bump."""

    type: Literal["tasks.updated"] = "tasks.updated"
    items: list[dict]


class PlanUpdated(_Body):
    """The written plan a Plan-level turn produced, or its status changing.

    Distinct from :class:`TasksUpdated` in the way the two concepts are: that one is the
    running checklist, this is the document the operator is being asked to approve. Carries
    the whole plan for the same reason — full state replays idempotently — and a
    ``revision`` that increments on every resubmission, which is what lets the panel treat
    a revised plan as a new arrival rather than a redraw of the old one.

    ``status`` is one of pending/approved/revising/denied. Additive to v1; no bump."""

    type: Literal["plan.updated"] = "plan.updated"
    title: str
    body: str
    steps: list[str]
    status: str
    revision: int


class PermissionChanged(_Body):
    """The thread's permission level changed from inside the run.

    Load-bearing rather than informational. The level is the one fact the client both
    *holds* and *sends back* — it rides every message and the backend persists it against
    the conversation — so a client that did not hear about a level the agent changed would
    write the stale one straight back over it on the operator's next message. Emitted by
    the two tools that move it (``tools/plan.py``); an operator-chosen level needs no event
    because the client is where it came from. Additive to v1; no bump."""

    type: Literal["permission.changed"] = "permission.changed"
    level: str
    #: Why, in one line, for the transcript — "entering plan mode", "plan approved".
    reason: str
