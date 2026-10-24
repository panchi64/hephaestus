"""The thread actions this installation ships with.

Data, not code — the same discipline ``services/subagents/roster`` keeps, and for the same
reason: these are a list of names with descriptions, and the moment one of them is a
function the list stops being readable as a list.

**An action names an id, never a route.** Each of these already has a control in the
interface and a relay behind it; a command is a second way to reach the one that exists,
so what the catalog carries is the id the client maps onto that relay. ``new-thread`` is
the case that settles the shape: it navigates and calls nothing, so a method-and-path
record would have had to carry an empty one.

**Which of these a given request may use is not decided here.** An action needing a
conversation is useless in a composer that has none, and the route is what knows whether
there is one — see ``registry``. This file says what exists.
"""

from __future__ import annotations

from services.permissions import PERMISSION_LADDER

from .spec import CommandSpec

#: Every built-in action, in the order the picker lists them: the two that act on the
#: conversation the operator is looking at, then the one that leaves it, then the two that
#: change what the thread *is*.
BUILTIN_ACTIONS: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="compact",
        source="action",
        kind="action",
        action="compact",
        title="Compact now",
        description=(
            "Fold this thread's earlier turns into a summary without waiting for the threshold."
        ),
        argument_hint="what the summary should keep",
    ),
    CommandSpec(
        name="fork",
        source="action",
        kind="action",
        action="fork",
        title="Fork from the last turn",
        description="Open a new thread carrying history up to here, leaving this one untouched.",
    ),
    CommandSpec(
        name="retitle",
        source="action",
        kind="action",
        action="retitle",
        title="Retitle this thread",
        description="Have the backend name the conversation again from what it now holds.",
    ),
    CommandSpec(
        name="new",
        source="action",
        kind="action",
        action="new-thread",
        title="New thread",
        description="Start a fresh conversation in the current mode.",
    ),
    CommandSpec(
        name="level",
        source="action",
        kind="action",
        action="permission-level",
        title="Set the permission level",
        description="How far the model may go before it stops to ask. Rides the next send.",
        argument_hint=" | ".join(PERMISSION_LADDER),
        # From the permissions vocabulary rather than spelled again: a further level would
        # otherwise be offered everywhere except here, which is the one place the operator
        # would go looking for it. The **ladder**, not the set beside it — a set has no
        # order to hand a picker, and string hashing is randomised per process, so the
        # levels would come out shuffled differently on every boot. The hint is joined from
        # the same tuple for the same reason: it was spelled out once, and one level later
        # it was the only line in the command registry still claiming there were four.
        action_choices=PERMISSION_LADDER,
        argument_required=True,
    ),
)

#: The actions that mean nothing without a conversation to act on. ``new`` is deliberately
#: absent — starting a thread is exactly what you do when you have none.
NEEDS_CONVERSATION: frozenset[str] = frozenset({"compact", "fork", "retitle"})
