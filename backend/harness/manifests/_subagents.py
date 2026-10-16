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
from pathlib import Path
from typing import override

from pydantic_ai import RunContext

from agent.summarize import resolve_auto_compact_policy
from core.diagnostics import render_tool_error
from core.exceptions import InvalidInputError, NotFoundError
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
from services.conversations import ConversationStore
from services.offline import OfflineModeService
from services.permissions import DEFAULT_PERMISSION, permission_level, stricter_permission
from services.projects import ProjectStore, WorktreeManager
from services.projects.repo import WorktreeError
from services.projects.worktree import WorktreeBusyError
from services.registry import ModelRegistry
from services.settings_store import (
    SettingsStore,
    get_agent_request_limit_override,
    get_context_thresholds,
    get_subagent_limit,
)
from services.subagent_store import LIVE_STATUSES, SubagentStore
from services.subagents.briefing import brief_for
from services.subagents.callsigns import allocate, fold, resolve
from services.subagents.launcher import (
    PARENT_ADDRESS,
    ROOT_HANDLE,
    ChildWorktree,
    LaunchedSubagent,
    Recipient,
    SubagentLauncher,
    SubagentParent,
    SubagentUnavailableError,
    SubagentView,
)
from services.tool_policy import effective_disabled_tools
from services.uploads import UploadStore
from services.workspace import DELEGATION_SEP, split_workspace_key
from tools import InstructionProvider, RunDeps

from ._subagent_wake import SubagentWake

logger = logging.getLogger(__name__)

#: The run kind a spawned agent's first turn is submitted under: foreground for the model,
#: background for the operator (``runs/lanes.py``).
SUBAGENT_KIND = "linked"

#: How much of a task is kept as the conversation's name.
_TITLE_MAX_CHARS = 80

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
        self._ctx: HarnessContext = ctx
        self._records: SubagentStore = records
        self._wake: SubagentWake = wake
        self._conversations: ConversationStore = ctx.services.get(ConversationStore)
        self._models: ModelRegistry = ctx.services.get(ModelRegistry)
        self._runs: RunRegistry = ctx.services.get(RunRegistry)
        self._settings: SettingsStore = ctx.services.get(SettingsStore)
        self._offline: OfflineModeService = ctx.services.get(OfflineModeService)
        self._uploads: UploadStore = ctx.services.get(UploadStore)
        self._agents: AgentCatalog = ctx.services.get(AgentCatalog)
        #: Handles and folded callsigns chosen but not yet recorded, as (owner, root, name).
        #: A launch picks its names before a whole turn is composed and records them after,
        #: so concurrent launches in one step consult these as well as the register.
        self._reserved: set[tuple[str, str, str]] = set()
        self._reserve_lock: asyncio.Lock = asyncio.Lock()

    # -- spawning ------------------------------------------------------------------

    @override
    async def launch(
        self,
        owner_id: str,
        agent: AgentDefinition,
        task: str,
        *,
        handle: str,
        parent: SubagentParent | None = None,
        worktree: bool = False,
    ) -> LaunchedSubagent:
        task = task.strip()
        if not task:
            raise SubagentUnavailableError("An agent needs a task to do.")
        parent = parent or SubagentParent()
        thread = parent.conversation_id or ""
        position = await self._position(owner_id, thread)
        await self._check_depth(owner_id, thread, position)
        await self._check_capacity(owner_id, position.root)
        handle, callsign = await self._names_for(owner_id, position.root, handle, agent)
        try:
            return await self._launch(
                owner_id,
                agent,
                task,
                handle=handle,
                callsign=callsign,
                parent=parent,
                position=position,
                worktree=worktree,
            )
        finally:
            async with self._reserve_lock:
                self._reserved.discard((owner_id, position.root, handle))
                self._reserved.discard((owner_id, position.root, fold(callsign)))

    async def _launch(
        self,
        owner_id: str,
        agent: AgentDefinition,
        task: str,
        *,
        handle: str,
        callsign: str,
        parent: SubagentParent,
        position: _Position,
        worktree: bool,
    ) -> LaunchedSubagent:
        try:
            models = await resolve_turn_models(
                self._models, agent.endpoint_id, agent.model, owner_id=owner_id
            )
        except Exception as exc:
            raise SubagentUnavailableError(f"No usable model is configured: {exc}") from exc

        # What the definition asks for, capped by what the spawning thread may do: one
        # approved launch must not buy a level the operator never chose.
        permission = permission_level(agent.permission_ceiling or DEFAULT_PERMISSION)
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
                + f"{'is' if len(withheld) == 1 else 'are'} switched off or unavailable."
            )

        policy = _isolation(agent.workspace, worktree=worktree)
        delegation_id = f"s-{secrets.token_hex(4)}"
        base = parent.workspace_key or parent.conversation_id or ""
        if not base:
            if policy == "isolated":
                raise SubagentUnavailableError(
                    render_tool_error(
                        "this run has no thread, so there is no project to cut a worktree from",
                        hints=["spawn it without `worktree`, or do the work here"],
                    )
                )
            policy = "own"
        base_ref = (
            await self._cut_worktree(owner_id, parent, base, delegation_id)
            if policy == "isolated"
            else None
        )
        workspace_key = _workspace_key(base, policy, delegation_id)

        thread = parent.conversation_id or ""
        conversation_id = await self._conversations.create_conversation(
            owner_id,
            title=_title_for(callsign, handle, task),
            project_id=parent.project_id,
            permission=permission,
            agent_name=agent.name,
            parent_id=thread or None,
            workspace_key=workspace_key,
            # Hidden from the session list until the operator opens it.
            ephemeral=True,
        )
        # Read back the way every later turn of it reads it, so the worktree and leases
        # it works under are one rule (`ConversationBinding.leases_from`).
        stored = await self._conversations.binding(conversation_id)
        binding = replace(
            stored,
            permission=permission,
            tools=agent.tools,
            permission_ceiling=permission,
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
            agent_class=agent.agent_class,
            callsign=callsign,
            task=task,
            workspace_policy=policy,
            workspace_key=workspace_key,
            delegation_id=delegation_id if policy == "isolated" else None,
            project_id=parent.project_id,
            base_ref=base_ref,
        )
        return LaunchedSubagent(
            subagent_id=created.run_id,
            conversation_id=conversation_id,
            run_id=created.run_id,
            name=agent.name,
            handle=handle,
            callsign=callsign,
            agent_class=agent.agent_class,
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
                + "spawn agents of your own. Do the work yourself, or message `parent` if "
                + "it needs splitting further."
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

    async def _names_for(
        self, owner_id: str, root: str, requested: str, agent: AgentDefinition
    ) -> tuple[str, str]:
        """The handle and callsign this agent will answer to, unique in its tree and claimed
        before this returns, so concurrent launches cannot both take them. A handle is
        suffixed rather than refused on a clash — ``explorer-2`` is legible, and a failed
        launch costs a retry. Handles and callsigns share one namespace, so a reference
        never fits one agent's handle and another's callsign."""
        try:
            base = normalize_agent_name(requested) if requested.strip() else agent.name
        except AgentNameError as exc:
            raise SubagentUnavailableError(str(exc)) from exc
        async with self._reserve_lock:
            rows = await self._records.tree(root, owner_id)
            reserved = {
                name for owner, tree, name in self._reserved if owner == owner_id and tree == root
            }
            handles = {fold(row.handle) for row in rows if row.handle}
            callsigns = {fold(row.callsign) for row in rows if row.callsign}
            live = {
                fold(row.callsign)
                for row in rows
                if row.callsign and row.status in LIVE_STATUSES
            }
            taken = handles | callsigns | reserved | _RESERVED_HANDLES
            handle = base
            suffix = 2
            while handle in taken:
                handle = f"{base}-{suffix}"
                suffix += 1
            callsign = allocate(
                agent.agent_class, live | handles | reserved | {handle}, avoid=callsigns
            )
            self._reserved.add((owner_id, root, handle))
            if callsign:
                self._reserved.add((owner_id, root, fold(callsign)))
            return handle, callsign

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
            + "at once. Do not spawn another yet and do not retry this — you will be told as "
            + "each one finishes. Carry on with whatever does not depend on them, or end "
            + "your turn."
        )

    async def _cut_worktree(
        self, owner_id: str, parent: SubagentParent, base: str, delegation_id: str
    ) -> str:
        """Cut a sub-agent's worktree from where its parent works, returning the commit
        it starts at: the parent's own worktree, or the checkout as it stands."""
        projects = self._ctx.services.get_optional(ProjectStore)
        worktrees = self._ctx.services.get_optional(WorktreeManager)
        if not parent.project_id:
            raise SubagentUnavailableError(
                render_tool_error(
                    "this thread is not filed under a project, so there is no project to "
                    + "cut a worktree from",
                    hints=["spawn it without `worktree`; it shares your scratch either way"],
                )
            )
        if projects is None or worktrees is None:
            raise SubagentUnavailableError("Worktrees are unavailable in this deployment.")
        holder, parent_delegation = split_workspace_key(base)
        try:
            project = await projects.get(owner_id, parent.project_id)
            root = Path(project.root_path)
            if parent_delegation is not None:
                source = worktrees.reopen_child(
                    project_id=parent.project_id,
                    conversation_id=holder,
                    delegation_id=parent_delegation,
                )
            elif parent.worktree:
                source = await worktrees.acquire(
                    project_id=parent.project_id,
                    root=root,
                    base_ref=project.base_ref,
                    conversation_id=holder,
                )
            else:
                source = None
            state = await worktrees.cut_child(
                project_id=parent.project_id,
                root=root,
                conversation_id=holder,
                delegation_id=delegation_id,
                source=source,
            )
        except (WorktreeError, WorktreeBusyError, InvalidInputError, NotFoundError) as exc:
            raise SubagentUnavailableError(
                render_tool_error(
                    f"could not cut a worktree for it: {exc}",
                    hints=["spawn it without `worktree` to have it work in your files"],
                )
            ) from exc
        return state.base_ref

    # -- reaching agents in the tree -----------------------------------------------

    @override
    async def recipient(self, owner_id: str, conversation_id: str, to: str) -> Recipient:
        position = await self._position(owner_id, conversation_id)
        address = _address(to)
        if address == PARENT_ADDRESS:
            if position.record is None or not position.record.parent_conversation_id:
                raise SubagentUnavailableError(
                    "No agent spawned this thread, so it has no parent to message."
                )
            target = position.record.parent_conversation_id
            parent = await self._records.by_child(target)
            found = Recipient(
                target,
                parent.handle if parent else ROOT_HANDLE,
                parent.spec_name if parent else None,
                upward=True,
            )
        elif address == ROOT_HANDLE:
            found = Recipient(position.root, ROOT_HANDLE, None, upward=True)
        else:
            row = await self._resolve(owner_id, position.root, address)
            found = Recipient(
                row.child_conversation_id,
                row.handle,
                row.spec_name,
                upward=await self._is_above(row.child_conversation_id, position.record),
            )
        if not found.conversation_id or found.conversation_id == conversation_id:
            raise SubagentUnavailableError(f"`{to}` is you; there is nobody to send it to.")
        return found

    @override
    async def message(self, owner_id: str, conversation_id: str, to: str, text: str) -> str:
        text = text.strip()
        if not text:
            raise SubagentUnavailableError("A message with nothing in it says nothing.")
        position = await self._position(owner_id, conversation_id)
        sender = position.record.handle if position.record else ROOT_HANDLE
        found = await self.recipient(owner_id, conversation_id, to)
        delivered = await self._wake.deliver(
            owner_id, found.conversation_id, text, source="agent", sender=sender
        )
        if not delivered:
            raise SubagentUnavailableError(
                f"`{found.label}` was stopped, so nothing will read this. Spawn a new agent "
                + "if the work still needs doing."
            )
        return found.label

    @override
    async def read(self, owner_id: str, conversation_id: str, handle: str) -> SubagentView:
        position = await self._position(owner_id, conversation_id)
        view = self._live_view(await self._resolve(owner_id, position.root, handle))
        if view.summary is None and view.status in {"running", "blocked"}:
            # Its latest answer, so a parent checking on a long-running agent sees where it
            # has got to. A current best, explicitly not a report.
            view = replace(view, summary=await self._latest_answer(view.conversation_id))
        return view

    @override
    async def stop(self, owner_id: str, conversation_id: str, handle: str) -> list[str]:
        position = await self._position(owner_id, conversation_id)
        row = await self._resolve(owner_id, position.root, handle)
        if not await self._is_below(row, conversation_id):
            raise SubagentUnavailableError(
                f"`{row.handle}` was not spawned by you or by an agent below you, so it is "
                + "not yours to stop. Message it, or its parent, instead."
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

    @override
    async def tree(self, owner_id: str, conversation_id: str) -> list[SubagentView]:
        position = await self._position(owner_id, conversation_id)
        rows = await self._records.tree(position.root, owner_id)
        return [self._live_view(row) for row in rows]

    @override
    async def below(self, owner_id: str, conversation_id: str) -> list[SubagentView]:
        rows = await self._records.descendants(conversation_id, owner_id)
        return [self._live_view(row) for row in rows]

    @override
    async def worktrees(self, owner_id: str, conversation_id: str) -> list[ChildWorktree]:
        rows = await self._records.descendants(conversation_id, owner_id)
        own = (row for row in rows if row.parent_conversation_id == conversation_id)
        found = (self._child_worktree(row) for row in own)
        return [child for child in found if child is not None]

    @override
    async def worktree(self, owner_id: str, conversation_id: str, handle: str) -> ChildWorktree:
        position = await self._position(owner_id, conversation_id)
        row = await self._resolve(owner_id, position.root, handle)
        if row.parent_conversation_id != conversation_id:
            raise SubagentUnavailableError(
                f"`{row.handle}` was not spawned by you, so its work is not yours to merge "
                + "or discard. The agent that spawned it decides."
            )
        child = self._child_worktree(row)
        if child is None:
            raise SubagentUnavailableError(
                f"`{row.handle}` has no worktree open: it worked in your files, or its "
                + "worktree was already merged or discarded."
            )
        return child

    def _child_worktree(self, row: SubagentRecord) -> ChildWorktree | None:
        """A record's worktree, if it was spawned into one that is still on disk."""
        worktrees = self._ctx.services.get_optional(WorktreeManager)
        if worktrees is None or not row.delegation_id or not row.project_id:
            return None
        holder, named = split_workspace_key(row.workspace_key or "")
        delegation = row.delegation_id
        if named != delegation:
            return None
        try:
            state = worktrees.reopen_child(
                project_id=row.project_id, conversation_id=holder, delegation_id=delegation
            )
        except WorktreeError:
            return None
        return ChildWorktree(
            handle=row.handle or row.spec_name,
            conversation_id=row.child_conversation_id,
            project_id=row.project_id,
            path=state.path,
            branch=state.branch,
            base_ref=row.base_ref or state.base_ref,
            holder_id=holder,
            delegation_id=delegation,
        )

    async def _position(self, owner_id: str, conversation_id: str) -> _Position:
        if not conversation_id:
            return _Position(root="", depth=0, record=None)
        row = await self._records.by_child(conversation_id)
        if row is None or row.owner_id != owner_id:
            return _Position(root=conversation_id, depth=0, record=None)
        root = row.root_conversation_id or row.parent_conversation_id
        self._wake.remember_root(conversation_id, root)
        return _Position(root=root, depth=row.depth, record=row)

    async def _is_above(self, conversation_id: str, sender: SubagentRecord | None) -> bool:
        """Whether ``conversation_id`` is the thread of an agent above ``sender``."""
        current = sender
        while current is not None and current.parent_conversation_id:
            if current.parent_conversation_id == conversation_id:
                return True
            current = await self._records.by_child(current.parent_conversation_id)
        return False

    async def _is_below(self, row: SubagentRecord, conversation_id: str) -> bool:
        current: SubagentRecord | None = row
        while current is not None:
            if current.parent_conversation_id == conversation_id:
                return True
            if not current.parent_conversation_id:
                return False
            current = await self._records.by_child(current.parent_conversation_id)
        return False

    async def _resolve(self, owner_id: str, root: str, reference: str) -> SubagentRecord:
        """The agent of this tree a handle or callsign names, or a refusal naming it back."""
        return resolve(reference, await self._records.tree(root, owner_id))

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


def _isolation(declared: WorkspacePolicy, *, worktree: bool) -> WorkspacePolicy:
    """Where this launch works: in a worktree of its own when asked for one or when its
    definition declares it, else where the definition says."""
    return "isolated" if worktree else declared


def _workspace_key(base: str, policy: WorkspacePolicy, delegation_id: str) -> str:
    """The key naming the workspace this agent works in. ``own`` is the empty key, which
    every run reads as "my own conversation's"; a worktree of its own is named under the
    thread at the top of the parent's workspace, whose scratch it shares."""
    if policy == "own":
        return ""
    if policy == "shared":
        return base
    return f"{split_workspace_key(base)[0]}{DELEGATION_SEP}{delegation_id}"


def _briefing(text: str) -> InstructionProvider:
    """The agent's brief as a dynamic instruction, rebuilt every turn, so nothing it later
    reads can rewrite the terms it works under."""

    async def provider(_ctx: RunContext[RunDeps]) -> str:
        return text

    return provider


def _title_for(callsign: str, handle: str, task: str) -> str:
    """The agent's thread name, ``CALLSIGN · handle: task`` on one line."""
    folded = " ".join(task.split())
    title = f"{callsign} · {handle}: {folded}" if callsign else f"{handle}: {folded}"
    if len(title) <= _TITLE_MAX_CHARS:
        return title
    return title[: _TITLE_MAX_CHARS - 1] + "…"
