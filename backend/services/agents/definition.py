"""What an agent *is*, as data.

One record covers the agent a chat runs, the built-in workers the model delegates to, the
ones a project declares in files, and the ones the operator or an agent writes. Behaviour
lives in the engine; a definition only decides how one ordinary turn is composed.

**A definition is a request, never a grant.** Every limit here is folded against the
thread or launcher that uses it: ``permission_ceiling`` with the conversation's own level,
``wall_clock_timeout_s`` with the operator's bound. An agent that
declares itself all-powerful gets what the operator already allowed and no more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from prompts.voice import CLASSES

#: Which files a delegated agent works on. Each is one workspace *key*; see
#: ``services/workspace.py`` for how a key is read.
#:
#: ``shared`` — the launching thread's own workspace: it sees the work in progress, and what
#: it changes is simply there afterwards.
#:
#: ``isolated`` — a branch and worktree of its own, cut from where the launching thread
#: works and sharing its scratch; the launching thread reviews it and merges or discards it.
#:
#: ``own`` — a fresh scratch of its own, for work that is not about the parent's files.
WorkspacePolicy = Literal["shared", "isolated", "own"]

WORKSPACE_POLICIES: frozenset[str] = frozenset({"shared", "isolated", "own"})


def workspace_policy(raw: str) -> WorkspacePolicy | None:
    """``raw`` as a policy, or None when it names none."""
    if raw == "shared":
        return "shared"
    if raw == "isolated":
        return "isolated"
    if raw == "own":
        return "own"
    return None


#: Where a definition came from. ``user`` and ``agent`` are stored; ``builtin`` ships in
#: code; ``project`` is read from the bound project's files; ``plugin`` is contributed by an
#: installed plugin.
AgentSource = Literal["builtin", "project", "user", "agent", "plugin"]

#: The agent a chat runs when it names none.
DEFAULT_AGENT = "default"

#: The class an agent belongs to when its definition names none.
DEFAULT_CLASS = "tripodes"

#: The class of the agent the operator talks to, at the root of every delegation tree.
ROOT_CLASS = "talos"

#: Classes no operator-written agent may take: there is one Talos, the built-in default,
#: and the helpers are a voice rather than an agent anyone runs.
UNASSIGNABLE_CLASSES: frozenset[str] = frozenset({ROOT_CLASS, "keledones"})


def class_key(raw: str) -> str | None:
    """``raw`` (a class key or display name, any case) as a key of ``CLASSES``, or None."""
    key = raw.strip().lower().replace("ē", "e")
    return key if key in CLASSES else None


def agent_display_name(name: str | None) -> str:
    """What the operator reads for an agent: the default one is Talos."""
    return CLASSES[ROOT_CLASS].display if not name or name == DEFAULT_AGENT else name


@dataclass(frozen=True)
class ToolAccess:
    """Which tools an agent is offered, before the level, the workspace and the operator's
    own switches withhold anything further.

    An entry is a tool's full name (``files_write_file``) or a category (``files``), which
    covers every tool named ``<category>_…``. ``allow`` of None offers everything.
    """

    allow: frozenset[str] | None = None
    deny: frozenset[str] = frozenset()

    def permits(self, tool: str) -> bool:
        if _matches(tool, self.deny):
            return False
        return self.allow is None or _matches(tool, self.allow)


UNRESTRICTED = ToolAccess()


def _matches(tool: str, entries: frozenset[str]) -> bool:
    if tool in entries:
        return True
    category, sep, _ = tool.partition("_")
    return bool(sep) and category in entries


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    #: One or two lines on what the agent is for. The launch tool's roster is these lines,
    #: so it is the only thing the model reads when choosing whom to delegate to.
    description: str
    #: Its key in ``prompts.voice.CLASSES``: the role, voice and callsign pool it takes.
    agent_class: str = DEFAULT_CLASS
    #: The agent's standing brief, delivered as an instruction so nothing it later reads
    #: can rewrite it. May be empty: an agent can differ from the default only in its model
    #: or its tools.
    instructions: str = ""
    #: The model this agent runs on: a registry endpoint and a model on it. None for either
    #: leaves it to the thread's pick, else the ``main`` role.
    endpoint_id: str | None = None
    model: str | None = None
    tools: ToolAccess = UNRESTRICTED
    #: Tools without which the agent is a pretence rather than a degraded version of itself
    #: (a researcher with no web reach answers from memory). Delegating to it is refused.
    required: frozenset[str] = field(default_factory=frozenset)
    #: The furthest this agent may reach. None takes the thread's (or the default) level.
    permission_ceiling: str | None = None
    #: Where a delegated run of this agent works by default. The launch may ask for more
    #: isolation, never less.
    workspace: WorkspacePolicy = "shared"
    wall_clock_timeout_s: float | None = None
    #: Whether this agent may hand work to other agents.
    can_delegate: bool = False
    #: How many levels below itself this agent's delegation may reach. None defers to the
    #: installation's limit.
    max_depth: int | None = None
    #: Model requests one run of this agent may make before it ends with a partial result.
    #: None leaves the run unbounded.
    max_requests: int | None = None
    #: Whether other agents may delegate to this one. False keeps it off the launch roster.
    delegable: bool = True
    source: AgentSource = "builtin"
    #: Only published agents run. A draft is visible to the operator and to nothing else.
    published: bool = True
    #: The stored row's id, for ``user`` and ``agent`` sources.
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("an agent needs a name")
        if self.max_requests is not None and self.max_requests <= 0:
            raise ValueError("max_requests must be positive")
        if self.agent_class not in CLASSES:
            raise ValueError(f"{self.agent_class!r} is not an agent class")
