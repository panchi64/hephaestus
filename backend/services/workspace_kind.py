"""Where a chat's file work happens, and which tools belong to each place.

A chat with no project works in its own sandbox. A chat bound to a project works in that
project's own folder, on whatever branch is checked out there (``checkout``). The agent
moves it to a git worktree on a branch of its own (``project_open_worktree``) when the
work should be isolated. That move is one-way and can happen mid-turn, so the tools below
are withheld live at the enabled gate (``tools/toolsets.py``) rather than folded into the
per-turn union in ``services/tool_policy.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast, get_args

type WorkspaceKind = Literal["sandbox", "checkout", "worktree"]

DEFAULT_WORKSPACE_KIND: WorkspaceKind = "sandbox"

_KINDS: tuple[WorkspaceKind, ...] = get_args(cast("object", WorkspaceKind.__value__))

#: The tools that belong to only some kinds of workspace. A tool in neither set is offered
#: everywhere: every thread has the same home, so the shell and the code tools are in all
#: of them. The repo inventory reads the thread's own project, which a sandbox thread has
#: none of.
_SANDBOX_TOOLS: frozenset[str] = frozenset()
_PROJECT_TOOLS = frozenset({"repo_inventory_agent_context"})
WORKSPACE_TOOLS: Mapping[WorkspaceKind, frozenset[str]] = {
    "sandbox": _SANDBOX_TOOLS,
    "checkout": _PROJECT_TOOLS,
    "worktree": _PROJECT_TOOLS,
}


def workspace_kind(value: str | None) -> WorkspaceKind:
    """A stored value as a kind, falling back to the sandbox, which never reaches the host."""
    for known in _KINDS:
        if value == known:
            return known
    return DEFAULT_WORKSPACE_KIND


def workspace_disabled_tools(kind: str) -> frozenset[str]:
    """The tools that belong only to other kinds of workspace."""
    own = WORKSPACE_TOOLS[workspace_kind(kind)]
    return frozenset(name for names in WORKSPACE_TOOLS.values() for name in names) - own


def is_project_folder(kind: str) -> bool:
    """Whether the files are the operator's own (a checkout or a worktree of it)."""
    return workspace_kind(kind) != "sandbox"
