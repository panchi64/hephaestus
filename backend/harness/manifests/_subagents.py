"""Spawning agents, and reaching them afterwards — the implementation.

The abstraction lives in ``services/subagents/``, where ``tools/`` can name it; this half
composes actual chat turns, and so sits up here with the wiring. Private module (the
leading underscore keeps manifest discovery from mistaking it for a feature).

There is no agent engine here. Spawning one is: make a conversation, decide how far it may
reach and which files it works on, and submit the turn a route would have submitted.
Everything else — messages, reports, stops — rides runs that already exist or wakes that
start new ones (``_subagent_wake.py``).

**Trees.** Every spawned agent belongs to the delegation tree of the thread that spawned
it, rooted at the operator's own thread. A handle is unique within a tree, and a tree is
the boundary of who may address whom: an agent can message, read or list anything in its
own tree and nothing outside it, and can stop only what is below it.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, replace

from agent.summarize import resolve_auto_compact_policy
from harness.manifest import DormantCategory, HarnessContext
from models.subagent import SubagentRecord
from routes.chat import compose_turn, resolve_turn_models
from runs import RunRegistry, RunStatus
from services.agents import (
    AgentCatalog,
    AgentDefinition,
    AgentNameError,
    lower_of,
    normalize_agent_name,
)
from services.agents.definition import WorkspacePolicy
from services.agents.thread import DELEGATION_TOOLS, may_delegate, resolve_thread_agent
from services.conversations import ConversationBinding, ConversationStore
from services.offline import OfflineModeService
from services.permissions import DEFAULT_PERMISSION, stricter_permission
from services.projects import ProjectStore, WorktreeManager
from services.registry import ModelRegistry
from services.settings_store import (
    SettingsStore,
    get_agent_request_limit_override,
    get_context_thresholds,
    get_subagent_limit,
)
from services.subagent_store import SubagentStore
from services.subagents.briefing import brief_for
from services.subagents.launcher import (
    PARENT_ADDRESS,
    ROOT_HANDLE,
    LaunchedSubagent,
    SubagentLauncher,
    SubagentParent,
    SubagentUnavailableError,
    SubagentView,
)
from services.tool_policy import effective_disabled_tools
from services.uploads import UploadStore
from services.workspace import DELEGATION_SEP
from services.workspace_kind import workspace_kind

from ._subagent_wake import SubagentWake

logger = logging.getLogger(__name__)

#: The run kind a spawned agent's first turn is submitted under: foreground for the model,
#: background for the operator (``runs/lanes.py``).
SUBAGENT_KIND = "linked"

#: How much of a task is kept as the conversation's name.
_TITLE_MAX_CHARS = 80

#: Isolation asked for at launch only ever moves a definition's own answer further apart.
_ISOLATION_RANK: dict[WorkspacePolicy, int] = {"shared": 0, "isolated": 1, "own": 2}

#: Words ``to`` reads as an address rather than a handle, so no agent may be called them.
_RESERVED_HANDLES: frozenset[str] = frozenset({PARENT_ADDRESS, ROOT_HANDLE})


@dataclass(frozen=True)
class _Position:
    """Where a thread sits in its delegation tree."""

    #: The tree's root thread. Empty for a run with no thread of its own.
    root: str
    #: 0 for the root, 1 for the agents it spawned, and so on.
    depth: int
    #: The thread's own record, when another agent spawned it.
    record: SubagentRecord | None


class ConversationSubagents(SubagentLauncher):
    """Spawned agents, launched and reached as ordinary conversations.

    Constructed at wiring time because a tool runs deep inside a Run and has no other way
    to reach turn composition.
    """

    def __init__(self, ctx: HarnessContext, records: SubagentStore, wake: SubagentWake) -> None:
        self._ctx = ctx
        self._records = records
        self._wake = wake
        self._conversations = ctx.services.get(ConversationStore)
        self._models = ctx.services.get(ModelRegistry)
        self._runs = ctx.services.get(RunRegistry)
        self._settings = ctx.services.get(SettingsStore)
        self._offline = ctx.services.get(OfflineModeService)
        self._uploads = ctx.services.get(UploadStore)
        self._agents = ctx.services.get(AgentCatalog)
        #: Handles chosen but not yet recorded, as (owner, root, handle). A launch picks its
        #: name before a whole turn is composed and records it after, so concurrent
        #: launches in one step consult these as well as the register.
        self._reserved: set[tuple[str, str, str]] = set()
        self._reserve_lock = asyncio.Lock()

    # -- spawning ------------------------------------------------------------------

    async def launch(
        self,
        owner_id: str,
        agent: AgentDefinition,
        task: str,
        *,
        handle: str,
        parent: SubagentParent | None = None,
        isolate: bool = False,
    ) -> LaunchedSubagent:
        task = task.strip()
        if not task:
            raise SubagentUnavailableError("An agent needs a task to do.")
        parent = parent or SubagentParent()
        thread = parent.conversation_id or ""
        position = await self._position(owner_id, thread)
        await self._check_depth(owner_id, thread, position)
        await self._check_capacity(owner_id, position.root)
        handle = await self._handle_for(owner_id, position.root, handle, agent)
        try:
            return await self._launch(
                owner_id,
                agent,
                task,
                handle=handle,
                parent=parent,
                position=position,
                isolate=isolate,
            )
        finally:
            async with self._reserve_lock:
                self._reserved.discard((owner_id, position.root, handle))

    async def _launch(
        self,
        owner_id: str,
        agent: AgentDefinition,
        task: str,
        *,
        handle: str,
        parent: SubagentParent,
        position: _Position,
        isolate: bool,
    ) -> LaunchedSubagent:
        try:
            models = await resolve_turn_models(
                self._models, agent.endpoint_id, agent.model, owner_id=owner_id
            )
        except Exception as exc:
            raise SubagentUnavailableError(f"No usable model is configured: {exc}") from exc

        # What the definition asks for, capped by what the spawning thread may do: one
        # approved launch must not buy a level the operator never chose.
        permission = agent.permission_ceiling or DEFAULT_PERMISSION
        if parent.permission is not None:
            permission = stricter_permission(parent.permission, permission)

        depth = position.depth + 1
        disabled = await effective_disabled_tools(
            self._settings,
            self._offline,
            owner_id,
            vision=models[4],
            # Nobody is in the agent's conversation, so it is not offered the tools that
            # would stop and wait for someone.
            kind=SUBAGENT_KIND,
            availability=self._ctx.category_availability,
            caps=self._ctx.capabilities,
        )
        if not may_delegate(agent, depth, self._ctx.settings.agent_max_depth):
            disabled = disabled | DELEGATION_TOOLS
        # Refuse rather than degrade: an agent missing the one capability its job rests on
        # still answers, from memory, and reads as though it had done the work.
        withheld = {
            tool for tool in agent.required if tool in disabled or not agent.tools.permits(tool)
        }
        if withheld:
            raise SubagentUnavailableError(
                f"`{agent.name}` needs {', '.join(sorted(withheld))}, which "
                f"{'is' if len(withheld) == 1 else 'are'} switched off or unavailable."
            )

        policy = _isolation(agent.workspace, isolate=isolate)
        delegation_id = f"s-{secrets.token_hex(4)}"
        base = parent.workspace_key or parent.conversation_id or ""
        if not base:
            # Nothing to share means a workspace of its own; nothing to fork is a failure.
            if policy == "isolated":
                raise SubagentUnavailableError(
                    "This conversation has no workspace to give an agent its own copy "
                    "of. Spawn it without isolation, or do the work here."
                )
            policy = "own"
        workspace_key = _workspace_key(base, policy, delegation_id)

        parent_kind = workspace_kind(parent.workspace_kind)
        if policy == "isolated" and parent_kind == "checkout":
            raise SubagentUnavailableError(
                "This conversation works directly in the project's folder, which cannot "
                "be copied for an agent. Call `project_open_worktree` first, or spawn "
                "it without isolation."
            )
        child_kind = "sandbox" if policy == "own" else parent_kind
        binding = ConversationBinding(
            workspace_kind=child_kind,
            project_id=parent.project_id,
            permission=permission,
            agent_name=agent.name,
            tools=agent.tools,
            permission_ceiling=permission,
            workspace_key=workspace_key,
        )
        thread = parent.conversation_id or ""
        conversation_id = await self._conversations.create_conversation(
            owner_id,
            title=_title_for(handle, task),
            project_id=parent.project_id,
            workspace_kind=child_kind,
            permission=permission,
            agent_name=agent.name,
            parent_id=thread or None,
            workspace_key=workspace_key,
            # Hidden from the session list until the operator opens it.
            ephemeral=True,
        )
        root = position.root or thread
        self._wake.remember_root(conversation_id, root)

        created = compose_turn(
            prompt=task,
            conversation_id=conversation_id,
            models=models,
            capabilities=self._ctx.capabilities,
            categories=self._ctx.tool_categories,
            dormant=DormantCategory.summaries(self._ctx.dormant_categories),
            instruction_providers=(
                *self._ctx.instruction_providers,
                _briefing(brief_for(agent.instructions, policy)),
            ),
            prompt_context_providers=self._ctx.prompt_context_providers,
            registry=self._runs,
            store=self._conversations,
            uploads=self._uploads,
            disabled_tools=disabled,
            binding=binding,
            # A definition's limit is a request, folded under the operator's.
            request_limit=lower_of(
                agent.request_limit,
                await get_agent_request_limit_override(self._settings, owner_id),
            ),
            context_thresholds=await get_context_thresholds(self._settings, owner_id),
            auto_compact=await resolve_auto_compact_policy(self._settings, owner_id),
            owner_id=owner_id,
            kind=SUBAGENT_KIND,
            # Nobody is watching, and a streaming model keeps the inactivity watchdog fed,
            # so only a wall clock can end a wedged one.
            wall_clock_timeout_s=lower_of(
                agent.wall_clock_timeout_s,
                self._ctx.settings.subagent_wall_clock_timeout_s,
            ),
            ephemeral=True,
        )

        await self._records.record(
            subagent_id=created.run_id,
            owner_id=owner_id,
            parent_conversation_id=thread,
            root_conversation_id=root,
            depth=depth,
            child_conversation_id=conversation_id,
            run_id=created.run_id,
            parent_run_id=parent.run_id,
            spec_name=agent.name,
            handle=handle,
            task=task,
            workspace_policy=policy,
            workspace_key=workspace_key,
            delegation_id=delegation_id if policy == "isolated" else None,
            # Stored: the fork is landed from a terminal hook, long after the binding that
            # knew these is out of reach.
            workspace_kind=child_kind,
            project_id=parent.project_id,
        )
        return LaunchedSubagent(
            subagent_id=created.run_id,
            conversation_id=conversation_id,
            run_id=created.run_id,
            name=agent.name,
            handle=handle,
            task=task,
        )

    async def _check_depth(self, owner_id: str, thread: str, position: _Position) -> None:
        spawner = await self._thread_agent(owner_id, thread)
        if not spawner.can_delegate:
            raise SubagentUnavailableError(
                f"`{spawner.name}` is not allowed to delegate. Do the work yourself."
            )
        if not may_delegate(spawner, position.depth, self._ctx.settings.agent_max_depth):
            raise SubagentUnavailableError(
                "The delegation tree is already as deep as it may grow, so you cannot "
                "spawn agents of your own. Do the work yourself, or message `parent` if "
                "it needs splitting further."
            )

    async def _thread_agent(self, owner_id: str, thread: str) -> AgentDefinition:
        if not thread:
            return await self._agents.resolve(owner_id, None)
        applied = await resolve_thread_agent(
            self._agents,
            owner_id,
            await self._conversations.binding(thread),
            thread,
            projects=self._ctx.services.get_optional(ProjectStore),
            worktrees=self._ctx.services.get_optional(WorktreeManager),
        )
        return applied.agent

    async def _handle_for(
        self, owner_id: str, root: str, requested: str, agent: AgentDefinition
    ) -> str:
        """The name this agent will answer to: normalised, unique in its tree, and claimed
        before this returns, so concurrent launches cannot both take it. Suffixed rather
        than refused on a clash — ``explorer-2`` is legible, and a failed launch costs a
        retry."""
        try:
            base = normalize_agent_name(requested) if requested.strip() else agent.name
        except AgentNameError as exc:
            raise SubagentUnavailableError(str(exc)) from exc
        async with self._reserve_lock:
            taken = await self._records.handles_in_tree(root, owner_id)
            taken |= _RESERVED_HANDLES
            taken |= {
                handle
                for owner, tree, handle in self._reserved
                if owner == owner_id and tree == root
            }
            chosen = base
            suffix = 2
            while chosen in taken:
                chosen = f"{base}-{suffix}"
                suffix += 1
            self._reserved.add((owner_id, root, chosen))
            return chosen

    async def _check_capacity(self, owner_id: str, root: str) -> None:
        """Refuse a launch that would put this tree over the operator's cap. An agent parked
        on an approval still counts: what it waits for is a person, not a slot."""
        limit = await get_subagent_limit(self._settings, owner_id)
        if limit is None:
            return
        running = len(await self._records.live(owner_id, root_conversation_id=root))
        if running < limit:
            return
        raise SubagentUnavailableError(
            f"{running} agents are already working in this tree, which is the most allowed "
            "at once. Do not spawn another yet and do not retry this — you will be told as "
            "each one finishes. Carry on with whatever does not depend on them, or end "
            "your turn."
        )

    # -- reaching agents in the tree -----------------------------------------------

    async def message(self, owner_id: str, conversation_id: str, to: str, text: str) -> str:
        text = text.strip()
        if not text:
            raise SubagentUnavailableError("A message with nothing in it says nothing.")
        position = await self._position(owner_id, conversation_id)
        sender = position.record.handle if position.record else ROOT_HANDLE
        address = _address(to)
        if address == PARENT_ADDRESS:
            if position.record is None or not position.record.parent_conversation_id:
                raise SubagentUnavailableError(
                    "No agent spawned this thread, so it has no parent to message."
                )
            target = position.record.parent_conversation_id
            parent = await self._records.by_child(target)
            label = parent.handle if parent else ROOT_HANDLE
        elif address == ROOT_HANDLE:
            target, label = position.root, ROOT_HANDLE
        else:
            row = await self._resolve(owner_id, position.root, address)
            target, label = row.child_conversation_id, row.handle
        if not target or target == conversation_id:
            raise SubagentUnavailableError(f"`{to}` is you; there is nobody to send it to.")
        delivered = await self._wake.deliver(owner_id, target, text, source="agent", sender=sender)
        if not delivered:
            raise SubagentUnavailableError(
                f"`{label}` was stopped, so nothing will read this. Spawn a new agent if "
                "the work still needs doing."
            )
        return label

    async def read(self, owner_id: str, conversation_id: str, handle: str) -> SubagentView:
        position = await self._position(owner_id, conversation_id)
        view = self._live_view(await self._resolve(owner_id, position.root, handle))
        if view.summary is None and view.status in {"running", "blocked"}:
            # Its latest answer, so a parent checking on a long-running agent sees where it
            # has got to. A current best, explicitly not a report.
            view = replace(view, summary=await self._latest_answer(view.conversation_id))
        return view

    async def stop(self, owner_id: str, conversation_id: str, handle: str) -> list[str]:
        position = await self._position(owner_id, conversation_id)
        row = await self._resolve(owner_id, position.root, handle)
        if not await self._is_below(row, conversation_id):
            raise SubagentUnavailableError(
                f"`{row.handle}` was not spawned by you or by an agent below you, so it is "
                "not yours to stop. Message it, or its parent, instead."
            )
        rows = [row, *await self._records.descendants(row.child_conversation_id, owner_id)]
        # Marked stopped before any run is cancelled, so the reports the cancellations
        # produce do not wake an agent that is itself being stopped.
        for each in rows:
            await self._records.settle(each.id, status="cancelled")
        stopped: list[str] = []
        for each in rows:
            run = self._runs.active_run_for(each.child_conversation_id, owner_id)
            if run is not None and not run.is_terminal and await self._runs.cancel(run.id):
                stopped.append(each.handle or each.spec_name)
        return stopped

    async def tree(self, owner_id: str, conversation_id: str) -> list[SubagentView]:
        position = await self._position(owner_id, conversation_id)
        rows = await self._records.tree(position.root, owner_id)
        return [self._live_view(row) for row in rows]

    async def below(self, owner_id: str, conversation_id: str) -> list[SubagentView]:
        rows = await self._records.descendants(conversation_id, owner_id)
        return [self._live_view(row) for row in rows]

    async def _position(self, owner_id: str, conversation_id: str) -> _Position:
        if not conversation_id:
            return _Position(root="", depth=0, record=None)
        row = await self._records.by_child(conversation_id)
        if row is None or row.owner_id != owner_id:
            return _Position(root=conversation_id, depth=0, record=None)
        root = row.root_conversation_id or row.parent_conversation_id
        self._wake.remember_root(conversation_id, root)
        return _Position(root=root, depth=row.depth, record=row)

    async def _is_below(self, row: SubagentRecord, conversation_id: str) -> bool:
        current: SubagentRecord | None = row
        while current is not None:
            if current.parent_conversation_id == conversation_id:
                return True
            if not current.parent_conversation_id:
                return False
            current = await self._records.by_child(current.parent_conversation_id)
        return False

    async def _resolve(self, owner_id: str, root: str, handle: str) -> SubagentRecord:
        """The agent of this tree called ``handle``, or a refusal naming it back."""
        address = _address(handle)
        row = await self._records.by_handle(root, address, owner_id)
        if row is None:
            raise SubagentUnavailableError(
                f"Nothing in your delegation tree is called {handle!r}. `agents_list` has "
                "every agent in it."
            )
        return row

    def _live_view(self, row: SubagentRecord) -> SubagentView:
        """A stored row, corrected by the agent's running turn if it has one."""
        run = self._runs.active_run_for(row.child_conversation_id, row.owner_id)
        if run is None or run.is_terminal:
            return self._records.view(row)
        metrics = run.metrics
        return self._records.view(
            row,
            status="blocked" if run.status is RunStatus.awaiting_input else "running",
            context_used=getattr(metrics, "context_used", None) if metrics else None,
            context_window=run.context_window,
        )

    async def _latest_answer(self, conversation_id: str) -> str | None:
        turns = await self._conversations.messages_view(conversation_id)
        return next(
            (t.content for t in reversed(turns) if t.role == "assistant" and t.content), None
        )


def _address(to: str) -> str:
    try:
        return normalize_agent_name(to)
    except AgentNameError:
        return to.strip()


def _isolation(declared: WorkspacePolicy, *, isolate: bool) -> WorkspacePolicy:
    """Where this launch works. Isolation only ever increases over the definition's own."""
    asked: WorkspacePolicy = "isolated" if isolate else declared
    return asked if _ISOLATION_RANK[asked] > _ISOLATION_RANK[declared] else declared


def _workspace_key(base: str, policy: WorkspacePolicy, delegation_id: str) -> str:
    """The key naming the workspace this agent works in. ``own`` is the empty key, which
    every run reads as "my own conversation's"."""
    if policy == "own":
        return ""
    if policy == "shared":
        return base
    return f"{base}{DELEGATION_SEP}{delegation_id}"


def _briefing(text: str):
    """The agent's brief as a dynamic instruction, rebuilt every turn, so nothing it later
    reads can rewrite the terms it works under."""

    async def provider(_ctx) -> str:
        return text

    return provider


def _title_for(handle: str, task: str) -> str:
    """The agent's thread name: its handle and its task on one line."""
    folded = " ".join(task.split())
    title = f"{handle}: {folded}"
    if len(title) <= _TITLE_MAX_CHARS:
        return title
    return title[: _TITLE_MAX_CHARS - 1] + "…"
