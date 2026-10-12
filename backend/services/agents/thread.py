"""Running an agent as a thread's own.

A thread names its agent; every turn on it — the operator's, a wake carrying a sub-agent's
report, a compaction written on the turn's agent — resolves that name here and runs under
what it says: its brief, its tools, its ceiling. One mapping, so none of those turns can
run a different agent than the others.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.exceptions import NotFoundError
from services.agents.catalog import AgentCatalog
from services.agents.definition import AgentDefinition
from services.conversations import ConversationBinding
from services.permissions import permission_level, stricter_permission
from services.projects import ProjectStore, WorktreeManager
from services.projects.tree import thread_tree
from services.workspace_kind import is_project_folder

#: The tools that hand work to another agent, withheld from one that may not delegate.
DELEGATION_TOOLS: frozenset[str] = frozenset(
    {"subagents_launch", "subagents_send", "subagents_list"}
)

type InstructionProvider = Callable[[Any], Awaitable[str]]


@dataclass(frozen=True)
class ThreadAgent:
    agent: AgentDefinition
    #: The thread's binding with the agent's tools applied and its level capped.
    binding: ConversationBinding
    #: Tools the agent withholds from the turn on top of the operator's own set.
    withheld: frozenset[str]
    #: The agent's brief, when it has one.
    instructions: tuple[InstructionProvider, ...]


async def resolve_thread_agent(
    catalog: AgentCatalog,
    owner_id: str,
    binding: ConversationBinding,
    conversation_id: str | None,
    *,
    projects: ProjectStore | None = None,
    worktrees: WorktreeManager | None = None,
) -> ThreadAgent:
    """The agent this thread runs, applied to its binding."""
    root = await _project_root(projects, worktrees, owner_id, binding, conversation_id)
    agent = await catalog.resolve(owner_id, binding.agent_name, project_root=root)
    return as_thread(agent, binding)


def as_thread(agent: AgentDefinition, binding: ConversationBinding) -> ThreadAgent:
    ceiling = (
        None if agent.permission_ceiling is None else permission_level(agent.permission_ceiling)
    )
    permission = binding.permission
    if ceiling is not None:
        permission = stricter_permission(permission, ceiling)
    return ThreadAgent(
        agent=agent,
        binding=replace(
            binding, tools=agent.tools, permission=permission, permission_ceiling=ceiling
        ),
        withheld=frozenset() if agent.can_delegate else DELEGATION_TOOLS,
        instructions=(_brief(agent.instructions),) if agent.instructions else (),
    )


def _brief(text: str) -> InstructionProvider:
    async def agent_brief_instructions(_ctx: Any) -> str:
        return text

    return agent_brief_instructions


async def _project_root(
    projects: ProjectStore | None,
    worktrees: WorktreeManager | None,
    owner_id: str,
    binding: ConversationBinding,
    conversation_id: str | None,
) -> Path | None:
    if projects is None or binding.project_id is None:
        return None
    if not is_project_folder(binding.workspace_kind):
        return None
    try:
        project = await projects.get(owner_id, binding.project_id)
    except NotFoundError:
        return None
    root = Path(project.root_path)
    if worktrees is None:
        return root
    return await thread_tree(
        root, worktrees.path_for(binding.project_id), binding.workspace_kind, conversation_id
    )
