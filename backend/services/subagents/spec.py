"""What a sub-agent *is*, as data.

A sub-agent is not a class and not a run loop. It is a handful of values that decide how
one ordinary turn is composed — where it works, how far it may reach, what standing
brief it is given, whether it gets a copy of the workspace. Everything else about it is the
engine every other turn uses.

Keeping it data is what makes the roster extensible without new code: a built-in sub-agent
and one a project declares in a file are the same record, built two ways, and the launcher
cannot tell them apart. A sub-agent that needed *behaviour* of its own would be a sign the
engine was missing something, not a reason for a subclass here.

**A spec is a request, never a grant.** Every field below is read as a ceiling the launcher
then lowers against the thread that asked — most importantly ``permission_ceiling``, which
is folded with the parent's own level so a sub-agent can never reach further than the
conversation that launched it. A project file declaring itself all-powerful gets exactly
what the operator already allowed and no more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: Which files a sub-agent works on. All three are one workspace *key* and nothing else —
#: see ``services/workspace.py`` for how a key is read.
#:
#: ``shared`` — the launching thread's own workspace. The default, and the one that makes a
#: sub-agent feel like part of the same session: it sees the work in progress, and what it
#: does is simply there afterwards, with nothing to merge. The cost is that two agents are
#: editing one tree, which is fine for reading and fine for work the parent is waiting on,
#: and wrong for anything long-running the parent means to keep working alongside.
#:
#: ``isolated`` — a delegated fork of that workspace: its own sandbox session, or its own
#: checkout on its own branch, cut from what the parent's transcript describes. What it
#: changed is merged back when it ends, and anything the parent changed meanwhile comes
#: back as a reported conflict rather than as the sub-agent's version winning.
#:
#: ``own`` — its own conversation's workspace, unrelated to the parent's. For a sub-agent
#: whose work is not about the parent's files at all (reading the open web, say).
WorkspacePolicy = Literal["shared", "isolated", "own"]


@dataclass(frozen=True)
class SubagentSpec:
    """One sub-agent the model may launch."""

    #: How the model names it in the launch call, and how the operator sees it on a card.
    name: str
    #: One or two lines saying what this sub-agent is *for*, joined with its peers into the
    #: launch tool's own description. This is the only place the model reads the roster —
    #: not a standing instruction at the prompt head, which would repeat itself inside the
    #: cached prefix on every request and say it furthest from where the choice is made.
    description: str
    #: The sub-agent's standing brief, delivered as an instruction rather than as its
    #: opening message: instructions are rebuilt each turn and never read back out of
    #: history, so nothing the sub-agent later reads can rewrite what it was told to do.
    brief: str
    #: The furthest this sub-agent may reach, before the parent's own level is folded in.
    #: None takes the default level. Never raises what the operator allowed.
    permission_ceiling: str | None = None
    #: Where this sub-agent works by default. The launch call may ask for ``isolated``
    #: whatever this says — the model knows whether *this* task is one the launching thread
    #: means to work alongside — but it may never ask for less isolation than the spec
    #: declares, so a sub-agent written to stay out of the way cannot be talked into the
    #: operator's own tree.
    workspace: WorkspacePolicy = "shared"
    #: Tools withheld from this sub-agent on top of everything the workspace, level and the
    #: operator's own switches already withhold. For narrowing a sub-agent to its job —
    #: a reviewer that reads and reports has no business editing.
    withheld: frozenset[str] = field(default_factory=frozenset)
    #: Tools without which this sub-agent is a pretence rather than a degraded version of
    #: itself — a researcher with no web reach answers from memory and reads, to the
    #: operator, exactly as though it had looked. The launcher refuses instead.
    required: frozenset[str] = field(default_factory=frozenset)
    #: Model round-trips it may spend, and how long it may take in total. None leaves both
    #: to the engine's own defaults (the operator's ceiling, else the config default).
    request_limit: int | None = None
    wall_clock_timeout_s: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a sub-agent needs a name")
        if not self.brief.strip():
            raise ValueError(f"sub-agent {self.name!r} has no brief")
