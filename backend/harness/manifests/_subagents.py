"""Launching a sub-agent, and reading one back — the implementation.

The abstraction lives in ``services/subagents/``, where ``tools/`` can name it; this is the
half that composes an actual chat turn, and so has to sit up here with the wiring. Private
module (the leading underscore keeps manifest discovery from mistaking it for a feature)
beside the manifest that builds it, the same split ``_research_threads.py`` uses and for
the same reason.

Everything here is ordinary, and that is the entire point. There is no sub-agent engine —
no run loop, no step budget of its own, no progress protocol, no second way of holding a
message history. Launching one is: make a conversation, decide how far it may reach and
which files it works on, and submit exactly the turn a route would have submitted. Reading
one is: what did it say, and is it still going. The shortness of this module against what
the deleted blocking delegation used to be is the evidence that nothing was rebuilt.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

from agent.summarize import resolve_auto_compact_policy
from harness.manifest import DormantCategory, HarnessContext
from models.subagent import SubagentRecord
from routes.chat import compose_turn, resolve_turn_models
from runs import RunRegistry, RunStatus
from services.agents import AgentDefinition, AgentNameError, lower_of, normalize_agent_name
from services.agents.definition import WorkspacePolicy
from services.conversations import ConversationBinding, ConversationStore
from services.offline import OfflineModeService
from services.permissions import DEFAULT_PERMISSION, stricter_permission
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

logger = logging.getLogger(__name__)

#: The run kind a sub-agent is submitted under. ``linked`` is already defined as "a thread
#: the agent opened for itself" (``runs/lanes.py``) — foreground for the model, background
#: for the operator — which is exactly what this is, so it waits in that lane rather than
#: in one invented here.
SUBAGENT_KIND = "linked"

#: How much of a task is kept as the conversation's name. A sub-agent's thread is hidden
#: from the session list, but the card in the panel is one line wide.
_TITLE_MAX_CHARS = 80

#: How far the isolation asked for at launch may move a definition's own answer. Only one way:
#: a definition that says it works apart cannot be talked into the operator's files, while a
#: definition that shares by default can always be asked to step out of the way.
_ISOLATION_RANK: dict[WorkspacePolicy, int] = {"shared": 0, "isolated": 1, "own": 2}


class ConversationSubagents(SubagentLauncher):
    """Sub-agents, launched and read as ordinary (hidden) conversations.

    Holds handles rather than a request, which is the whole reason it is constructed at
    wiring time: a tool runs deep inside a Run and has no other way to reach turn
    composition.
    """

    def __init__(self, ctx: HarnessContext, records: SubagentStore) -> None:
        self._ctx = ctx
        self._records = records
        self._conversations = ctx.services.get(ConversationStore)
        self._models = ctx.services.get(ModelRegistry)
        self._runs = ctx.services.get(RunRegistry)
        self._settings = ctx.services.get(SettingsStore)
        self._offline = ctx.services.get(OfflineModeService)
        self._uploads = ctx.services.get(UploadStore)
        #: Handles chosen but not yet written down. A launch picks its name at the top and
        #: records the row at the bottom, with a whole turn's composition in between — so
        #: the register cannot answer "what is taken" for a launch that is still in flight,
        #: and the fan-out this feature exists for is exactly the case where two of them
        #: are. Held with the lock below, and dropped once the row lands (or the launch
        #: fails), after which the register answers for it.
        self._reserved: set[tuple[str, str, str]] = set()
        self._reserve_lock = asyncio.Lock()

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
            raise SubagentUnavailableError("A sub-agent needs a task to do.")
        parent = parent or SubagentParent()
        await self._check_capacity(owner_id)
        thread = parent.conversation_id or ""
        handle = await self._handle_for(owner_id, thread, handle, agent)
        try:
            return await self._launch(
                owner_id, agent, task, handle=handle, parent=parent, isolate=isolate
            )
        finally:
            # Held only until the row exists — or until the launch gives up. After that
            # the register answers for the name itself, and a reservation outliving it
            # would suffix the next launch against a sub-agent nobody ever started.
            async with self._reserve_lock:
                self._reserved.discard((owner_id, thread, handle))

    async def _launch(
        self,
        owner_id: str,
        agent: AgentDefinition,
        task: str,
        *,
        handle: str,
        parent: SubagentParent,
        isolate: bool,
    ) -> LaunchedSubagent:
        """Everything a launch does once its name is settled — see :meth:`launch`."""
        try:
            models = await resolve_turn_models(
                self._models, agent.endpoint_id, agent.model, owner_id=owner_id
            )
        except Exception as exc:
            # `resolve_turn_models` answers in HTTP, because its other caller is a route.
            # Down here there is no response to shape — the model asked for a sub-agent and
            # has to be told, in words it can act on, that none can be served.
            raise SubagentUnavailableError(f"No usable model is configured: {exc}") from exc

        # The level the sub-agent runs at, settled before anything is created: what the
        # definition asks for (else the default), capped by what the *parent* was allowed to do.
        # A sub-agent that could act further than the thread that launched it would turn
        # one launch into a standing grant the operator never gave.
        permission = agent.permission_ceiling or DEFAULT_PERMISSION
        if parent.permission is not None:
            permission = stricter_permission(parent.permission, permission)

        disabled = await effective_disabled_tools(
            self._settings,
            self._offline,
            owner_id,
            vision=models[4],
            # Nobody is in the sub-agent's conversation, so it is not offered the tools
            # that would stop and wait for someone.
            kind=SUBAGENT_KIND,
            availability=self._ctx.category_availability,
            caps=self._ctx.capabilities,
        )
        disabled = disabled | _NO_RECURSION
        # Refuse rather than degrade. A sub-agent missing the one capability its whole
        # job rests on still answers — from the model's own memory — and reads, to
        # everyone downstream, exactly as though it had done the work.
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
            # Nothing to share. Sharing nothing is not a failure — it just means this
            # sub-agent works in a workspace of its own, which is what every thread with
            # no parent already does. Forking nothing *is* a failure, because there is no
            # copy to take and no merge to make afterwards.
            if policy == "isolated":
                raise SubagentUnavailableError(
                    "This conversation has no workspace to give a sub-agent its own copy "
                    "of. Launch it without isolation, or do the work here."
                )
            policy = "own"
        workspace_key = _workspace_key(base, policy, delegation_id)

        # Where the files are follows from the policy: a workspace of its own is a
        # sandbox, and sharing or forking the parent's keeps the parent's kind. The
        # operator's own folder cannot be forked; that takes a worktree.
        parent_kind = workspace_kind(parent.workspace_kind)
        if policy == "isolated" and parent_kind == "checkout":
            raise SubagentUnavailableError(
                "This conversation works directly in the project's folder, which cannot "
                "be copied for a sub-agent. Call `project_open_worktree` first, or launch "
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
        )
        conversation_id = await self._conversations.create_conversation(
            owner_id,
            title=_title_for(handle, task),
            project_id=parent.project_id,
            workspace_kind=child_kind,
            permission=permission,
            agent_name=agent.name,
            # Hidden from the session list: nobody can type into a sub-agent, so a thread
            # they cannot use would be noise in the one place they look for their own work.
            # It stays a whole real conversation underneath — readable and streamable by id
            # — which is what the panel opens when a card is expanded.
            ephemeral=True,
        )

        created = compose_turn(
            prompt=task,
            conversation_id=conversation_id,
            models=models,
            # The same assembled capability bag, catalog and instruction set an interactive
            # turn gets, plus this sub-agent's own standing brief. A sub-agent must not
            # quietly be a lesser agent than the one that launched it.
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
            # Which files it works on: the launching thread's own workspace, a delegated
            # fork of it, or one of its own (`services/workspace.py` reads the key).
            workspace_key=workspace_key,
            # Folded, never substituted. A definition is a request and not a grant — the same
            # rule its permission ceiling is read under — so a sub-agent asking for 60
            # round trips on an installation whose operator deliberately lowered theirs to
            # 10 gets 10. Where the operator set nothing, the definition's number stands: that is
            # a floor raised over a default nobody chose.
            request_limit=lower_of(
                agent.request_limit,
                await get_agent_request_limit_override(self._settings, owner_id),
            ),
            context_thresholds=await get_context_thresholds(self._settings, owner_id),
            auto_compact=await resolve_auto_compact_policy(self._settings, owner_id),
            owner_id=owner_id,
            kind=SUBAGENT_KIND,
            # A thread nobody is sitting in front of needs a wall clock: the inactivity
            # watchdog cannot end it (a model streaming tokens refreshes that clock on
            # every frame), and a wedged sub-agent would hold its lane for as long as it
            # cared to.
            wall_clock_timeout_s=lower_of(
                agent.wall_clock_timeout_s,
                self._ctx.settings.subagent_wall_clock_timeout_s,
            ),
            # A sub-agent's thread is hidden, so auto-titling it is invisible work that
            # only holds the run open after the answer — it is named from its task above.
            ephemeral=True,
        )

        subagent_id = created.run_id
        await self._records.record(
            subagent_id=subagent_id,
            owner_id=owner_id,
            parent_conversation_id=parent.conversation_id or "",
            child_conversation_id=conversation_id,
            run_id=created.run_id,
            parent_run_id=parent.run_id,
            spec_name=agent.name,
            handle=handle,
            task=task,
            workspace_policy=policy,
            workspace_key=workspace_key,
            delegation_id=delegation_id if policy == "isolated" else None,
            # Stored, not re-derived later: the fork is landed from a terminal hook, long
            # after the binding that knew these is out of reach.
            workspace_kind=child_kind,
            project_id=parent.project_id,
        )
        return LaunchedSubagent(
            subagent_id=subagent_id,
            conversation_id=conversation_id,
            run_id=created.run_id,
            name=agent.name,
            handle=handle,
            task=task,
        )

    async def _handle_for(
        self, owner_id: str, parent_conversation_id: str, requested: str, agent: AgentDefinition
    ) -> str:
        """The name this sub-agent will answer to — normalised, and unique in its thread.

        Both halves are done here rather than at the tool, and for different reasons. The
        *rule* is ``normalize_agent_name``'s, shared with the one that reads a project's
        agent files, because a launch handle and an agent file's name are read in the same
        places by the same eyes. The *uniqueness* is here because it is a fact about the
        register, which the tool cannot see — and because a model launching three
        ``explorer``s in one step has no way to know what the other two took.

        Suffixed rather than refused on a clash. A launch is the expensive thing the model
        just decided to do; failing it over a name would spend a retry re-deciding, and the
        suffix is legible enough that ``explorer-2`` in a report is obviously the second
        explorer. The returned handle is therefore what the caller must tell the model,
        never what it asked for.

        **Reserved, not merely read.** A model launching three ``explorer``s in one step
        launches them *concurrently*, and a launch does not write its row until a whole
        turn has been composed — so three reads of the register would all answer "nothing
        taken" and all three would be called ``explorer``, after which ``by_handle`` lands
        every direction on the newest and the other two are unaddressable for good. So the
        register and the launches still in flight are consulted together, under a lock, and
        the name is claimed before this returns.
        """
        try:
            base = normalize_agent_name(requested) if requested.strip() else agent.name
        except AgentNameError as exc:
            # Defence in depth: the tool validates first, so this catches a caller inside
            # the process rather than the model. Reported as a state rather than raised as
            # a bug, because everything else this seam refuses is reported that way.
            raise SubagentUnavailableError(str(exc)) from exc
        async with self._reserve_lock:
            taken = await self._records.handles_for_parent(parent_conversation_id, owner_id)
            taken |= {
                handle
                for owner, thread, handle in self._reserved
                if owner == owner_id and thread == parent_conversation_id
            }
            chosen = base
            suffix = 2
            while chosen in taken:
                chosen = f"{base}-{suffix}"
                suffix += 1
            self._reserved.add((owner_id, parent_conversation_id, chosen))
            return chosen

    async def _check_capacity(self, owner_id: str) -> None:
        """Refuse a launch that would put the operator over the cap they set.

        Counted from the register rather than from a number held here, because the register
        is what survives a restart and what the panel draws from — a second count would be
        the one that was wrong. A sub-agent parked on an approval still counts: what it is
        waiting for is a person, not a slot, and freeing its budget would let the model pile
        up work behind a decision nobody has made yet.

        Checked before anything is created, so a refusal leaves no conversation, no row and
        no forked workspace behind.
        """
        limit = await get_subagent_limit(self._settings, owner_id)
        if limit is None:
            return
        running = len(await self._records.live(owner_id))
        if running < limit:
            return
        # Phrased as a state of the world with a way out of it. A model told only "no" will
        # try again immediately and spend the turn doing it; a model told what it is waiting
        # for, and that it will be told when the wait is over, stops.
        raise SubagentUnavailableError(
            f"{running} sub-agents are already running, which is the most you have been "
            "given at once. Do not launch another yet and do not retry this — you will be "
            "told as each one finishes, and you can launch again then. Carry on with "
            "whatever does not depend on them, or end your turn."
        )

    async def steer(
        self, owner_id: str, conversation_id: str, handle: str, message: str
    ) -> SubagentView:
        message = message.strip()
        if not message:
            raise SubagentUnavailableError("A direction with nothing in it changes nothing.")
        row = await self._resolve(owner_id, conversation_id, handle)
        run = self._runs.get(row.run_id)
        if run is None or run.is_terminal:
            # Not a failure of the caller's: it is racing something that finished. Say what
            # is true now, because the answer it wanted is already on its way as a report.
            raise SubagentUnavailableError(
                f"`{row.handle or row.spec_name}` has already finished, so there is nothing "
                "left to redirect. Its report is on its way to you — read it, and launch "
                "another sub-agent if there is more to do."
            )
        # The steering road, unchanged: queued on the run and handed to its next
        # not-yet-sent request, so this cannot interrupt a model mid-stream. `source` is
        # what makes it read as its launcher's direction rather than as an operator who is
        # not there (`agent/injected.py` picks the envelope from it).
        run.enqueue_message(message, source="parent")
        return self._live_view(row)

    async def read(self, owner_id: str, conversation_id: str, handle: str) -> SubagentView:
        row = await self._resolve(owner_id, conversation_id, handle)
        view = self._live_view(row)
        if view.summary is None and view.status in {"running", "blocked"}:
            # Nothing reported yet: hand over its latest answer instead, so a parent
            # checking on a long-running sub-agent sees where it has got to rather than a
            # blank. A current best, explicitly not a report.
            view = _with_summary(view, await self._latest_answer(row.child_conversation_id))
        return view

    async def _resolve(
        self, owner_id: str, conversation_id: str, handle: str
    ) -> SubagentRecord:
        """The one sub-agent a thread calls ``handle``, or an answer the model can use.

        The refusal names the handle back, because the failure this is nearly always
        reporting is a model addressing a sub-agent by a name it half-remembers — and the
        useful thing to tell it is which name did not land, not that a lookup failed.
        Normalised on the way in so ``Helper Function Finder`` reaches the same row
        ``helper-function-finder`` does; an unusable one simply matches nothing, which is
        the truth and needs no separate branch.
        """
        try:
            handle = normalize_agent_name(handle)
        except AgentNameError:
            pass
        row = await self._records.by_handle(conversation_id, handle, owner_id)
        if row is None:
            raise SubagentUnavailableError(
                f"You have no sub-agent called {handle!r}. Use the name you gave it when "
                "you launched it — `subagents_list` has the ones still working."
            )
        return row

    async def live(
        self, owner_id: str, *, conversation_id: str | None = None
    ) -> list[SubagentView]:
        rows = await self._records.live(owner_id, parent_conversation_id=conversation_id)
        return [self._live_view(row) for row in rows]

    async def for_parent(self, owner_id: str, conversation_id: str) -> list[SubagentView]:
        """Every sub-agent a thread launched, live and finished — the panel's backfill."""
        rows = await self._records.for_parent(conversation_id, owner_id)
        return [self._live_view(row) for row in rows]

    def _live_view(self, row) -> SubagentView:
        """A stored row, corrected by the Run if one is still going.

        The register is written at the ends of a sub-agent's life; a run in flight knows
        more than it does — whether it has parked on an approval, and how full its context
        has become — so the live numbers win where there are any.
        """
        run = self._runs.get(row.run_id)
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


#: The tools a sub-agent is never offered. Depth stays 1 by construction rather than by a
#: counter somebody has to remember to increment: a tree of agents is unbounded cost and
#: unbounded blast radius, and nothing about the work needs one.
#:
#: ``send`` and ``list`` are here for a second reason, and it is not depth. Nothing about a
#: sub-agent's job needs to reach another one, and both of these are shaped to reach one:
#: ``list`` enumerates a thread's sub-agents and ``send`` redirects one with text the
#: envelope frames as coming from the agent that launched it. Addressing is now scoped to
#: the launching thread — a sub-agent asking about its *own* thread would find nothing,
#: because it launched nothing — so this is belt and braces rather than the only thing
#: holding the door; it stays because a tool that can only ever return nothing is a tool
#: the model spends a call discovering is useless. (``subagents_read`` stays for the
#: mirror-image reason: it only reads, and with no way to launch, a sub-agent has no handle
#: to read but ones it never created.)
_NO_RECURSION: frozenset[str] = frozenset(
    {"subagents_launch", "subagents_send", "subagents_list"}
)


def _isolation(declared: WorkspacePolicy, *, isolate: bool) -> WorkspacePolicy:
    """Where this launch works, folding the caller's request into the definition's own answer.

    Isolation only ever increases. The launching model knows whether *this* task is one it
    means to work alongside, which the definition cannot; what it does not get to do is pull a
    sub-agent written to stay out of the way back into the operator's own files.
    """
    asked: WorkspacePolicy = "isolated" if isolate else declared
    return asked if _ISOLATION_RANK[asked] > _ISOLATION_RANK[declared] else declared


def _workspace_key(base: str, policy: WorkspacePolicy, delegation_id: str) -> str:
    """The key naming the workspace this sub-agent works in.

    ``own`` is the empty key, which is how every ordinary run says "my own conversation's"
    — the sub-agent's run fills it in for itself, and nothing here has to know how.
    """
    if policy == "own":
        return ""
    if policy == "shared":
        return base
    return f"{base}{DELEGATION_SEP}{delegation_id}"


def _briefing(text: str):
    """This sub-agent's brief as a dynamic instruction.

    An instruction rather than its opening message, because that is the difference between
    a brief and a thing it was told once: instructions are rebuilt every turn and never
    read back out of history, so a file the sub-agent reads later cannot rewrite the terms
    it is working under.
    """

    async def provider(_ctx) -> str:
        return text

    return provider


def _title_for(handle: str, task: str) -> str:
    """The sub-agent's thread name — what it is, and what it was asked to do, on one line.

    The task *is* the title, rather than the thread being auto-named from its first
    exchange: nobody wrote this thread's opening message, and a card reading "Untitled" is
    a card the operator cannot tell from the three beside it.

    Named by its *handle* and not by its definition, for the same reason: three ``explorer``s out
    at once produce three titles beginning ``explorer:`` and differing only in however much
    of the task survives the width, while three handles differ in the first word.
    """
    folded = " ".join(task.split())
    title = f"{handle}: {folded}"
    if len(title) <= _TITLE_MAX_CHARS:
        return title
    return title[: _TITLE_MAX_CHARS - 1] + "…"


def _with_summary(view: SubagentView, summary: str | None) -> SubagentView:
    from dataclasses import replace

    return replace(view, summary=summary)
