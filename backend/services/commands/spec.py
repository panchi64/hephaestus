"""What a slash command *is*, as data.

A command is not a new kind of thing. It is a **name the operator can type** that resolves
to something the platform already has — a published skill, a sub-agent on the roster, a
thread action that already has a route, and later a prompt template of their own. So this
record carries no behaviour: it says which source answered, what to show in the picker, and
the one handle (``target`` or ``action``) the resolver needs to do the rest.

**Two names, and the second is why nothing is ever dropped.** ``name`` is what the operator
types; ``qualified_name`` prefixes it with the source. A skill, a workflow and a sub-agent
may all legitimately be called ``reviewer`` — three real things, and deleting two of them
because the third took the name is a data-loss bug wearing a precedence rule. So all three
ship in the catalog, ``/reviewer`` resolves the winner, and ``/agent:reviewer`` resolves
exactly.

**Precedence is locality, extended rather than re-invented.** ``services/subagents/roster``
already says a project's declaration beats the one this installation ships, because a
repository that has written down how *its* reviewer works knows something we do not. The
same ordering applies one level up, across sources: what the installation ships loses to
what the operator wrote, which loses to what the project declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: What a command does when the operator picks it. ``prompt`` composes a turn — the
#: expansion rides the tail of that turn's user message. ``action`` sends no message at
#: all: it names something the client already knows how to do.
CommandKind = Literal["prompt", "action"]

#: Which source answered. Also the picker's grouping key, so the headings are the
#: backend's rather than a list the client keeps in step by hand.
CommandSource = Literal["action", "agent", "skill", "workflow", "project"]

#: The thread actions a command can name. A **stable id**, never a method and a path: the
#: client maps it onto the relay it already has, and ``new-thread`` has no route behind it
#: at all — it is navigation — which is exactly why a URL shape would have been wrong.
ActionId = Literal["compact", "fork", "new-thread", "permission-level", "retitle"]

#: Locality, as a number. Higher wins a bare-name collision. The three tiers are what the
#: installation ships, what the operator wrote, and what the project declared.
_TIER: dict[CommandSource, int] = {
    "action": 0,
    "agent": 0,
    "skill": 1,
    "workflow": 1,
    "project": 2,
}

#: The prefix a qualified name carries. Singular and short, because the operator types it:
#: ``/agent:reviewer`` reads as a sentence and ``/subagents:reviewer`` does not.
_PREFIX: dict[CommandSource, str] = {
    "action": "action",
    "agent": "agent",
    "skill": "skill",
    "workflow": "workflow",
    "project": "project",
}


@dataclass(frozen=True)
class CommandSpec:
    """One command, from whichever source produced it."""

    #: What the operator types after the slash.
    name: str
    source: CommandSource
    kind: CommandKind
    #: The picker's row label — sentence case, the operator's words rather than a slug.
    title: str
    #: One line saying what picking this does. Shown under the title.
    description: str
    #: What to type after the name, if anything ("a brief for the sub-agent"). Rendered as
    #: a hint, never enforced — the resolver decides whether an argument was required.
    argument_hint: str | None = None
    #: The thing this command resolves to: a skill's name, a sub-agent's roster name, a
    #: workflow's id. ``None`` for an action, whose handle is ``action``.
    target: str | None = None
    #: For ``kind == "action"`` only.
    action: ActionId | None = None
    #: An action whose argument is a closed set (the permission level). Empty means free
    #: text, or none at all.
    action_choices: tuple[str, ...] = ()
    #: Whether the command is useless without an argument. The picker can say so; the
    #: resolver is what enforces it.
    argument_required: bool = False
    #: The template body, for the sources that carry one. Never sent to the picker — it is
    #: the expansion's input, and a catalog that shipped every body would cost the operator
    #: a page of prose to render a list of names.
    body: str | None = None
    #: Workspace kinds this command is offered in. Empty means every kind; the route
    #: narrows further on facts it can only know per request (a disabled toolset, no
    #: conversation yet).
    workspaces: tuple[str, ...] = ()

    @property
    def qualified_name(self) -> str:
        """The name that resolves to *this* spec and no other."""
        return f"{_PREFIX[self.source]}:{self.name}"

    @property
    def tier(self) -> int:
        """Locality — higher wins a collision on the bare name."""
        return _TIER[self.source]


@dataclass(frozen=True)
class Invocation:
    """A picked command as it is *written down* on the turn it was sent with.

    Two fields and no spec, deliberately. What ``/reviewer`` resolves to is a question with
    a different answer on Tuesday than it had on Monday — a skill gets unpublished, a
    project file is edited, a tool is switched off — and the honest record of a turn is that
    the operator typed that name, not a frozen copy of what it meant at the time.

    So this is what a regenerate re-reads, and the expansion is built again from whatever
    the name means *now*. The alternative — persisting the expanded block into history —
    was rejected twice over: a template body would replay forever after the file it came
    from changed, and a directive reading "call this before anything else in this turn"
    is a lie the moment there is another turn after it.
    """

    #: Exactly what the client sent — bare (``reviewer``) or qualified (``agent:reviewer``).
    #: Not normalised to one or the other: which of them the operator typed is the whole of
    #: what says whether they meant "the reviewer" or "*that* reviewer".
    name: str
    argument: str = ""
